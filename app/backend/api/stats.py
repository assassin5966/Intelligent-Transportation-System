"""实时统计 API."""
from fastapi import APIRouter, Query

from ...schemas.events import RealtimeStats
from ..core.realtime import get_stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/realtime", response_model=RealtimeStats)
async def realtime():
    """当前车辆/人员数量、今日累计、活跃设备数."""
    return RealtimeStats(**await get_stats())


@router.get("/trend")
async def trend(hours: int = Query(24, ge=1, le=168)):
    """历史趋势 (逐小时车辆/人员进出总量, 来自 Redis 小时聚合)."""
    # 懒加载: prediction 模块不可用时仅本端点失败, 不影响后端启动
    from ...prediction.repository import load_history

    vehicle = await load_history("vehicle", hours)
    person = await load_history("person", hours)
    return {"hours": hours, "vehicle": vehicle, "person": person}
