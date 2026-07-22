"""预测数据访问: 从 HourlyStat 读取历史序列."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.models import HourlyStat


async def load_history(
    session: AsyncSession, metric: str, days: int = 7
) -> list[float]:
    """读取近 days 天的小时序列.

    metric: "vehicle" -> peak_vehicles, "person" -> peak_persons
    """
    since = datetime.utcnow() - timedelta(days=days)
    res = await session.execute(
        select(HourlyStat)
        .where(HourlyStat.stat_hour >= since)
        .order_by(HourlyStat.stat_hour)
    )
    rows = list(res.scalars())
    if metric == "vehicle":
        return [float(r.peak_vehicles) for r in rows]
    return [float(r.peak_persons) for r in rows]
