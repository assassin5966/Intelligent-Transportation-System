"""事件接收 API: AI 推送 VehicleEnter/Exit, PersonEnter/Exit -> 实时状态 + 告警评估."""
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from ...common.logger import logger
from ...common.config import settings
from ...common.redis_client import get_redis
from ...schemas.events import EVENT_TYPES, EventIn
from ..core.alerts import evaluate
from ..core.realtime import EventIdConflict, apply_event, list_events

router = APIRouter(prefix="/api/events", tags=["events"])


@router.post("", status_code=201)
async def receive_event(
    event: EventIn,
    background: BackgroundTasks,
):
    """接收 AI 推送的业务事件."""
    if event.event_type not in EVENT_TYPES:
        raise HTTPException(400, f"invalid event_type: {event.event_type}")

    try:
        applied = await apply_event(event.event_type, event.device_id, event.occurred_at, event.event_id)
    except EventIdConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    if applied:
        background.add_task(evaluate)
        now = datetime.now(event.occurred_at.tzinfo)
        event_hour = event.occurred_at.replace(minute=0, second=0, microsecond=0)
        if event_hour < now.replace(minute=0, second=0, microsecond=0):
            logger.warning(
                f"延迟越线事件 {event.event_id} ({event.device_id}) 落入已结束小时；"
                "已进入重归档或隔离流程，请检查归档状态"
            )

    logger.info(f"事件 {event.event_type} <- {event.device_id}")
    return {"status": "ok", "event_type": event.event_type,
            "event_id": event.event_id, "applied": applied}


@router.get("/capabilities")
async def event_capabilities():
    """重试客户端必须先确认后端支持原子幂等。"""
    return {"idempotency": "permanent-v1"}


@router.get("/archive-status")
async def archive_status():
    """持久隔离与等待重归档的数量，供运维监控。"""
    redis = get_redis()
    prefix = settings.redis_prefix
    pipe = redis.pipeline(transaction=False)
    pipe.hlen(f"{prefix}:archive:dirty")
    pipe.hlen(f"{prefix}:archive:quarantine")
    pipe.hlen(f"{prefix}:archive:quarantine_events")
    dirty, quarantined_hours, quarantined_events = await pipe.execute()
    return {"dirty_hours": dirty, "quarantined_hours": quarantined_hours,
            "quarantined_events": quarantined_events}


@router.get("")
async def recent_events(limit: int = Query(100, ge=1, le=2000)):
    """读取最近 N 条越线事件历史 (Redis List, 保留最近 2000 条)."""
    return await list_events(limit)
