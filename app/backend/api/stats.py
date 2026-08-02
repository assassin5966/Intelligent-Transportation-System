"""实时统计 API."""
from fastapi import APIRouter

from ...schemas.events import RealtimeStats
from ..core.realtime import get_stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/realtime", response_model=RealtimeStats)
async def realtime():
    """当前车辆/人员数量、今日累计、活跃设备数."""
    return RealtimeStats(**await get_stats())
