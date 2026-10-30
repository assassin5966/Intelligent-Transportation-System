"""实时位置 API — 为前端提供设备 GPS 位置查询与实时推送.

端点:
  GET /api/location/devices         列表(支持 ?deviceType= 过滤)
  GET /api/location/devices/{id}    单设备最新位置
  GET /api/location/stream          SSE 实时推送 (EventSource)

数据来源: core.gps_location 写入的 Redis 最新位置; 实时性由 Pulsar 消费保证.
坐标系: WGS84 (与规范一致).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...common.config import settings
from ..core.gps_location import (
    LocationPoint,
    _LOCATION_UPDATES_CHANNEL,
    get_all_locations,
    get_location,
)

router = APIRouter(prefix="/api/location", tags=["location"])


class LocationOut(BaseModel):
    """对外输出的位置点 (在规范字段基础上补充 online 状态)."""

    deviceId: str
    deviceType: Optional[str] = None
    longitude: float
    latitude: float
    speed: Optional[float] = None
    heading: Optional[float] = None
    altitude: Optional[float] = None
    positioningTime: Optional[str] = None
    extension: Optional[str] = None
    receivedAt: Optional[str] = None
    online: bool = False


def _to_out(p: LocationPoint) -> LocationOut:
    online = False
    if p.receivedAt:
        try:
            dt = datetime.fromisoformat(p.receivedAt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - dt) < timedelta(
                seconds=settings.location_online_timeout_seconds
            ):
                online = True
        except ValueError:
            online = False
    return LocationOut(**p.model_dump(), online=online)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/devices", response_model=list[LocationOut])
async def list_devices(deviceType: Optional[str] = None):
    """所有设备最新位置; deviceType 可过滤 (HANDHELD_RADIO/BODY_CAMERA/MOTORCYCLE_PATROL)."""
    points = await get_all_locations(device_type=deviceType)
    return [_to_out(p) for p in points]


@router.get("/devices/{device_id}", response_model=LocationOut)
async def get_device(device_id: str):
    """单个设备最新位置; 无数据返回 404."""
    p = await get_location(device_id)
    if p is None:
        raise HTTPException(status_code=404, detail="device location not found")
    return _to_out(p)


@router.get("/stream")
async def stream(request: Request):
    """SSE 实时推送位置更新 (前端用 EventSource 订阅).

    首帧发送 snapshot(全部当前位置), 其后每次 Pulsar 收到新位置即推送 update 事件.
    客户端断开或 ?deviceType= 过滤仅影响首帧快照; update 事件始终全量推送单点.
    """
    import redis.asyncio as aioredis

    device_type = request.query_params.get("deviceType")

    async def event_gen():
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(_LOCATION_UPDATES_CHANNEL)
        try:
            # 初始快照
            points = await get_all_locations(device_type=device_type)
            yield _sse("snapshot", [_to_out(p).model_dump() for p in points])
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    try:
                        p = LocationPoint(**json.loads(msg["data"]))
                        if device_type and p.deviceType != device_type:
                            continue
                        yield _sse("update", _to_out(p).model_dump())
                    except (json.JSONDecodeError, ValueError):
                        continue
        finally:
            await pubsub.unsubscribe(_LOCATION_UPDATES_CHANNEL)
            await pubsub.close()
            await r.close()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
