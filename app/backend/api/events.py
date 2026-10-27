"""事件接收 API: AI 推送 VehicleEnter/Exit, PersonEnter/Exit -> 实时状态 + 告警评估."""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from ...common.logger import logger
from ...schemas.events import EVENT_TYPES, EventIn
from ..core.alerts import evaluate
from ..core.realtime import apply_event, list_events

router = APIRouter(prefix="/api/events", tags=["events"])


@router.post("", status_code=201)
async def receive_event(
    event: EventIn,
    background: BackgroundTasks,
):
    """接收 AI 推送的业务事件."""
    if event.event_type not in EVENT_TYPES:
        raise HTTPException(400, f"invalid event_type: {event.event_type}")

    await apply_event(event.event_type, event.device_id, event.occurred_at)

    background.add_task(evaluate)

    logger.info(f"事件 {event.event_type} <- {event.device_id}")
    return {"status": "ok", "event_type": event.event_type}


@router.get("")
async def recent_events(limit: int = Query(100, ge=1, le=2000)):
    """读取最近 N 条越线事件历史 (Redis List, 保留最近 2000 条)."""
    return await list_events(limit)
