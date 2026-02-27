from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Customer, Device
from app.schemas import CustomerCreate, CustomerOut, CustomerUpdate, MessageOut

router = APIRouter(prefix="/api/customers", tags=["customers"])


async def _get_or_404(db: AsyncSession, customer_id: int) -> Customer:
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@router.get("", response_model=list[CustomerOut])
async def list_customers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Customer, func.count(Device.id).label("device_count"))
        .outerjoin(Device, Device.customer_id == Customer.id)
        .group_by(Customer.id)
        .order_by(Customer.name)
    )
    rows = result.all()
    out = []
    for customer, device_count in rows:
        d = CustomerOut.model_validate(customer)
        d.device_count = device_count
        out.append(d)
    return out


@router.post("", response_model=CustomerOut, status_code=201)
async def create_customer(payload: CustomerCreate, db: AsyncSession = Depends(get_db)):
    customer = Customer(**payload.model_dump())
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return CustomerOut.model_validate(customer)


@router.get("/{customer_id}", response_model=CustomerOut)
async def get_customer(customer_id: int, db: AsyncSession = Depends(get_db)):
    customer = await _get_or_404(db, customer_id)
    result = await db.execute(
        select(func.count(Device.id)).where(Device.customer_id == customer_id)
    )
    device_count = result.scalar_one()
    d = CustomerOut.model_validate(customer)
    d.device_count = device_count
    return d


@router.put("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: int, payload: CustomerUpdate, db: AsyncSession = Depends(get_db)
):
    customer = await _get_or_404(db, customer_id)
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(customer, key, value)
    await db.commit()
    await db.refresh(customer)
    return CustomerOut.model_validate(customer)


@router.delete("/{customer_id}", response_model=MessageOut)
async def delete_customer(customer_id: int, db: AsyncSession = Depends(get_db)):
    customer = await _get_or_404(db, customer_id)
    await db.delete(customer)
    await db.commit()
    return MessageOut(message="Customer deleted")
