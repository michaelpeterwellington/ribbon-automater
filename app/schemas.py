from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr

from app.models import DeviceType, JobStatus


# ── Customers ──────────────────────────────────────────────────────────────

class CustomerCreate(BaseModel):
    name: str
    contact_name: str | None = None
    contact_email: str | None = None
    notes: str | None = None


class CustomerUpdate(CustomerCreate):
    pass


class CustomerOut(BaseModel):
    id: int
    name: str
    contact_name: str | None
    contact_email: str | None
    notes: str | None
    created_at: datetime
    device_count: int = 0

    model_config = {"from_attributes": True}


# ── Devices ────────────────────────────────────────────────────────────────

class DeviceCreate(BaseModel):
    customer_id: int
    name: str
    ip_address: str
    device_type: DeviceType
    username: str
    password: str
    notes: str | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    ip_address: str | None = None
    device_type: DeviceType | None = None
    username: str | None = None
    password: str | None = None
    notes: str | None = None


class DeviceOut(BaseModel):
    id: int
    customer_id: int
    customer_name: str | None = None
    name: str
    ip_address: str
    device_type: DeviceType
    username: str
    current_version: str | None
    last_checked_at: datetime | None
    notes: str | None

    model_config = {"from_attributes": True}


# ── Firmware ───────────────────────────────────────────────────────────────

class FirmwareOut(BaseModel):
    id: int
    filename: str
    version: str
    compatible_types: list[str]
    file_size: int
    sha256: str
    uploaded_at: datetime
    notes: str | None

    model_config = {"from_attributes": True}


# ── Upgrade Jobs ───────────────────────────────────────────────────────────

class UpgradeJobCreate(BaseModel):
    device_id: int
    firmware_id: int
    scheduled_at: datetime | None = None  # None = run immediately
    triggered_by: str | None = "web"


class UpgradeJobOut(BaseModel):
    id: int
    device_id: int
    device_name: str | None = None
    customer_name: str | None = None
    firmware_id: int
    firmware_filename: str | None = None
    firmware_file_size: int | None = None
    status: JobStatus
    scheduled_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    log: str
    triggered_by: str | None
    upload_bytes_sent: int | None = None
    has_backup: bool = False

    model_config = {"from_attributes": True}


# ── Email Config ───────────────────────────────────────────────────────────

class EmailConfigIn(BaseModel):
    smtp_host: str
    smtp_port: int = 587
    use_tls: bool = True
    username: str
    password: str
    from_address: str
    to_address_default: str
    enabled: bool = True


class EmailConfigOut(BaseModel):
    smtp_host: str
    smtp_port: int
    use_tls: bool
    username: str
    from_address: str
    to_address_default: str
    enabled: bool

    model_config = {"from_attributes": True}


# ── Generic ────────────────────────────────────────────────────────────────

class MessageOut(BaseModel):
    message: str
    detail: Any = None
