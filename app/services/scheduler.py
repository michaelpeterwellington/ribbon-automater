"""APScheduler singleton for scheduling upgrade jobs."""

import logging
from datetime import datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from app.config import settings

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        jobstores = {
            "default": SQLAlchemyJobStore(url=f"sqlite:///{settings.db_path}")
        }
        _scheduler = BackgroundScheduler(jobstores=jobstores)
    return _scheduler


def start_scheduler() -> None:
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started")


def stop_scheduler() -> None:
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")


def schedule_upgrade(job_id: int, run_at: datetime) -> str:
    """
    Schedule an upgrade job to run at a specific datetime.
    Returns the APScheduler job ID.
    """
    # Import here to avoid circular imports
    from app.services.upgrade_service import run_upgrade_job

    scheduler = get_scheduler()
    aps_job = scheduler.add_job(
        run_upgrade_job,
        trigger=DateTrigger(run_date=run_at),
        args=[job_id],
        id=f"upgrade_{job_id}",
        replace_existing=True,
        misfire_grace_time=300,  # Run up to 5 min late if missed
    )
    logger.info(f"Scheduled upgrade job {job_id} for {run_at}")
    return aps_job.id


def cancel_scheduled_upgrade(aps_job_id: str) -> bool:
    """Remove a scheduled job. Returns True if removed."""
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(aps_job_id)
        logger.info(f"Cancelled scheduled job {aps_job_id}")
        return True
    except Exception:
        return False


def schedule_cert_job(job_id: int, run_at: datetime) -> str:
    """Schedule a cert job to run at a specific datetime. Returns the APScheduler job ID."""
    from app.services.cert_job_service import run_cert_job

    scheduler = get_scheduler()
    aps_job = scheduler.add_job(
        run_cert_job,
        trigger=DateTrigger(run_date=run_at),
        args=[job_id],
        id=f"cert_{job_id}",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info(f"Scheduled cert job {job_id} for {run_at}")
    return aps_job.id


def cancel_scheduled_cert_job(aps_job_id: str) -> bool:
    """Remove a scheduled cert job. Returns True if removed."""
    return cancel_scheduled_upgrade(aps_job_id)
