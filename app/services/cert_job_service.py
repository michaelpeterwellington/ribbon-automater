"""Certificate update job service."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models import CertJob, CertificateFile, Device, JobStatus
from app.services.audit import audit_log
from app.services.crypto import decrypt_value
from app.services.ribbon_client import RibbonWebClient

logger = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


async def _append_log(db: AsyncSession, job: CertJob, line: str) -> None:
    job.log = (job.log or "") + f"[{_ts()}] {line}\n"
    await db.commit()
    logger.info(f"[cert_job {job.id}] {line}")


async def _set_status(db: AsyncSession, job: CertJob, status: JobStatus) -> None:
    job.status = status
    if status == JobStatus.UPLOADING and not job.started_at:
        job.started_at = datetime.now(timezone.utc)
    await db.commit()


def run_cert_job(job_id: int) -> None:
    """Synchronous wrapper called by APScheduler."""
    asyncio.run(_async_run_cert_job(job_id))


async def _async_run_cert_job(job_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CertJob)
            .where(CertJob.id == job_id)
            .options(
                selectinload(CertJob.device).selectinload(Device.customer),
                selectinload(CertJob.certificate),
            )
        )
        job = result.scalar_one_or_none()
        if not job:
            logger.error(f"Cert job {job_id} not found")
            return

        if job.status == JobStatus.CANCELLED:
            logger.info(f"Cert job {job_id} was cancelled before it started")
            return

        device: Device = job.device
        certificate: CertificateFile = job.certificate

        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            await _run_cert_workflow(db, job, device, certificate)
        except Exception as e:
            await _set_status(db, job, JobStatus.FAILED)
            await _append_log(db, job, f"FATAL ERROR: {e}")
            await audit_log(
                db, "cert_job.failed",
                f"Cert job #{job.id} FAILED — {device.name} ({device.ip_address})",
                "cert_job", job.id,
                {"device": device.name, "ip": device.ip_address,
                 "certificate": certificate.filename, "error": str(e)[:500]},
            )


async def _run_cert_workflow(
    db: AsyncSession,
    job: CertJob,
    device: Device,
    certificate: CertificateFile,
) -> None:
    cert_path = Path(certificate.file_path)
    if not cert_path.exists():
        raise FileNotFoundError(f"Certificate file not found on disk: {cert_path}")

    cert_bytes = cert_path.read_bytes()
    password = decrypt_value(device.password_encrypted)

    async with RibbonWebClient(device.ip_address, device.username, password) as client:
        await _set_status(db, job, JobStatus.LOGIN)
        await _append_log(db, job, f"Connecting to {device.ip_address}…")
        await client.login()
        await _append_log(db, job, "Login successful")

        await _set_status(db, job, JobStatus.UPLOADING)
        await _append_log(db, job, f"Uploading certificate '{certificate.filename}'…")
        result = await client.upload_certificate(cert_bytes, certificate.filename)
        await _append_log(db, job, f"Certificate uploaded — device response: {result[:200] or '(empty)'}")

        job.status = JobStatus.COMPLETE
        job.completed_at = datetime.now(timezone.utc)
        await _append_log(db, job, "Certificate update COMPLETE")
        await db.commit()
        await audit_log(
            db, "cert_job.completed",
            f"Cert job #{job.id} COMPLETED — {device.name} certificate updated",
            "cert_job", job.id,
            {"device": device.name, "ip": device.ip_address,
             "certificate": certificate.filename},
        )
