"""设备管理 API (注册/列表/删除, 含越线计数线配置).

注册/删除时转发到 AI 分析服务启停视频处理管道, 同时在 Redis 保存配置.
AI 服务不可达时仅告警, 不阻塞配置落库.
"""
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis

router = APIRouter(prefix="/api/devices", tags=["devices"])

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"


class DeviceIn(BaseModel):
    id: str
    name: str
    stream_url: str
    line_coords: Optional[str] = None  # "x1,y1,x2,y2" 归一化 0-1


class DeviceOut(BaseModel):
    id: str
    name: str
    stream_url: str
    line_coords: Optional[str] = None
    status: str = "registered"


def _line_from_coords(line_coords: Optional[str]) -> list[list[float]]:
    """解析 line_coords -> [[x1,y1],[x2,y2]]; 无法解析时返回默认外线."""
    if line_coords:
        try:
            parts = [float(x) for x in line_coords.split(",")]
            if len(parts) == 4:
                return [[parts[0], parts[1]], [parts[2], parts[3]]]
        except ValueError:
            pass
    return [[0.1, 0.4], [0.9, 0.4]]


async def _forward_to_ai(method: str, path: str, json_body: Optional[dict] = None) -> None:
    """转发到 AI 分析服务; 失败仅告警, 不阻塞设备配置落库."""
    url = f"{settings.ai_service_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if method == "POST":
                resp = await client.post(url, json=json_body)
            else:
                resp = await client.request(method, url)
            if resp.status_code >= 400:
                logger.warning(f"AI 服务转发失败 {method} {url}: HTTP {resp.status_code}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AI 服务不可达 {method} {url}: {e}")


@router.get("")
async def list_():
    redis = get_redis()
    found = []
    async for key in redis.scan_iter(f"{_DEVICE_KEY_PREFIX}*"):
        data = await redis.hgetall(key)
        if data:
            found.append(DeviceOut(**data))
    return found


@router.post("", status_code=201)
async def register(dev: DeviceIn):
    redis = get_redis()
    key = f"{_DEVICE_KEY_PREFIX}{dev.id}"
    await redis.hset(
        key,
        mapping={
            "id": dev.id,
            "name": dev.name,
            "stream_url": dev.stream_url,
            "line_coords": dev.line_coords or "",
            "status": "registered",
        },
    )
    line = _line_from_coords(dev.line_coords)
    await _forward_to_ai(
        "POST",
        "/devices",
        {"device_id": dev.id, "stream_url": dev.stream_url, "line": line},
    )
    return {"id": dev.id, "status": "registered"}


@router.delete("/{device_id}")
async def remove(device_id: str):
    redis = get_redis()
    key = f"{_DEVICE_KEY_PREFIX}{device_id}"
    exists = await redis.exists(key)
    if not exists:
        raise HTTPException(404, "device not found")
    await redis.delete(key)
    await _forward_to_ai("DELETE", f"/devices/{device_id}")
    return {"status": "deleted", "id": device_id}
