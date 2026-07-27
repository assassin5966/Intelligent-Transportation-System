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

# 模块级共享 httpx 连接池 (设备 API 调用频率低, 复用避免反复建连)
_shared_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=10.0)
    return _shared_client


def _warn_if_out_of_range(coords: list[list[float]], name: str) -> None:
    """归一化坐标应在 [0,1]; 超出范围告警 (计数线会画到画面外)."""
    for pt in coords:
        for v in pt:
            if v < 0 or v > 1:
                logger.warning(f"{name} 存在超出 [0,1] 的值 {v}, 计数线可能偏离画面")
                return


class DeviceIn(BaseModel):
    id: str
    name: str
    stream_url: str
    line_coords: Optional[str] = None  # "x1,y1,x2,y2" 归一化 0-1
    anchor_coords: Optional[str] = None  # "x,y" 归一化 0-1, 内侧锚点
    count_only: Optional[str] = None  # None=双向, "enter"=只计Enter, "exit"=只计Exit
    camera_type: Optional[str] = None  # None=全部检测, "vehicle"=只检测机动车, "person"=只检测人流(含非机动车)


class DeviceOut(BaseModel):
    id: str
    name: str
    stream_url: str
    line_coords: Optional[str] = None
    anchor_coords: Optional[str] = None
    count_only: Optional[str] = None
    camera_type: Optional[str] = None
    status: str = "registered"


def _line_from_coords(line_coords: Optional[str]) -> list[list[float]]:
    """解析 line_coords -> [[x1,y1],[x2,y2]]; 无法解析时返回默认线并告警."""
    if line_coords:
        try:
            parts = [float(x) for x in line_coords.split(",")]
            if len(parts) == 4:
                coords = [[parts[0], parts[1]], [parts[2], parts[3]]]
                _warn_if_out_of_range(coords, "line_coords")
                return coords
        except ValueError:
            logger.warning(f"line_coords 格式非法, 使用默认线: {line_coords!r}")
    return [[0.5, 0.1], [0.5, 0.9]]


def _anchor_from_coords(anchor_coords: Optional[str]) -> Optional[list[float]]:
    """解析 anchor_coords -> [x,y]; 无法解析时返回 None (由 counter 用默认锚点) 并告警."""
    if anchor_coords:
        try:
            parts = [float(x) for x in anchor_coords.split(",")]
            if len(parts) == 2:
                _warn_if_out_of_range([parts], "anchor_coords")
                return [parts[0], parts[1]]
        except ValueError:
            logger.warning(f"anchor_coords 格式非法, 使用默认锚点: {anchor_coords!r}")
    return None


async def _forward_to_ai(method: str, path: str, json_body: Optional[dict] = None) -> None:
    """转发到 AI 分析服务; 失败仅告警, 不阻塞设备配置落库."""
    url = f"{settings.ai_service_url}{path}"
    try:
        client = _get_client()
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
            "anchor_coords": dev.anchor_coords or "",
            "count_only": dev.count_only or "",
            "camera_type": dev.camera_type or "",
            "status": "registered",
        },
    )
    line = _line_from_coords(dev.line_coords)
    anchor = _anchor_from_coords(dev.anchor_coords)
    payload = {"device_id": dev.id, "stream_url": dev.stream_url, "line": line}
    if anchor is not None:
        payload["anchor"] = anchor
    if dev.count_only is not None:
        payload["count_only"] = dev.count_only
    if dev.camera_type is not None:
        payload["camera_type"] = dev.camera_type
    await _forward_to_ai("POST", "/devices", payload)
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
