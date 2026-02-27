"""
Ribbon SBC Web Session Client

Automates firmware upgrade by replicating the Ribbon WebUI CGI/PHP workflow.
No formal REST API is available for firmware operations — this client uses the
same HTTP requests the browser makes.

Login flow (3 steps):
  1. GET  /cgi/index.php               → establishes PHPSESSID + csrfp_token + splashCookie
  2. POST /cgi/login/login.php         → passes the pre-login splash/banner screen
  3. POST /cgi/login/login_do.php      → actual username/password authentication

Upgrade flow:
  1. Login  (3 steps above)
  2. Scrape → GET  upgrade page, extract hidden form fields (__m_* metadata)
  3. Validate → GET /cgi/system/validateSwUpgrade.php?backupFlag=1&partition=3
  4. Backup  → POST /cgi/system/configBackup.php  (backupState=pending)
  5. Marker  → GET /cgi/system/validateSwUpgrade.php?createFile=true
  6. Upload  → POST /cgi/system/swDownload_do.phpx (firmware file + form fields)
  7. Poll    → GET /cgi/system/getHighestSevAlarm.php (wait for reboot completion)
  8. Verify  → re-login + parse version
"""

import asyncio
import logging
import warnings
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# Suppress HTTPS warnings for self-signed certs once
warnings.filterwarnings("once", category=UserWarning, module="httpx")
logger = logging.getLogger(__name__)

_REBOOT_POLL_INTERVAL = 15  # seconds
_REBOOT_TIMEOUT = 660  # seconds (11 min)
_UPLOAD_TIMEOUT = 600  # seconds (firmware files can be large)


class RibbonLoginError(Exception):
    pass


class RibbonUpgradeError(Exception):
    pass


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
        # Detect failure by looking for the login form still being present
        if "loginForm" in resp.text or "loginbutton" in resp.text:
            raise RibbonLoginError("Login failed — invalid username or password")

        logger.debug(f"Login successful for {self.base_url}")

    # ── Version Detection ──────────────────────────────────────────────────

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
            # Look for patterns like "13.0.0" or "V13.0.0" or "Version 13.0.0"
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
        Load the upgrade task page and extract all hidden form fields.
        These include platform metadata (__m_*) that the PHP backend may validate.
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
            if name:
                fields[name] = value
        logger.debug(f"Scraped {len(fields)} form fields from upgrade page")
        return fields

    async def validate_upgrade(self, partition: int = 3) -> dict:
        """Pre-flight check. Returns the JSON/text response from the device."""
        resp = await self._client.get(
            f"{self.base_url}/cgi/system/validateSwUpgrade.php",
            params={"backupFlag": "1", "partition": str(partition)},
        )
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    async def backup_config(self, form_fields: dict[str, str]) -> None:
        """Trigger config backup before upgrade."""
        payload = {**form_fields, "backupState": "pending", "action": "codeUpload"}
        resp = await self._client.post(
            f"{self.base_url}/cgi/system/configBackup.php",
            data=payload,
        )
        logger.debug(f"Config backup response: {resp.status_code}")

    async def create_upload_marker(self) -> None:
        """Ask the server to create the upload slot."""
        resp = await self._client.get(
            f"{self.base_url}/cgi/system/validateSwUpgrade.php",
            params={"createFile": "true"},
        )
        logger.debug(f"Upload marker response: {resp.status_code} — {resp.text[:200]}")

    async def upload_firmware(
        self,
        firmware_path: Path,
        form_fields: dict[str, str],
        partition: int = 3,
    ) -> None:
        """
        Upload the firmware image file to the device.
        This triggers installation and automatic reboot.
        """
        base_fields = {
            "nestingLevel": "0",
            "cfg": "/views/system/codeUploadTask.xml",
            "type": "Partition",
            "treeNodeID": "",
            "refreshFullTree": "false",
            "openerNavigationContext": "",
            "downloadType": "1",
            "mask": "runtime",
            "Partition": str(partition),
            "setActive": "true",
            "action": "codeUpload",
            "backupState": "completed",
            "fsWare": "Software",
            "MAX_FILE_SIZE": "134217728",
            "operation": "0",
        }
        # Merge scraped __m_* fields on top of base fields
        merged = {**base_fields, **{k: v for k, v in form_fields.items() if k.startswith("__m_") or k in base_fields}}

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
        logger.info(f"Firmware upload submitted, HTTP {resp.status_code}")

    async def wait_for_reboot(self, timeout: int = _REBOOT_TIMEOUT) -> bool:
        """
        Poll the alarm endpoint until the device responds again after reboot.
        Returns True on success, False on timeout.
        """
        # First wait a short initial delay to allow the device to actually start rebooting
        await asyncio.sleep(30)

        elapsed = 30
        while elapsed < timeout:
            try:
                resp = await self._client.get(
                    f"{self.base_url}/cgi/system/getHighestSevAlarm.php",
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    logger.info(f"Device responded after reboot (elapsed {elapsed}s)")
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                logger.debug(f"Device not yet available ({elapsed}s elapsed)…")

            await asyncio.sleep(_REBOOT_POLL_INTERVAL)
            elapsed += _REBOOT_POLL_INTERVAL

        return False

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
