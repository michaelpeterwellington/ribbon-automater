import asyncio
import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Customer, Device, DeviceType
from app.schemas import DeviceCreate, DeviceOut, DeviceUpdate, MessageOut
from app.services.audit import audit_log
from app.services.crypto import decrypt_value, encrypt_value
from app.services.ribbon_client import RibbonWebClient

router = APIRouter(prefix="/api/devices", tags=["devices"])


async def _get_or_404(db: AsyncSession, device_id: int) -> Device:
    result = await db.execute(
        select(Device).where(Device.id == device_id).options(selectinload(Device.customer))
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def _to_out(device: Device) -> DeviceOut:
    d = DeviceOut.model_validate(device)
    if device.customer:
        d.customer_name = device.customer.name
    return d


_TYPE_ALIASES: dict[str, str] = {
    "swe_edge": "SWE_EDGE", "swe edge": "SWE_EDGE", "sweedge": "SWE_EDGE", "swe": "SWE_EDGE",
    "sbc1k": "SBC1K", "sbc 1000": "SBC1K", "sbc1000": "SBC1K", "1000": "SBC1K",
    "sbc2k": "SBC2K", "sbc 2000": "SBC2K", "sbc2000": "SBC2K", "2000": "SBC2K",
}


def _normalise_headers(row: dict) -> dict:
    """Return a copy of row with keys lowercased and spaces replaced with underscores."""
    return {k.strip().lower().replace(" ", "_"): v for k, v in row.items()}


@router.post("/bulk-import")
async def bulk_import_devices(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    customers_created: list[str] = []
    devices_created: list[str] = []
    errors: list[dict] = []

    # Pre-load existing customers into cache keyed by lowercase name
    customer_cache: dict[str, Customer] = {}
    result = await db.execute(select(Customer))
    for c in result.scalars():
        customer_cache[c.name.lower()] = c

    for row_num, raw_row in enumerate(reader, start=2):
        row = _normalise_headers(raw_row)
        try:
            customer_name = row.get("customer", "").strip()
            device_name = (row.get("name") or row.get("device", "")).strip()
            ip_address = (row.get("ip_address") or row.get("ip", "")).strip()
            raw_type = (row.get("type") or row.get("device_type", "")).strip()
            username = row.get("username", "").strip() or "admin"
            password = row.get("password", "").strip()

            missing = [
                f for f, v in [
                    ("customer", customer_name), ("name", device_name),
                    ("ip_address", ip_address), ("type", raw_type),
                ]
                if not v
            ]
            if missing:
                errors.append({"row": row_num, "error": f"Missing required column(s): {', '.join(missing)}"})
                continue

            device_type_key = _TYPE_ALIASES.get(raw_type.lower())
            if not device_type_key:
                errors.append({
                    "row": row_num,
                    "error": f"Unknown device type '{raw_type}'. Use SWE_EDGE, SBC1K, or SBC2K",
                })
                continue

            ckey = customer_name.lower()
            if ckey not in customer_cache:
                customer = Customer(name=customer_name)
                db.add(customer)
                await db.flush()
                customer_cache[ckey] = customer
                customers_created.append(customer_name)
            else:
                customer = customer_cache[ckey]

            device = Device(
                customer_id=customer.id,
                name=device_name,
                ip_address=ip_address,
                device_type=DeviceType(device_type_key),
                username=username,
                password_encrypted=encrypt_value(password),
            )
            db.add(device)
            devices_created.append(device_name)

        except Exception as exc:
            errors.append({"row": row_num, "error": str(exc)})

    if devices_created or customers_created:
        await db.commit()
        await audit_log(
            db, "device.bulk_imported",
            f"Bulk import: {len(devices_created)} device(s) added, "
            f"{len(customers_created)} customer(s) created"
            + (f", {len(errors)} error(s)" if errors else ""),
            "device", None,
            {"devices": devices_created, "customers_created": customers_created,
             "error_count": len(errors)},
        )

    return {
        "customers_created": customers_created,
        "devices_created": devices_created,
        "errors": errors,
    }


@router.get("", response_model=list[DeviceOut])
async def list_devices(
    customer_id: int | None = None, db: AsyncSession = Depends(get_db)
):
    q = (
        select(Device)
        .options(selectinload(Device.customer))
        .order_by(Device.name)
    )
    if customer_id is not None:
        q = q.where(Device.customer_id == customer_id)
    result = await db.execute(q)
    return [_to_out(d) for d in result.scalars()]


@router.post("", response_model=DeviceOut, status_code=201)
async def create_device(payload: DeviceCreate, db: AsyncSession = Depends(get_db)):
    # Verify customer exists
    cust_result = await db.execute(select(Customer).where(Customer.id == payload.customer_id))
    customer = cust_result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    data = payload.model_dump()
    password = data.pop("password")
    device = Device(**data, password_encrypted=encrypt_value(password))
    db.add(device)
    await db.commit()
    await db.refresh(device)

    result = await db.execute(
        select(Device).where(Device.id == device.id).options(selectinload(Device.customer))
    )
    device = result.scalar_one()
    await audit_log(db, "device.created",
                    f"Device '{device.name}' ({device.ip_address}) added to customer '{customer.name}'",
                    "device", device.id,
                    {"name": device.name, "ip": device.ip_address, "type": device.device_type,
                     "customer": customer.name})
    return _to_out(device)


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(device_id: int, db: AsyncSession = Depends(get_db)):
    return _to_out(await _get_or_404(db, device_id))


@router.put("/{device_id}", response_model=DeviceOut)
async def update_device(
    device_id: int, payload: DeviceUpdate, db: AsyncSession = Depends(get_db)
):
    device = await _get_or_404(db, device_id)
    data = payload.model_dump(exclude_none=True)
    if "password" in data:
        device.password_encrypted = encrypt_value(data.pop("password"))
    for key, value in data.items():
        setattr(device, key, value)
    await db.commit()
    await db.refresh(device)

    result = await db.execute(
        select(Device).where(Device.id == device_id).options(selectinload(Device.customer))
    )
    device = result.scalar_one()
    await audit_log(db, "device.updated", f"Device '{device.name}' updated",
                    "device", device_id, {"name": device.name, "ip": device.ip_address})
    return _to_out(device)


@router.delete("/{device_id}", response_model=MessageOut)
async def delete_device(device_id: int, db: AsyncSession = Depends(get_db)):
    device = await _get_or_404(db, device_id)
    name, ip = device.name, device.ip_address
    customer_name = device.customer.name if device.customer else None
    await db.delete(device)
    await db.commit()
    await audit_log(db, "device.deleted",
                    f"Device '{name}' ({ip}) deleted" + (f" from customer '{customer_name}'" if customer_name else ""),
                    "device", device_id, {"name": name, "ip": ip, "customer": customer_name})
    return MessageOut(message="Device deleted")


@router.post("/{device_id}/test-connection")
async def test_connection(device_id: int, db: AsyncSession = Depends(get_db)):
    device = await _get_or_404(db, device_id)
    password = decrypt_value(device.password_encrypted)
    async with RibbonWebClient(device.ip_address, device.username, password) as client:
        success, message = await client.test_connection()
    return {"success": success, "message": message}


@router.post("/{device_id}/check-version")
async def check_version(device_id: int, db: AsyncSession = Depends(get_db)):
    device = await _get_or_404(db, device_id)
    password = decrypt_value(device.password_encrypted)
    async with RibbonWebClient(device.ip_address, device.username, password) as client:
        try:
            await client.login()
            version = await client.get_version()
            device.current_version = version
            device.last_checked_at = datetime.now(timezone.utc)
            await db.commit()
            return {"version": version, "updated": True}
        except Exception as e:
            return {"version": None, "updated": False, "error": str(e)}
