from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import CertJob, CertificateFile, Device, JobStatus
from app.schemas import CertJobCreate, CertJobOut, MessageOut
from app.services.audit import audit_log
from app.services.scheduler import cancel_scheduled_cert_job, schedule_cert_job

router = APIRouter(prefix="/api/cert-jobs", tags=["cert-jobs"])


async def _get_or_404(db: AsyncSession, job_id: int) -> CertJob:
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
        raise HTTPException(status_code=404, detail="Cert job not found")
    return job


def _job_to_out(job: CertJob) -> CertJobOut:
    d = CertJobOut.model_validate(job)
    if job.device:
        d.device_name = job.device.name
        if job.device.customer:
            d.customer_name = job.device.customer.name
    if job.certificate:
        d.certificate_filename = job.certificate.filename
    return d


@router.get("", response_model=list[CertJobOut])
async def list_cert_jobs(
    device_id: int | None = None,
    status: JobStatus | None = None,
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(CertJob)
        .options(
            selectinload(CertJob.device).selectinload(Device.customer),
            selectinload(CertJob.certificate),
        )
        .order_by(CertJob.id.desc())
    )
    if device_id is not None:
        q = q.where(CertJob.device_id == device_id)
    if status is not None:
        q = q.where(CertJob.status == status)
    result = await db.execute(q)
    return [_job_to_out(j) for j in result.scalars()]


@router.post("", response_model=CertJobOut, status_code=201)
async def create_cert_job(
    payload: CertJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    dev_result = await db.execute(select(Device).where(Device.id == payload.device_id))
    device = dev_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    cert_result = await db.execute(
        select(CertificateFile).where(CertificateFile.id == payload.certificate_id)
    )
    certificate = cert_result.scalar_one_or_none()
    if not certificate:
        raise HTTPException(status_code=404, detail="Certificate not found")

    job = CertJob(
        device_id=payload.device_id,
        certificate_id=payload.certificate_id,
        status=JobStatus.PENDING,
        scheduled_at=payload.scheduled_at,
        triggered_by=payload.triggered_by or "web",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    is_scheduled = payload.scheduled_at and payload.scheduled_at > datetime.now(timezone.utc)
    if is_scheduled:
        aps_id = schedule_cert_job(job.id, payload.scheduled_at)
        job.apscheduler_job_id = aps_id
        await db.commit()
        await audit_log(
            db, "cert_job.created",
            f"Cert job #{job.id} scheduled for {payload.scheduled_at.strftime('%Y-%m-%d %H:%M UTC')}: "
            f"'{device.name}' ← {certificate.filename}",
            "cert_job", job.id,
            {"device": device.name, "certificate": certificate.filename,
             "scheduled_at": payload.scheduled_at.isoformat()},
        )
    else:
        background_tasks.add_task(_run_in_thread, job.id)
        await audit_log(
            db, "cert_job.created",
            f"Immediate cert job #{job.id} queued: '{device.name}' ← {certificate.filename}",
            "cert_job", job.id,
            {"device": device.name, "certificate": certificate.filename, "immediate": True},
        )

    result = await db.execute(
        select(CertJob)
        .where(CertJob.id == job.id)
        .options(
            selectinload(CertJob.device).selectinload(Device.customer),
            selectinload(CertJob.certificate),
        )
    )
    return _job_to_out(result.scalar_one())


def _run_in_thread(job_id: int) -> None:
    import asyncio
    from app.services.cert_job_service import _async_run_cert_job
    asyncio.run(_async_run_cert_job(job_id))


@router.get("/{job_id}", response_model=CertJobOut)
async def get_cert_job(job_id: int, db: AsyncSession = Depends(get_db)):
    return _job_to_out(await _get_or_404(db, job_id))


@router.post("/{job_id}/cancel", response_model=MessageOut)
async def cancel_cert_job(job_id: int, db: AsyncSession = Depends(get_db)):
    job = await _get_or_404(db, job_id)
    if job.status != JobStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a job with status '{job.status}'. Only pending jobs can be cancelled.",
        )
    device_name = job.device.name if job.device else f"device #{job.device_id}"
    if job.apscheduler_job_id:
        cancel_scheduled_cert_job(job.apscheduler_job_id)
    job.status = JobStatus.CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await audit_log(
        db, "cert_job.cancelled",
        f"Cert job #{job_id} cancelled (device: '{device_name}')",
        "cert_job", job_id, {"device": device_name},
    )
    return MessageOut(message="Cert job cancelled")
