"""
Ribbon SBC Web Session Client

Automates firmware upgrade by replicating the Ribbon WebUI CGI/PHP workflow.
No formal REST API is available for firmware operations — this client uses the
same HTTP requests the browser makes.

Login flow (3 steps):
  1. GET  /cgi/index.php               → establishes PHPSESSID + csrfp_token + splashCookie
  2. POST /cgi/login/login.php         → passes the pre-login splash/banner screen
  3. POST /cgi/login/login_do.php      → actual username/password authentication

Upgrade flow (confirmed against browser HAR capture):
  1. Login  (3 steps above)
  2. Scrape → GET  upgrade page, extract ALL hidden form fields (__m_* metadata + platform fields)
  3. Validate → GET /cgi/system/validateSwUpgrade.php?backupFlag=1&partition=3
  4. Backup  → POST /cgi/system/configBackup.php  (backupState=pending, all form fields)
  5. Marker  → GET /cgi/system/validateSwUpgrade.php?createFile=true
  6. Upload  → POST /cgi/system/swDownload_do.phpx (firmware file + ALL form fields, backupState=completed)
               This is a long-running request — the upload itself takes several minutes for large files.
               The 200 response arrives only after the file is fully received by the device.
  7. Install → GET /cgi/system/swUpgradeStatus.php?partitionNumber=3&setActive=true (poll ~2s intervals)
               Returns "status;step;startEpoch;currentEpoch" while installing.
               Terminal success: "Success:System was successfully upgraded. System will reboot now"
               Device then reboots automatically.
  8. Online  → poll getHighestSevAlarm.php until device responds again after reboot (~5-10 min)
  9. Verify  → re-login + parse version from index.php
"""

import asyncio
import logging
import uuid
import warnings
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# Suppress HTTPS warnings for self-signed certs once
warnings.filterwarnings("once", category=UserWarning, module="httpx")
logger = logging.getLogger(__name__)

_INSTALL_POLL_INTERVAL = 2    # seconds between swUpgradeStatus polls
_INSTALL_TIMEOUT = 300        # seconds to wait for install to complete after upload
_REBOOT_POLL_INTERVAL = 15   # seconds between online polls
_REBOOT_TIMEOUT = 660         # seconds (11 min) to wait for device to come back
_UPLOAD_TIMEOUT = 900         # seconds — large firmware files (~100 MB) can take 8+ minutes


class RibbonLoginError(Exception):
    pass


class RibbonUpgradeError(Exception):
    pass


def _normalize_hypervisor(text: str) -> str | None:
    """Map a raw hypervisor string from the device to a canonical tag."""
    import re
    v = text.lower()
    if re.search(r"hyper.?v", v):
        return "HYPERV"
    if "kvm" in v:
        return "KVM"
    if "vmware" in v or "esxi" in v:
        return "VMWARE"
    return None


class _MultipartProgressStream(httpx.AsyncByteStream):
    """
    Streaming multipart/form-data body that updates a progress dict as bytes are sent.

    Passing this as content= to httpx.AsyncClient.build_request() keeps the firmware
    file streaming off disk rather than loading it into memory, and lets us report
    per-chunk progress to the caller without using httpx internals.
    """

    def __init__(self, fields: dict[str, str], firmware_path: Path, progress: dict) -> None:
        self.boundary = uuid.uuid4().hex
        self._fields = fields
        self._firmware_path = firmware_path
        self._progress = progress
        self.content_type = f"multipart/form-data; boundary={self.boundary}"

        file_size = firmware_path.stat().st_size
        progress["total"] = file_size
        progress["sent"] = 0
        self.length = self._calc_length(file_size)

    def _calc_length(self, file_size: int) -> int:
        """Pre-compute the exact Content-Length so the device doesn't need chunked encoding."""
        total = 0
        for name, value in self._fields.items():
            total += len(
                f"--{self.boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{name}\"\r\n"
                f"\r\n"
                f"{value}\r\n"
            )
        filename = self._firmware_path.name
        total += len(
            f"--{self.boundary}\r\n"
            f"Content-Disposition: form-data; name=\"Filename\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"\r\n"
        )
        total += file_size
        total += len(f"\r\n--{self.boundary}--\r\n")
        return total

    async def __aiter__(self):
        for name, value in self._fields.items():
            yield (
                f"--{self.boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{name}\"\r\n"
                f"\r\n"
                f"{value}\r\n"
            ).encode("utf-8")

        filename = self._firmware_path.name
        yield (
            f"--{self.boundary}\r\n"
            f"Content-Disposition: form-data; name=\"Filename\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"\r\n"
        ).encode("utf-8")

        with open(self._firmware_path, "rb") as fh:
            while chunk := fh.read(65536):
                yield chunk
                self._progress["sent"] += len(chunk)
                await asyncio.sleep(0)  # yield control so the progress updater task can run

        yield f"\r\n--{self.boundary}--\r\n".encode("utf-8")


