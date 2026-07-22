"""小时统计聚合: 每小时把事件滚动写入 HourlyStat (趋势/预测的数据源)."""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select, func

from ...common.db import AsyncSessionLocal
from ...common.logger import logger
from ...common.models import Event, HourlyStat
from .realtime import get_stats

_task: asyncio.Task | None = None


def _hour_bucket(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


async def _aggregate_hour(hour: datetime) -> None:
    """聚合指定小时 (整点) 的事件计数, 并更新峰值."""
    start = hour
    end = hour + timedelta(hours=1)
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Event.event_type, func.count(Event.id))
            .where(Event.occurred_at >= start, Event.occurred_at < end)
            .group_by(Event.event_type)
        )
        counts = {et: c for et, c in res.all()}

        existing = await session.execute(
            select(HourlyStat).where(HourlyStat.stat_hour == start)
        )
        row = existing.scalars().first()
        if row is None:
            row = HourlyStat(stat_hour=start)
            session.add(row)

        row.vehicle_in = counts.get("VehicleEnter", 0)
        row.vehicle_out = counts.get("VehicleExit", 0)
        row.person_in = counts.get("PersonEnter", 0)
        row.person_out = counts.get("PersonExit", 0)

        # 峰值取当前实时存量与历史峰值的较大值
        stats = await get_stats()
        row.peak_vehicles = max(row.peak_vehicles, stats["current_vehicles"])
        row.peak_persons = max(row.peak_persons, stats["current_persons"])

        await session.commit()
        logger.info(
            f"聚合 {start:%Y-%m-%d %H:00}: "
            f"车辆 in={row.vehicle_in} out={row.vehicle_out}, "
            f"人员 in={row.person_in} out={row.person_out}"
        )


async def _loop() -> None:
    while True:
        try:
            # 聚合上一个完整小时
            await _aggregate_hour(_hour_bucket(datetime.utcnow() - timedelta(hours=1)))
        except Exception as e:  # noqa: BLE001
            logger.error(f"小时聚合失败: {e}")
        await asyncio.sleep(3600)


async def start_aggregator() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())
        logger.info("小时统计聚合器已启动 (每小时一次)")


async def stop_aggregator() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
