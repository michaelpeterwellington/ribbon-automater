import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Device, FirmwareFile, JobStatus, UpgradeJob
from app.schemas import MessageOut, UpgradeJobCreate, UpgradeJobOut
from app.services.scheduler import cancel_scheduled_upgrade, schedule_upgrade

router = APIRouter(prefix="/api/upgrades", tags=["upgrades"])


async def _get_or_404(db: AsyncSession, job_id: int) -> UpgradeJob:
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
        raise HTTPException(status_code=404, detail="Upgrade job not found")
    return job


def _job_to_out(job: UpgradeJob) -> UpgradeJobOut:
    d = UpgradeJobOut.model_validate(job)
    if job.device:
        d.device_name = job.device.name
        if job.device.customer:
            d.customer_name = job.device.customer.name
    if job.firmware:
        d.firmware_filename = job.firmware.filename
    return d


@router.get("", response_model=list[UpgradeJobOut])
async def list_jobs(
    customer_id: int | None = None,
    device_id: int | None = None,
    status: JobStatus | None = None,
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(UpgradeJob)
        .options(
            selectinload(UpgradeJob.device).selectinload(Device.customer),
            selectinload(UpgradeJob.firmware),
        )
        .order_by(UpgradeJob.id.desc())
    )
    if device_id is not None:
        q = q.where(UpgradeJob.device_id == device_id)
    if status is not None:
        q = q.where(UpgradeJob.status == status)
    if customer_id is not None:
        q = q.join(UpgradeJob.device).where(Device.customer_id == customer_id)
    result = await db.execute(q)
    return [_job_to_out(j) for j in result.scalars()]


@router.post("", response_model=UpgradeJobOut, status_code=201)
async def create_job(
    payload: UpgradeJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    # Verify device and firmware exist
    device = await db.execute(select(Device).where(Device.id == payload.device_id))
    if not device.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Device not found")

    fw = await db.execute(select(FirmwareFile).where(FirmwareFile.id == payload.firmware_id))
    if not fw.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Firmware not found")

    job = UpgradeJob(
        device_id=payload.device_id,
        firmware_id=payload.firmware_id,
        status=JobStatus.PENDING,
        scheduled_at=payload.scheduled_at,
        triggered_by=payload.triggered_by or "web",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    if payload.scheduled_at and payload.scheduled_at > datetime.now(timezone.utc):
        # Schedule for future
        aps_id = schedule_upgrade(job.id, payload.scheduled_at)
        job.apscheduler_job_id = aps_id
        await db.commit()
    else:
        # Run immediately in background
        from app.services.upgrade_service import run_upgrade_job
        background_tasks.add_task(_run_in_thread, job.id)

    result = await db.execute(
        select(UpgradeJob)
        .where(UpgradeJob.id == job.id)
        .options(
            selectinload(UpgradeJob.device).selectinload(Device.customer),
            selectinload(UpgradeJob.firmware),
        )
    )
    return _job_to_out(result.scalar_one())


def _run_in_thread(job_id: int) -> None:
    """Run upgrade in a thread pool (FastAPI BackgroundTasks runs in a thread)."""
    import asyncio
    from app.services.upgrade_service import _async_run_upgrade_job
    asyncio.run(_async_run_upgrade_job(job_id))


@router.get("/{job_id}", response_model=UpgradeJobOut)
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)):
    return _job_to_out(await _get_or_404(db, job_id))


@router.post("/{job_id}/cancel", response_model=MessageOut)
async def cancel_job(job_id: int, db: AsyncSession = Depends(get_db)):
    job = await _get_or_404(db, job_id)
    cancellable = {JobStatus.PENDING}
    if job.status not in cancellable:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a job with status '{job.status}'. Only pending jobs can be cancelled.",
        )
    if job.apscheduler_job_id:
        cancel_scheduled_upgrade(job.apscheduler_job_id)
    job.status = JobStatus.CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    await db.commit()
    return MessageOut(message="Job cancelled")