class RibbonWebClient:
    """
    Web-session client for a single Ribbon SBC device.
    Maintains a cookie jar across requests (PHPSESSID + csrfp_token).
    """

    def __init__(self, ip: str, username: str, password: str) -> None:
        self.base_url = f"https://{ip}"
        self.username = username
        self.password = password
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "RibbonWebClient":
        self._client = httpx.AsyncClient(
            verify=False,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, read=60.0),
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Authentication ─────────────────────────────────────────────────────

    async def login(self) -> None:
        """
        Three-step login flow for Ribbon SBC WebUI.

        Step 1: GET /cgi/index.php           — establishes PHPSESSID + csrfp_token + splashCookie
        Step 2: POST /cgi/login/login.php    — acknowledges the pre-login banner/splash
        Step 3: POST /cgi/login/login_do.php — submits username + password
        """
        # Step 1 — Establish session cookies
        await self._client.get(f"{self.base_url}/cgi/index.php")

        if "PHPSESSID" not in self._client.cookies:
            raise RibbonLoginError("Could not establish session with device — check IP/connectivity")

        # Step 2 — Pass the pre-login splash/banner screen
        await self._client.post(
            f"{self.base_url}/cgi/login/login.php",
            data={"splashbutton": " Enter "},
        )

        # Step 3 — Submit credentials
        resp = await self._client.post(
            f"{self.base_url}/cgi/login/login_do.php",
            data={
                "username": self.username,
                "password": self.password,
                "loginbutton": "Login",
                "passwordNonce-Hidden": "password",
                "NewPasswordNonce-Hidden": "NewPassword",
                "ConfirmNewPasswordNonce-Hidden": "ConfirmNewPassword",
            },
        )

        # A failed login re-renders the login form; a success redirects to /cgi/index.php
        if "loginForm" in resp.text or "loginbutton" in resp.text:
            raise RibbonLoginError("Login failed — invalid username or password")

        logger.debug(f"Login successful for {self.base_url}")

    # ── Version Detection ──────────────────────────────────────────────────

    async def get_hypervisor(self) -> str | None:
        """
        Detect the hypervisor platform of a SWe Edge device by querying the System Overview page.
        Returns 'KVM', 'HYPERV', 'VMWARE', or None if not detected.

        The System Overview page exposes a 'Hypervisor Environment' field whose value is rendered
        in the page HTML. This is only meaningful for SWe Edge (virtualised) devices.
        """
        import re
        try:
            resp = await self._client.get(
                f"{self.base_url}/cgi/phpUI/callDetailsEngine.php",
                params={"cfg": "/views/system/systemOverview.xml"},
                timeout=15.0,
            )
        except Exception as e:
            logger.debug(f"get_hypervisor fetch failed: {e}")
            return None

        text = resp.text
        soup = BeautifulSoup(text, "lxml")

        # Strategy 1: element with id='rt_Hypervisor_Environment'
        el = soup.find(id="rt_Hypervisor_Environment")
        if el:
            return _normalize_hypervisor(el.get_text(strip=True))

        # Strategy 2: label "Hypervisor Environment" → adjacent cell value
        for label_tag in soup.find_all(string=re.compile(r"Hypervisor\s+Environment", re.I)):
            parent = label_tag.parent
            sibling = parent.find_next_sibling()
            if sibling:
                result = _normalize_hypervisor(sibling.get_text(strip=True))
                if result:
                    return result
            row = parent.find_parent("tr")
            if row:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    result = _normalize_hypervisor(cells[-1].get_text(strip=True))
                    if result:
                        return result

        # Strategy 3: scan raw lines for known hypervisor strings
        for line in text.splitlines():
            if re.search(r"hyper.?v|kvm|vmware|esxi", line, re.I):
                result = _normalize_hypervisor(line)
                if result:
                    return result

        logger.debug("Hypervisor type not found in system overview page")
        return None

    async def get_version(self) -> str | None:
        """
        Parse the current firmware version from the main WebUI page.
        Returns version string or None if it cannot be determined.
        """
        resp = await self._client.get(f"{self.base_url}/cgi/index.php")
        soup = BeautifulSoup(resp.text, "lxml")

        # Ribbon displays version in a <span> or title like "SBC Software Version X.Y.Z"
        for tag in soup.find_all(string=True):
            text = str(tag).strip()
            import re
            m = re.search(r"(?:Version\s+|V)?(\d+\.\d+\.\d+(?:-\d+)?)", text, re.IGNORECASE)
            if m and "." in m.group(1):
                version = m.group(1)
                logger.debug(f"Detected version: {version}")
                return version

        # Fall back: look for the JS file path which often encodes the version
        for script in soup.find_all("script", src=True):
            src = script.get("src", "")
            import re
            m = re.search(r"(\d+\.\d+\.\d+)", src)
            if m:
                return m.group(1)

        return None

    # ── Upgrade Steps ──────────────────────────────────────────────────────

    async def scrape_upgrade_form_fields(self) -> dict[str, str]:
        """
        Load the upgrade task page and extract ALL hidden form fields.
        These include platform metadata (__m_*), HA config, asmVersion, platformType etc.
        The complete field set is needed verbatim for both configBackup.php and swDownload_do.phpx.
        The Filename file input is excluded here — it is handled separately in upload_firmware().
        """
        resp = await self._client.get(
            f"{self.base_url}/cgi/phpUI/config.php",
            params={
                "cfg": "/views/system/codeUploadTask.xml",
                "type": "Partition",
                "popup": "false",
                "navigationContext": "Tasks",
            },
        )
        soup = BeautifulSoup(resp.text, "lxml")
        fields: dict[str, str] = {}
        for inp in soup.find_all("input"):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name and name != "Filename":  # file input handled separately
                fields[name] = value
        logger.debug(f"Scraped {len(fields)} form fields from upgrade page")
        return fields

    async def validate_upgrade(self, partition: int = 3) -> dict:
        """Pre-flight check. Returns the text response from the device."""
        resp = await self._client.get(
            f"{self.base_url}/cgi/system/validateSwUpgrade.php",
            params={"backupFlag": "1", "partition": str(partition)},
        )
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    async def backup_config(self, form_fields: dict[str, str]) -> bytes:
        """
        Trigger config backup before upgrade.
        Sends ALL scraped form fields with backupState=pending.
        The device responds by streaming back a .tar config backup.
        Returns the raw backup bytes so the caller can persist them.
        """
        payload = {
            **form_fields,
            "backupState": "pending",
            "Passphrase": "",
            "backupPassphrase": "",
            "confirmBackupPassphrase": "",
        }
        payload.pop("Filename", None)
        resp = await self._client.post(
            f"{self.base_url}/cgi/system/configBackup.php",
            data=payload,
            timeout=120.0,
        )
        logger.debug(f"Config backup response: {resp.status_code}, size: {len(resp.content)} bytes")
        return resp.content

    async def create_upload_marker(self) -> None:
        """Ask the server to create the upload slot."""
        resp = await self._client.get(
            f"{self.base_url}/cgi/system/validateSwUpgrade.php",
            params={"createFile": "true"},
        )
        text = resp.text.strip()
        logger.debug(f"Upload marker response: {resp.status_code} — {text[:200]}")
        if "SuccessBackup" not in text and resp.status_code not in (200, 302):
            raise RibbonUpgradeError(f"Upload marker failed: {text[:200]}")

    async def upload_firmware(
        self,
        firmware_path: Path,
        form_fields: dict[str, str],
        partition: int = 3,
        progress: dict | None = None,
    ) -> None:
        """
        Upload the firmware image file to the device.

        Uses ALL scraped form fields (not a whitelist) to exactly match what the browser sends,
        including platformType, HA, asmVersion, hastandalone and all __m_* fields.

        When `progress` is provided (a dict with 'sent' and 'total' keys), the upload streams
        the file in 64 KB chunks and updates progress['sent'] as bytes are sent. A background
        task in the caller can read this dict and persist it to the DB for live UI display.

        The upload itself is the long step — large files (~100 MB) typically take 8+ minutes.
        The 200 response only returns after the file is fully received by the device.
        After this returns, call wait_for_install() to monitor the installation progress.
        """
        # Start with ALL scraped fields so every device-specific field is preserved verbatim
        merged = {**form_fields}
        merged.pop("Filename", None)  # handled separately as a multipart file part

        # Override the fields we control explicitly
        merged.update({
            "backupState": "completed",  # backup has been performed
            "Partition": str(partition),
            "setActive": "true",
            "action": "codeUpload",
            "downloadType": "1",
            "Passphrase": "",
            "backupPassphrase": "",
            "confirmBackupPassphrase": "",
        })

        if progress is not None:
            # Streaming upload with per-chunk progress tracking
            stream = _MultipartProgressStream(merged, firmware_path, progress)
            request = self._client.build_request(
                "POST",
                f"{self.base_url}/cgi/system/swDownload_do.phpx",
                content=stream,
                headers={
                    "Content-Type": stream.content_type,
                    "Content-Length": str(stream.length),
                },
                extensions={"timeout": httpx.Timeout(_UPLOAD_TIMEOUT).as_dict()},
            )
            resp = await self._client.send(request)
        else:
            with open(firmware_path, "rb") as fh:
                files = {"Filename": (firmware_path.name, fh, "application/octet-stream")}
                resp = await self._client.post(
                    f"{self.base_url}/cgi/system/swDownload_do.phpx",
                    data=merged,
                    files=files,
                    timeout=_UPLOAD_TIMEOUT,
                )

        if resp.status_code not in (200, 202, 302):
            raise RibbonUpgradeError(
                f"Firmware upload returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        logger.info(f"Firmware upload complete, HTTP {resp.status_code}")

    async def wait_for_install(self, partition: int = 3, timeout: int = _INSTALL_TIMEOUT) -> bool:
        """
        After upload_firmware() returns, the device installs the firmware from the uploaded file.
        Poll swUpgradeStatus.php until the device signals successful installation and imminent reboot.

        Response format while installing: "status;step;startEpoch;currentEpoch"
        Terminal success string: "Success:System was successfully upgraded. System will reboot now"

        Returns True on success, False on timeout.
        """
        elapsed = 0
        await asyncio.sleep(5)
        elapsed = 5

        while elapsed < timeout:
            try:
                resp = await self._client.get(
                    f"{self.base_url}/cgi/system/swUpgradeStatus.php",
                    params={"partitionNumber": str(partition), "setActive": "true"},
                    timeout=10.0,
                )
                text = resp.text.strip()
                logger.debug(f"swUpgradeStatus ({elapsed}s): {text[:120]}")

                if "Success:System was successfully upgraded" in text:
                    logger.info("Device confirmed successful installation — rebooting now")
                    return True

                if text.startswith("Error") or text.startswith("Fail"):
                    raise RibbonUpgradeError(f"Upgrade status reported failure: {text[:200]}")

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                logger.debug(f"Status poll connection error ({elapsed}s)")

            await asyncio.sleep(_INSTALL_POLL_INTERVAL)
            elapsed += _INSTALL_POLL_INTERVAL

        return False

    async def wait_for_online(self, timeout: int = _REBOOT_TIMEOUT) -> bool:
        """
        After the install success signal the device reboots. Poll until it responds again.
        Uses getHighestSevAlarm.php as a lightweight liveness check.
        Returns True when device is back online, False on timeout.
        """
        # Give the device time to actually start rebooting before polling
        await asyncio.sleep(30)
        elapsed = 30

        while elapsed < timeout:
            try:
                resp = await self._client.get(
                    f"{self.base_url}/cgi/system/getHighestSevAlarm.php",
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    logger.info(f"Device back online after reboot ({elapsed}s elapsed)")
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                logger.debug(f"Device not yet available ({elapsed}s elapsed)…")

            await asyncio.sleep(_REBOOT_POLL_INTERVAL)
            elapsed += _REBOOT_POLL_INTERVAL

        return False

    # ── Certificate Management ──────────────────────────────────────────────

    async def get_certificate_info(self) -> dict:
        """
        Fetch and parse the current platform (UX) server certificate details.

        POSTs to serverCertDisplay.php (mirroring what the browser does when the
        Server Certificates tab loads) and extracts all FormName/FormValue pairs.

        Returns a dict with keys: subject, issuer, certificate (each a dict of field→value),
        plus a flat 'raw' dict of all label→value pairs.
        """
        resp = await self._client.post(
            f"{self.base_url}/cgi/system/serverCertDisplay.php",
            params={
                "cert": "platform",
                "hasTitle": "no",
                "navigationContext": "Settings_0",
                "tabContainer": "Settings_ServerCertificates",
                "tabId": "0",
                "renderOnLoadUnload": "no",
            },
            data={"async": "false"},
            timeout=30.0,
        )
        if resp.status_code not in (200,):
            raise RibbonUpgradeError(
                f"Certificate display returned HTTP {resp.status_code}"
            )

        soup = BeautifulSoup(resp.text, "lxml")
        raw: dict[str, str] = {}
        for row in soup.find_all("tr", class_="FormRow"):
            name_td = row.find("td", class_="FormName")
            value_td = row.find("td", class_="FormValue")
            if name_td and value_td:
                key = name_td.get_text(strip=True)
                # Strip nested HTML tags (e.g. <div class="StatusNormalDetails"><b>OK</b></div>)
                val = value_td.get_text(strip=True)
                if key:
                    raw[key] = val

        # Group into logical sections based on the FieldGroup titles
        sections: dict[str, dict] = {}
        for group in soup.find_all("div", class_="FieldGroup"):
            title_el = group.find("div", class_="FieldGroupTitle")
            if not title_el:
                continue
            section_name = title_el.get_text(strip=True)
            section_data: dict[str, str] = {}
            for row in group.find_all("tr", class_="FormRow"):
                name_td = row.find("td", class_="FormName")
                value_td = row.find("td", class_="FormValue")
                if name_td and value_td:
                    key = name_td.get_text(strip=True)
                    val = value_td.get_text(strip=True)
                    if key:
                        section_data[key] = val
            if section_data:
                sections[section_name] = section_data

        logger.debug(f"Parsed certificate info: {list(raw.keys())}")
        return {"sections": sections, "raw": raw}

    async def upload_certificate(self, cert_bytes: bytes, cert_filename: str) -> str:
        """
        Upload a PEM certificate to replace the platform (UX) server certificate.

        Flow (mirrors what the browser does when clicking Import > X.509 Signed Certificate):
          1. GET  /cgi/phpUI/config.php?cfg=/views/system/uxServerCertificateImport.xml&type=UXCertificate
                  — loads the import dialog form, extracts all hidden fields
          2. POST /cgi/phpUI/config_do.php
                  — multipart form submit with cert file + all hidden fields

        Returns the response text from config_do.php (success or error message).
        """
        # Step 1 — Load the import form to get hidden fields
        resp = await self._client.get(
            f"{self.base_url}/cgi/phpUI/config.php",
            params={
                "cfg": "/views/system/uxServerCertificateImport.xml",
                "type": "UXCertificate",
            },
        )
        soup = BeautifulSoup(resp.text, "lxml")
        fields: dict[str, str] = {}
        for inp in soup.find_all("input"):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name and inp.get("type", "").lower() != "file":
                fields[name] = value
        for sel in soup.find_all("select"):
            name = sel.get("name", "")
            if name:
                opt = sel.find("option", selected=True)
                fields[name] = opt["value"] if opt else ""

        # Ensure the key fields are set correctly for a file-based import
        fields.setdefault("type", "UXCertificate")
        fields.setdefault("certificateType", "ux")
        fields.setdefault("cfg", "/views/system/uxServerCertificateImport.xml")
        fields["CertFileOperation-Field"] = "2"   # 2 = file upload (1 = paste)

        logger.debug(f"Certificate import form fields: {list(fields.keys())}")

        # Step 2 — POST the certificate file to config_do.php
        files = {"CertFileName-Field": (cert_filename, cert_bytes, "application/octet-stream")}
        resp = await self._client.post(
            f"{self.base_url}/cgi/phpUI/config_do.php",
            data=fields,
            files=files,
            timeout=60.0,
        )

        if resp.status_code not in (200, 302):
            raise RibbonUpgradeError(
                f"Certificate upload returned HTTP {resp.status_code}: {resp.text[:500]}"
            )

        text = resp.text
        logger.info(f"Certificate upload response ({resp.status_code}): {text[:200]}")

        # The device returns an error string starting with "Error" on failure
        if "error" in text.lower() and "success" not in text.lower():
            import re
            # Try to extract a human-readable error
            m = re.search(r"Error[^<\n]*", text, re.IGNORECASE)
            if m:
                raise RibbonUpgradeError(f"Certificate import failed: {m.group(0)}")

        return text

    # ── Convenience ────────────────────────────────────────────────────────

    async def test_connection(self) -> tuple[bool, str]:
        """Login and return (success, message)."""
        try:
            await self.login()
            version = await self.get_version()
            return True, version or "Connected (version unknown)"
        except RibbonLoginError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Connection error: {e}"
