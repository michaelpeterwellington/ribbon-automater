from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import EmailConfig
from app.schemas import EmailConfigIn, EmailConfigOut, MessageOut
from app.services.crypto import decrypt_value, encrypt_value

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _get_config(db: AsyncSession) -> EmailConfig:
    result = await db.execute(select(EmailConfig).where(EmailConfig.id == 1))
    config = result.scalar_one_or_none()
    if not config:
        config = EmailConfig(id=1)
        db.add(config)
        await db.commit()
        await db.refresh(config)
    return config


@router.get("/email", response_model=EmailConfigOut)
async def get_email_config(db: AsyncSession = Depends(get_db)):
    return await _get_config(db)


@router.put("/email", response_model=EmailConfigOut)
async def save_email_config(payload: EmailConfigIn, db: AsyncSession = Depends(get_db)):
    config = await _get_config(db)
    config.smtp_host = payload.smtp_host
    config.smtp_port = payload.smtp_port
    config.use_tls = payload.use_tls
    config.username = payload.username
    config.password_encrypted = encrypt_value(payload.password) if payload.password else ""
    config.from_address = payload.from_address
    config.to_address_default = payload.to_address_default
    config.enabled = payload.enabled
    await db.commit()
    await db.refresh(config)
    return config


@router.post("/email/test", response_model=MessageOut)
async def test_email(db: AsyncSession = Depends(get_db)):
    from app.services.notifications import send_test_email
    success, message = await send_test_email(db)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return MessageOut(message=message)
