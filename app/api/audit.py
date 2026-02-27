import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AuditLog
from app.schemas import AuditLogOut

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=list[AuditLogOut])
async def list_audit(
    event_type: str | None = None,
    entity_type: str | None = None,
    limit: int = 500,
    db: AsyncSession = Depends(get_db),
):
    q = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    if event_type:
        q = q.where(AuditLog.event_type == event_type)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    result = await db.execute(q)
    return [AuditLogOut.model_validate(row) for row in result.scalars()]


@router.get("/export.csv")
async def export_csv(
    event_type: str | None = None,
    entity_type: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Download the full audit log as a CSV file."""
    q = select(AuditLog).order_by(AuditLog.timestamp.desc())
    if event_type:
        q = q.where(AuditLog.event_type == event_type)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    result = await db.execute(q)
    rows = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Timestamp (UTC)", "Event Type", "Description", "Entity Type", "Entity ID", "Detail"])
    for row in rows:
        ts = row.timestamp.strftime("%Y-%m-%d %H:%M:%S") if row.timestamp else ""
        writer.writerow([
            ts,
            row.event_type,
            row.description,
            row.entity_type or "",
            row.entity_id or "",
            row.detail or "",
        ])

    filename = f"audit_log_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
