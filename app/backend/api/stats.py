"""实时统计与趋势 API."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.db import get_session
from ...schemas.events import RealtimeStats, TrendPoint
from ..core.realtime import get_stats
from ..crud import get_trend

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/realtime", response_model=RealtimeStats)
async def realtime():
    """当前车辆/人员数量、今日累计、活跃设备数."""
    return RealtimeStats(**await get_stats())


@router.get("/trend", response_model=list[TrendPoint])
async def trend(
    hours: int = Query(24, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
):
    """历史趋势曲线 (按小时聚合)."""
    rows = await get_trend(session, hours=hours)
    return [
        TrendPoint(
            time=r.stat_hour,
            vehicles=r.peak_vehicles,
            persons=r.peak_persons,
        )
        for r in rows
    ]
