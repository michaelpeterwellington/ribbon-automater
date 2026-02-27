import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DeviceType(str, enum.Enum):
    SBC1K = "SBC1K"
    SBC2K = "SBC2K"
    SWE_EDGE = "SWE_EDGE"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    LOGIN = "login"
    SCRAPING = "scraping"
    VALIDATING = "validating"
    BACKING_UP = "backing_up"
    UPLOADING = "uploading"
    REBOOTING = "rebooting"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(255))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    devices: Mapped[list["Device"]] = relationship(
        "Device", back_populates="customer", cascade="all, delete-orphan"
    )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(255), nullable=False)
    device_type: Mapped[DeviceType] = mapped_column(Enum(DeviceType), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    current_version: Mapped[str | None] = mapped_column(String(100))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    customer: Mapped["Customer"] = relationship("Customer", back_populates="devices")
    upgrade_jobs: Mapped[list["UpgradeJob"]] = relationship(
        "UpgradeJob", back_populates="device", cascade="all, delete-orphan"
    )


class FirmwareFile(Base):
    __tablename__ = "firmware_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    compatible_types: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )  # JSON list
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    notes: Mapped[str | None] = mapped_column(Text)

    upgrade_jobs: Mapped[list["UpgradeJob"]] = relationship(
        "UpgradeJob", back_populates="firmware"
    )


class UpgradeJob(Base):
    __tablename__ = "upgrade_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    firmware_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("firmware_files.id"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), nullable=False, default=JobStatus.PENDING
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    log: Mapped[str] = mapped_column(Text, nullable=False, default="")
    triggered_by: Mapped[str | None] = mapped_column(String(255))
    apscheduler_job_id: Mapped[str | None] = mapped_column(String(255))
    backup_path: Mapped[str | None] = mapped_column(Text)
    upload_bytes_sent: Mapped[int | None] = mapped_column(Integer)

    device: Mapped["Device"] = relationship("Device", back_populates="upgrade_jobs")
    firmware: Mapped["FirmwareFile"] = relationship(
        "FirmwareFile", back_populates="upgrade_jobs"
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(50))   # customer / device / firmware / job
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)               # JSON blob of extra context


class EmailConfig(Base):
    __tablename__ = "email_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    from_address: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    to_address_default: Mapped[str] = mapped_column(
        String(255), nullable=False, default=""
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
