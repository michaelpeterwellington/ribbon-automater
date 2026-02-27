"""Email notifications for upgrade job completion/failure."""

import logging
from email.mime.text import MIMEText

import aiosmtplib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmailConfig

logger = logging.getLogger(__name__)


async def _get_config(db: AsyncSession) -> EmailConfig | None:
    result = await db.execute(select(EmailConfig).where(EmailConfig.id == 1))
    return result.scalar_one_or_none()


async def send_upgrade_notification(
    db: AsyncSession,
    *,
    device_name: str,
    customer_name: str,
    status: str,
    firmware_version: str,
    log_tail: str = "",
    to_override: str | None = None,
) -> None:
    """Send an email when an upgrade job completes or fails."""
    config = await _get_config(db)
    if not config or not config.enabled or not config.smtp_host:
        return

    from app.services.crypto import decrypt_value

    to_address = to_override or config.to_address_default
    if not to_address:
        return

    subject = f"[Ribbon Upgrade] {device_name} ({customer_name}) — {status.upper()}"
    body_lines = [
        f"Device:   {device_name}",
        f"Customer: {customer_name}",
        f"Status:   {status.upper()}",
        f"Firmware: {firmware_version}",
    ]
    if log_tail:
        body_lines += ["", "--- Last log lines ---", log_tail]

    msg = MIMEText("\n".join(body_lines))
    msg["Subject"] = subject
    msg["From"] = config.from_address
    msg["To"] = to_address

    try:
        smtp_password = decrypt_value(config.password_encrypted) if config.password_encrypted else ""
        await aiosmtplib.send(
            msg,
            hostname=config.smtp_host,
            port=config.smtp_port,
            start_tls=config.use_tls,
            username=config.username or None,
            password=smtp_password or None,
        )
        logger.info(f"Upgrade notification sent to {to_address}")
    except Exception as e:
        logger.error(f"Failed to send upgrade notification: {e}")


async def send_test_email(db: AsyncSession) -> tuple[bool, str]:
    """Send a test email using current config. Returns (success, message)."""
    config = await _get_config(db)
    if not config or not config.smtp_host:
        return False, "Email not configured"

    from app.services.crypto import decrypt_value

    msg = MIMEText("This is a test email from the Ribbon SBC Upgrade Automation platform.")
    msg["Subject"] = "[Ribbon Upgrade] Test Email"
    msg["From"] = config.from_address
    msg["To"] = config.to_address_default

    try:
        smtp_password = decrypt_value(config.password_encrypted) if config.password_encrypted else ""
        await aiosmtplib.send(
            msg,
            hostname=config.smtp_host,
            port=config.smtp_port,
            start_tls=config.use_tls,
            username=config.username or None,
            password=smtp_password or None,
        )
        return True, f"Test email sent to {config.to_address_default}"
    except Exception as e:
        return False, f"Failed: {e}"
