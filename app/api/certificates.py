import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import CertJob, CertificateFile, JobStatus
from app.schemas import CertificateOut, MessageOut
from app.services.audit import audit_log
from app.services.crypto import encrypt_value

router = APIRouter(prefix="/api/certificates", tags=["certificates"])


async def _get_or_404(db: AsyncSession, cert_id: int) -> CertificateFile:
    result = await db.execute(
        select(CertificateFile).where(CertificateFile.id == cert_id)
    )
    cert = result.scalar_one_or_none()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    return cert


def _parse_cert_info(file_bytes: bytes, filename: str, pfx_password: str | None = None) -> tuple[str | None, str | None]:
    """Return (subject_cn, not_valid_after) from a PEM or PFX/P12 cert."""
    try:
        lower = filename.lower()
        if lower.endswith(".pfx") or lower.endswith(".p12"):
            from cryptography.hazmat.primitives.serialization import pkcs12
            pwd = pfx_password.encode() if pfx_password else None
            p12 = pkcs12.load_pkcs12(file_bytes, pwd)
            cert = p12.cert.certificate if p12.cert else None
            if not cert:
                return None, None
        else:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            cert = x509.load_pem_x509_certificate(file_bytes, default_backend())

        from cryptography import x509 as _x509
        cn_attr = cert.subject.get_attributes_for_oid(_x509.NameOID.COMMON_NAME)
        subject_cn = cn_attr[0].value if cn_attr else None
        not_valid_after = cert.not_valid_after_utc.strftime("%b %d %H:%M:%S %Y GMT")
        return subject_cn, not_valid_after
    except Exception:
        return None, None


@router.get("", response_model=list[CertificateOut])
async def list_certificates(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(CertificateFile).order_by(CertificateFile.uploaded_at.desc())
    )
    return [CertificateOut.from_orm_cert(c) for c in result.scalars()]


@router.post("", response_model=CertificateOut, status_code=201)
async def upload_certificate(
    file: UploadFile = File(...),
    password: str = Form(None),
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    upload_dir = Path(settings.upload_dir) / "certificates"
    upload_dir.mkdir(parents=True, exist_ok=True)

    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    dest_path = upload_dir / unique_name

    sha256 = hashlib.sha256()
    total_size = 0
    chunks = []
    while chunk := await file.read(1024 * 1024):
        chunks.append(chunk)
        sha256.update(chunk)
        total_size += len(chunk)

    file_bytes = b"".join(chunks)
    with open(dest_path, "wb") as fh:
        fh.write(file_bytes)

    subject_cn, not_valid_after = _parse_cert_info(file_bytes, file.filename, password)

    lower = (file.filename or "").lower()
    is_pfx = lower.endswith(".pfx") or lower.endswith(".p12")
    pfx_password_encrypted = encrypt_value(password) if (is_pfx and password) else None

    cert = CertificateFile(
        filename=file.filename,
        subject_cn=subject_cn,
        not_valid_after=not_valid_after,
        pfx_password_encrypted=pfx_password_encrypted,
        file_path=str(dest_path),
        file_size=total_size,
        sha256=sha256.hexdigest(),
        notes=notes,
    )
    db.add(cert)
    await db.commit()
    await db.refresh(cert)
    await audit_log(
        db, "certificate.uploaded",
        f"Certificate '{file.filename}' uploaded"
        + (f" (CN: {subject_cn})" if subject_cn else ""),
        "certificate", cert.id,
        {"filename": file.filename, "subject_cn": subject_cn,
         "not_valid_after": not_valid_after, "size": total_size},
    )
    return CertificateOut.from_orm_cert(cert)


@router.delete("/{cert_id}", response_model=MessageOut)
async def delete_certificate(cert_id: int, db: AsyncSession = Depends(get_db)):
    cert = await _get_or_404(db, cert_id)

    active_statuses = [
        JobStatus.PENDING, JobStatus.LOGIN, JobStatus.UPLOADING,
    ]
    result = await db.execute(
        select(CertJob).where(
            CertJob.certificate_id == cert_id,
            CertJob.status.in_(active_statuses),
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="Cannot delete certificate referenced by an active cert job",
        )

    filename = cert.filename
    file_path = Path(cert.file_path)
    if file_path.exists():
        file_path.unlink()

    await db.delete(cert)
    await db.commit()
    await audit_log(
        db, "certificate.deleted",
        f"Certificate '{filename}' deleted",
        "certificate", cert_id, {"filename": filename},
    )
    return MessageOut(message="Certificate deleted")
