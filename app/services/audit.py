"""
Audit logging helper.

Call `audit_log(db, event_type, description, ...)` from any API handler or
service to append a row to the audit_logs table. The helper commits immediately
so the record is persisted even if the surrounding request fails later.
"""

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

logger = logging.getLogger(__name__)


async def audit_log(
    db: AsyncSession,
    event_type: str,
    description: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    detail: dict | None = None,
) -> None:
    """Insert an audit log row and commit."""
    try:
        row = AuditLog(
            event_type=event_type,
            description=description,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=json.dumps(detail) if detail else None,
        )
        db.add(row)
        await db.commit()
    except Exception as exc:
        logger.error(f"Failed to write audit log ({event_type}): {exc}")
