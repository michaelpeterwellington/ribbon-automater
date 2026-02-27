import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Customer, Device
from app.schemas import DeviceCreate, DeviceOut, DeviceUpdate, MessageOut
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
    cust = await db.execute(select(Customer).where(Customer.id == payload.customer_id))
    if not cust.scalar_one_or_none():
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
    return _to_out(result.scalar_one())


@router.delete("/{device_id}", response_model=MessageOut)
async def delete_device(device_id: int, db: AsyncSession = Depends(get_db)):
    device = await _get_or_404(db, device_id)
    await db.delete(device)
    await db.commit()
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
