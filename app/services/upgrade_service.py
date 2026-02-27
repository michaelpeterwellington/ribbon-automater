"""
Upgrade orchestration service.

`run_upgrade_job(job_id)` is the entry point called by APScheduler or directly
for immediate upgrades. It runs the full CGI web session workflow against the
target device.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Device, FirmwareFile, JobStatus, UpgradeJob
from app.services.audit import audit_log
from app.services.crypto import decrypt_value
from app.services.ribbon_client import RibbonLoginError, RibbonUpgradeError, RibbonWebClient

logger = logging.getLogger(__name__)


# ── Log helpers ────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


async def _append_log(db: AsyncSession, job: UpgradeJob, line: str) -> None:
    job.log = (job.log or "") + f"[{_ts()}] {line}\n"
    await db.commit()
    logger.info(f"[job {job.id}] {line}")


async def _set_status(db: AsyncSession, job: UpgradeJob, status: JobStatus) -> None:
    job.status = status
    if status == JobStatus.UPLOADING and not job.started_at:
        job.started_at = datetime.now(timezone.utc)
    await db.commit()


# ── Main entry point ───────────────────────────────────────────────────────

def run_upgrade_job(job_id: int) -> None:
    """
    Synchronous wrapper called by APScheduler (BackgroundScheduler runs sync functions).
    Spins up a new event loop to run the async workflow.
    """
    asyncio.run(_async_run_upgrade_job(job_id))


async def _async_run_upgrade_job(job_id: int) -> None:
    """Full async upgrade workflow."""
    async with AsyncSessionLocal() as db:
        # Load job with all related objects eagerly — lazy loading raises MissingGreenlet in async context
        result = await db.execute(
            select(UpgradeJob)
            .where(UpgradeJob.id == job_id)
            .options(
                selectinload(UpgradeJob.device).selectinload(Device.customer),
                selectinload(UpgradeJob.firmware),
            )
        )
        job = result.scalar_one_or_none()
        if not job:
            logger.error(f"Upgrade job {job_id} not found")
            return

        if job.status == JobStatus.CANCELLED:
            logger.info(f"Job {job_id} was cancelled before it started")
            return

        device: Device = job.device
        firmware: FirmwareFile = job.firmware

        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            await _run_workflow(db, job, device, firmware)
        except Exception as e:
            await _set_status(db, job, JobStatus.FAILED)
            await _append_log(db, job, f"FATAL ERROR: {e}")
            await audit_log(db, "job.failed",
                            f"Upgrade job #{job.id} FAILED — {device.name} ({device.ip_address})",
                            "job", job.id,
                            {"device": device.name, "ip": device.ip_address,
                             "firmware": firmware.filename, "error": str(e)[:500]})
            await _send_notification(db, job, device, firmware, "failed")


async def _run_workflow(
    db: AsyncSession,
    job: UpgradeJob,
    device: Device,
    firmware: FirmwareFile,
) -> None:
    password = decrypt_value(device.password_encrypted)
    firmware_path = Path(firmware.file_path)

    if not firmware_path.exists():
        raise RibbonUpgradeError(f"Firmware file not found: {firmware_path}")

    async with RibbonWebClient(device.ip_address, device.username, password) as client:

        # Step 1 — Login
        await _set_status(db, job, JobStatus.LOGIN)
        await _append_log(db, job, f"Connecting to {device.ip_address}…")
        await client.login()
        await _append_log(db, job, "Login successful")

        # Step 2 — Scrape form fields
        await _set_status(db, job, JobStatus.SCRAPING)
        await _append_log(db, job, "Fetching upgrade page metadata…")
        form_fields = await client.scrape_upgrade_form_fields()
        await _append_log(db, job, f"Collected {len(form_fields)} form fields")

        # Step 3 — Pre-flight validation
        await _set_status(db, job, JobStatus.VALIDATING)
        await _append_log(db, job, "Running pre-upgrade validation…")
        validation = await client.validate_upgrade()
        await _append_log(db, job, f"Validation response: {str(validation)[:200]}")

        # Step 4 — Config backup (save returned bytes to disk)
        await _set_status(db, job, JobStatus.BACKING_UP)
        await _append_log(db, job, "Triggering config backup…")
        backup_bytes = await client.backup_config(form_fields)
        if backup_bytes:
            backups_dir = Path(settings.backups_dir)
            backups_dir.mkdir(parents=True, exist_ok=True)
            safe_ip = device.ip_address.replace(".", "_")
            backup_filename = f"job_{job.id}_{safe_ip}_config_backup.tar"
            backup_path = backups_dir / backup_filename
            backup_path.write_bytes(backup_bytes)
            job.backup_path = str(backup_path)
            await db.commit()
            await _append_log(
                db, job,
                f"Config backup saved ({len(backup_bytes) / 1024:.0f} KB) — "
                f"available for download from this job"
            )
        else:
            await _append_log(db, job, "Config backup requested (no file returned by device)")

        # Step 5 — Create upload marker + upload firmware
        await _set_status(db, job, JobStatus.UPLOADING)
        await _append_log(db, job, "Creating upload slot on device…")
        await client.create_upload_marker()
        await _append_log(
            db, job,
            f"Uploading {firmware.filename} ({firmware.file_size / 1024 / 1024:.1f} MB)…"
        )

        # Stream firmware with live progress tracking — a background task persists the counter
        progress: dict = {"sent": 0, "total": firmware.file_size}
        job.upload_bytes_sent = 0
        await db.commit()

        async def _progress_updater() -> None:
            while True:
                await asyncio.sleep(2)
                job.upload_bytes_sent = progress["sent"]
                await db.commit()

        updater = asyncio.create_task(_progress_updater())
        try:
            await client.upload_firmware(firmware_path, form_fields, progress=progress)
        finally:
            updater.cancel()
            try:
                await updater
            except asyncio.CancelledError:
                pass
            job.upload_bytes_sent = progress["sent"]
            await db.commit()

        await _append_log(db, job, "Firmware uploaded — device is installing and will reboot")

        # Step 6 — Wait for installation to complete, then wait for reboot
        await _set_status(db, job, JobStatus.REBOOTING)
        await _append_log(db, job, "Waiting for device to complete firmware installation…")
        installed = await client.wait_for_install()
        if not installed:
            raise RibbonUpgradeError("Firmware installation did not complete within timeout")
        await _append_log(db, job, "Installation confirmed — device is rebooting…")
        came_back = await client.wait_for_online()
        if not came_back:
            raise RibbonUpgradeError("Device did not come back online within timeout after reboot")
        await _append_log(db, job, "Device is back online")

        # Step 7 — Verify version
        await _set_status(db, job, JobStatus.VERIFYING)
        await _append_log(db, job, "Re-logging in to verify firmware version…")
        await client.login()
        new_version = await client.get_version()
        if new_version:
            await _append_log(db, job, f"Firmware version now: {new_version}")
            device.current_version = new_version
        else:
            await _append_log(db, job, "Could not determine firmware version post-upgrade")

        device.last_checked_at = datetime.now(timezone.utc)

        # Step 8 — Done
        job.status = JobStatus.COMPLETE
        job.completed_at = datetime.now(timezone.utc)
        await _append_log(db, job, "Upgrade COMPLETE")
        await db.commit()
        await audit_log(db, "job.completed",
                        f"Upgrade job #{job.id} COMPLETED — {device.name} now running "
                        f"{new_version or firmware.version}",
                        "job", job.id,
                        {"device": device.name, "ip": device.ip_address,
                         "firmware": firmware.filename, "new_version": new_version})

    await _send_notification(db, job, device, firmware, "complete")


async def _send_notification(
    db: AsyncSession,
    job: UpgradeJob,
    device: Device,
    firmware: FirmwareFile,
    status: str,
) -> None:
    try:
        from app.services.notifications import send_upgrade_notification

        log_tail = "\n".join((job.log or "").splitlines()[-10:])
        await send_upgrade_notification(
            db,
            device_name=device.name,
            customer_name=device.customer.name if device.customer else "Unknown",
            status=status,
            firmware_version=firmware.version,
            log_tail=log_tail,
        )
    except Exception as e:
        logger.error(f"Failed to send notification for job {job.id}: {e}")
