"""
Dialogic BorderNet SBC REST client.

REST API is available on port 8443 (HTTPS, self-signed cert).
Auth: HTTP Basic (username / password).

Key endpoints used by this client:
  POST /system/administration/upload/upgrade  — upload firmware (multipart, field: bnetUpgradeFile)
  PUT  /system/administration/upgrade         — trigger upgrade after upload
  GET  /ems/ka                                — keep-alive / connectivity probe
  GET  /ems/systemInformation                 — system info (used for version)
  GET  /ems/rollback                          — installed versions list
"""

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_PORT = 8443
_UPLOAD_TIMEOUT = 900  # 15 min — large firmware files


class DialogicError(Exception):
    pass


class DialogicClient:
    def __init__(self, ip: str, username: str, password: str) -> None:
        self._base = f"https://{ip}:{_PORT}"
        self._auth = (username, password)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DialogicClient":
        self._client = httpx.AsyncClient(
            verify=False,
            follow_redirects=True,
            auth=self._auth,
            timeout=httpx.Timeout(30.0, read=_UPLOAD_TIMEOUT),
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()

    # ── Connectivity ────────────────────────────────────────────────────────

    async def test_connection(self) -> tuple[bool, str]:
        """Probe the keep-alive endpoint. Any non-5xx reply counts as reachable."""
        assert self._client is not None
        try:
            resp = await self._client.get(
                f"{self._base}/ems/ka", timeout=15.0
            )
            if resp.status_code < 500:
                return True, f"Connected (HTTP {resp.status_code})"
            return False, f"Device returned HTTP {resp.status_code}"
        except httpx.ConnectError as e:
            return False, f"Connection refused: {e}"
        except httpx.TimeoutException:
            return False, "Connection timed out"
        except Exception as e:
            return False, f"Connection error: {e}"

    # ── Version ─────────────────────────────────────────────────────────────

    async def get_version(self) -> str | None:
        """
        Return the running firmware version string, or None if not parseable.

        Tries /ems/rollback first (returns list of installed versions with
        an 'active' flag), then falls back to /ems/systemInformation.
        """
        assert self._client is not None
        # Strategy 1: rollback endpoint returns installed versions
        try:
            resp = await self._client.get(f"{self._base}/ems/rollback", timeout=15.0)
            if resp.status_code == 200:
                data = resp.json()
                # Expect a list of {version, active} or similar objects
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, dict):
                            if entry.get("active") or entry.get("isActive") or entry.get("current"):
                                ver = entry.get("version") or entry.get("versionName") or entry.get("name")
                                if ver:
                                    return str(ver)
                    # If no 'active' flag, return the first entry
                    if data and isinstance(data[0], dict):
                        ver = data[0].get("version") or data[0].get("versionName") or data[0].get("name")
                        if ver:
                            return str(ver)
                elif isinstance(data, dict):
                    ver = data.get("version") or data.get("currentVersion")
                    if ver:
                        return str(ver)
        except Exception as e:
            logger.debug(f"Rollback endpoint failed: {e}")

        # Strategy 2: system information
        try:
            resp = await self._client.get(f"{self._base}/ems/systemInformation", timeout=15.0)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    ver = (
                        data.get("version")
                        or data.get("softwareVersion")
                        or data.get("swVersion")
                        or data.get("firmwareVersion")
                    )
                    if ver:
                        return str(ver)
        except Exception as e:
            logger.debug(f"systemInformation endpoint failed: {e}")

        return None

    # ── Firmware upload ─────────────────────────────────────────────────────

    async def upload_firmware(self, firmware_path: Path) -> str:
        """
        Upload a firmware file using multipart/form-data.

        The Dialogic API expects the file in a multipart field named
        'bnetUpgradeFile' (confirmed from the Spring MVC NullPointerException
        in the Swagger UI response when JSON was sent instead of multipart).

        Returns the response body text on success.
        Raises DialogicError on HTTP error.
        """
        assert self._client is not None
        filename = firmware_path.name
        file_bytes = firmware_path.read_bytes()
        logger.info(f"Uploading {filename} ({len(file_bytes) / 1024 / 1024:.1f} MB) to Dialogic SBC")

        resp = await self._client.post(
            f"{self._base}/system/administration/upload/upgrade",
            files={"bnetUpgradeFile": (filename, file_bytes, "application/octet-stream")},
            timeout=_UPLOAD_TIMEOUT,
        )
        if resp.status_code not in (200, 201, 202, 204):
            raise DialogicError(
                f"Firmware upload failed — HTTP {resp.status_code}: {resp.text[:500]}"
            )
        logger.info(f"Firmware upload response: HTTP {resp.status_code}")
        return resp.text
