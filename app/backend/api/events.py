"""事件接收 API: AI 推送 VehicleEnter/Exit, PersonEnter/Exit -> 落库 + 实时状态 + 告警评估."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.db import get_session
from ...common.logger import logger
from ...schemas.events import EVENT_TYPES, EventIn
from ..core.alerts import evaluate
from ..core.realtime import apply_event
from ..crud import create_event

router = APIRouter(prefix="/api/events", tags=["events"])


@router.post("", status_code=201)
async def receive_event(
    event: EventIn,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """接收 AI 推送的业务事件."""
    if event.event_type not in EVENT_TYPES:
        raise HTTPException(400, f"invalid event_type: {event.event_type}")

    # 1. 历史落库 (MySQL)
    await create_event(
        session,
        device_id=event.device_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
    )
    # 2. 更新 Redis 实时状态
    await apply_event(event.event_type, event.device_id)
    # 3. 异步评估告警规则
    background.add_task(evaluate)

    logger.info(f"事件 {event.event_type} <- {event.device_id}")
    return {"status": "ok", "event_type": event.event_type}
