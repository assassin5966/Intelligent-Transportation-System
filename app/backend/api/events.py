"""事件接收 API: AI 推送 VehicleEnter/Exit, PersonEnter/Exit -> 实时状态 + 告警评估."""
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import Field

from ...common.logger import logger
from ...common.config import settings
from ...common.redis_client import get_redis
from ...schemas.events import EVENT_TYPES, EventIn
from ..core.alerts import evaluate
from ..core.realtime import EventIdConflict, apply_event, list_events
from ..core.event_ledger import LedgerConflict, event_ledger

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
    result = {"idempotency": "permanent-v1"}
    if settings.event_ledger_enabled and settings.mysql_enabled:
        result['ledger'] = 'journal-v2'
    return result


class EventV2In(EventIn):
    evidence: dict = Field(default_factory=dict)


@router.post('/v2', status_code=201)
async def receive_event_v2(event: EventV2In):
    """仅确认 MySQL 已接收；Redis 应用状态从账本状态单独查询。"""
    if not settings.event_ledger_enabled or not settings.mysql_enabled:
        raise HTTPException(503, 'event ledger is disabled')
    if (not event.event_id or event.event_id != event.event_id.strip() or
            not event.device_id or len(event.device_id) > 128 or event.event_type not in EVENT_TYPES):
        raise HTTPException(422, 'invalid event_id, device_id or event_type')
    if event.occurred_at.tzinfo is None or event.occurred_at.utcoffset() is None:
        raise HTTPException(422, 'v2 occurred_at must include a timezone offset')
    payload = {'event_id': event.event_id, 'device_id': event.device_id,
               'event_type': event.event_type, 'occurred_at': event.occurred_at.isoformat()}
    try:
        created = await event_ledger.persist(payload, event.evidence)
    except LedgerConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, 'invalid event evidence') from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(f'事件账本持久化失败: {type(exc).__name__}: {exc}')
        raise HTTPException(503, 'event ledger unavailable') from exc
    return {'event_id': event.event_id, 'persisted': True, 'created': created}


@router.get('/ledger-status')
async def ledger_status():
    if not settings.event_ledger_enabled:
        return {'enabled': False}
    return {'enabled': True, **await event_ledger.snapshot()}


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
