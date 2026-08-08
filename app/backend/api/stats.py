"""实时统计 API."""
from fastapi import APIRouter, HTTPException

from ...common.config import settings
from ...common.redis_client import get_redis
from ...schemas.events import RealtimeStats
from ..core.realtime import get_stats, get_device_stats, get_all_device_stats

router = APIRouter(prefix="/api/stats", tags=["stats"])

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"


@router.get("/realtime", response_model=RealtimeStats)
async def realtime():
    """当前车辆/人员数量、今日累计、活跃设备数 (全局加总)."""
    return RealtimeStats(**await get_stats())


@router.get("/devices")
async def device_stats():
    """各设备分别计数 (当前在场 + 今日累计).

    返回所有注册设备的统计; 未产生事件的设备计数为 0.
    """
    redis = get_redis()
    # 1. 读所有注册设备配置 (含名称/类型/状态)
    devices: list[dict] = []
    async for key in redis.scan_iter(f"{_DEVICE_KEY_PREFIX}*"):
        data = await redis.hgetall(key)
        if data:
            devices.append({
                "device_id": data.get("id", ""),
                "name": data.get("name", ""),
                "camera_type": data.get("camera_type", ""),
                "status": data.get("status", ""),
            })
    if not devices:
        return []
    # 2. 批量读各设备统计 (一次 pipeline)
    stats_map: dict[str, dict] = {s["device_id"]: s for s in await get_all_device_stats()}
    # 3. 合并配置 + 统计
    result = []
    for dev in devices:
        did = dev["device_id"]
        s = stats_map.get(did)
        if s:
            result.append({**dev, **s})
        else:
            # 注册了但未产生事件的设备, 计数为 0
            result.append({
                **dev,
                "current_vehicles": 0,
                "current_persons": 0,
                "today_vehicle_in": 0,
                "today_vehicle_out": 0,
                "today_person_in": 0,
                "today_person_out": 0,
            })
    return result


@router.get("/devices/{device_id}")
async def device_stats_one(device_id: str):
    """单个设备分别计数 (当前在场 + 今日累计)."""
    redis = get_redis()
    data = await redis.hgetall(f"{_DEVICE_KEY_PREFIX}{device_id}")
    if not data:
        raise HTTPException(404, "device not found")
    stats = await get_device_stats(device_id)
    return {
        "device_id": device_id,
        "name": data.get("name", ""),
        "camera_type": data.get("camera_type", ""),
        "status": data.get("status", ""),
        **stats,
    }
