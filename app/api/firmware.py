import hashlib
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import FirmwareFile, JobStatus, UpgradeJob
from app.schemas import FirmwareOut, MessageOut

router = APIRouter(prefix="/api/firmware", tags=["firmware"])


async def _get_or_404(db: AsyncSession, firmware_id: int) -> FirmwareFile:
    result = await db.execute(
        select(FirmwareFile).where(FirmwareFile.id == firmware_id)
    )
    fw = result.scalar_one_or_none()
    if not fw:
        raise HTTPException(status_code=404, detail="Firmware file not found")
    return fw


def _firmware_to_out(fw: FirmwareFile) -> FirmwareOut:
    try:
        compatible_types = json.loads(fw.compatible_types)
    except Exception:
        compatible_types = []
    return FirmwareOut(
        id=fw.id,
        filename=fw.filename,
        version=fw.version,
        compatible_types=compatible_types,
        file_size=fw.file_size,
        sha256=fw.sha256,
        uploaded_at=fw.uploaded_at,
        notes=fw.notes,
    )


@router.get("", response_model=list[FirmwareOut])
async def list_firmware(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(FirmwareFile).order_by(FirmwareFile.uploaded_at.desc())
    )
    return [_firmware_to_out(fw) for fw in result.scalars()]


@router.post("", response_model=FirmwareOut, status_code=201)
async def upload_firmware(
    file: UploadFile = File(...),
    version: str = Form(...),
    compatible_types: str = Form("[]"),  # JSON array string e.g. '["SBC1K","SBC2K"]'
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Generate a unique stored filename to avoid collisions
    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    dest_path = upload_dir / unique_name

    sha256 = hashlib.sha256()
    total_size = 0
    with open(dest_path, "wb") as fh:
        while chunk := await file.read(1024 * 1024):  # 1 MB chunks
            fh.write(chunk)
            sha256.update(chunk)
            total_size += len(chunk)

    # Validate compatible_types JSON
    try:
        types_list = json.loads(compatible_types)
        if not isinstance(types_list, list):
            types_list = []
    except Exception:
        types_list = []

    fw = FirmwareFile(
        filename=file.filename,
        version=version,
        compatible_types=json.dumps(types_list),
        file_path=str(dest_path),
        file_size=total_size,
        sha256=sha256.hexdigest(),
        notes=notes,
    )
    db.add(fw)
    await db.commit()
    await db.refresh(fw)
    return _firmware_to_out(fw)


@router.delete("/{firmware_id}", response_model=MessageOut)
async def delete_firmware(firmware_id: int, db: AsyncSession = Depends(get_db)):
    fw = await _get_or_404(db, firmware_id)

    # Prevent deletion if referenced by an active/pending job
    active_statuses = [
        JobStatus.PENDING, JobStatus.LOGIN, JobStatus.SCRAPING,
        JobStatus.VALIDATING, JobStatus.BACKING_UP, JobStatus.UPLOADING,
        JobStatus.REBOOTING, JobStatus.VERIFYING,
    ]
    result = await db.execute(
        select(UpgradeJob).where(
            UpgradeJob.firmware_id == firmware_id,
            UpgradeJob.status.in_(active_statuses),
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="Cannot delete firmware referenced by an active upgrade job",
        )

    # Remove file from disk
    file_path = Path(fw.file_path)
    if file_path.exists():
        file_path.unlink()

    await db.delete(fw)
    await db.commit()
    return MessageOut(message="Firmware deleted")
