"""数据库 CRUD 操作."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.models import Alert, Device, Event, HourlyStat


async def create_event(
    session: AsyncSession,
    *,
    device_id: str,
    event_type: str,
    occurred_at: datetime,
) -> Event:
    ev = Event(device_id=device_id, event_type=event_type, occurred_at=occurred_at)
    session.add(ev)
    await session.commit()
    await session.refresh(ev)
    return ev


async def list_alerts(session: AsyncSession, limit: int = 100) -> list[Alert]:
    res = await session.execute(
        select(Alert).order_by(Alert.created_at.desc()).limit(limit)
    )
    return list(res.scalars())


async def list_devices(session: AsyncSession) -> list[Device]:
    res = await session.execute(select(Device).order_by(Device.id))
    return list(res.scalars())


async def upsert_device(session: AsyncSession, device: Device) -> Device:
    merged = await session.merge(device)
    await session.commit()
    await session.refresh(merged)
    return merged


async def delete_device(session: AsyncSession, device_id: str) -> bool:
    obj = await session.get(Device, device_id)
    if obj is None:
        return False
    await session.delete(obj)
    await session.commit()
    return True


async def get_trend(session: AsyncSession, hours: int = 24) -> list[HourlyStat]:
    since = datetime.utcnow() - timedelta(hours=hours)
    res = await session.execute(
        select(HourlyStat)
        .where(HourlyStat.stat_hour >= since)
        .order_by(HourlyStat.stat_hour)
    )
    return list(res.scalars())
