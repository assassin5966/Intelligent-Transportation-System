"""实时统计 API."""
from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel, Field

from ...common.config import settings
from ...common.redis_client import get_redis
from ...schemas.events import RealtimeStats
from ..core.realtime import get_stats, get_device_stats, get_all_device_stats
from ..core.congestion import record_congestion, latest_congestion

router = APIRouter(prefix="/api/stats", tags=["stats"])

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"


class CongestionIn(BaseModel):
    """AI 周期上报的拥挤判断输入."""
    device_id: str = Field(..., description="设备ID")
    roi_vehicles: int = Field(0, ge=0, description="ROI 内瞬时车辆个数")
    vehicle_flow_per_min: float = Field(0.0, ge=0, description="每分钟车流量 (辆/分钟)")


@router.get("/realtime", response_model=RealtimeStats)
async def realtime():
    """当前车辆/人员数量、今日累计、活跃设备数 (全局加总)."""
    return RealtimeStats(**await get_stats())


@router.get("/devices")
async def device_stats():
    """各设备分别计数 (当前在场 + 今日累计 + 拥挤状态).

    返回所有注册设备的统计; 未产生事件的设备计数为 0.
    """
    redis = get_redis()
    # 1. 读所有注册设备配置 (含名称/类型/状态/最大车辆数)
    devices: list[dict] = []
    async for key in redis.scan_iter(f"{_DEVICE_KEY_PREFIX}*"):
        data = await redis.hgetall(key)
        if data:
            max_v_raw = data.get("max_vehicles", "")
            try:
                max_vehicles = int(max_v_raw) if max_v_raw else None
            except (TypeError, ValueError):
                max_vehicles = None
            devices.append({
                "device_id": data.get("id", ""),
                "name": data.get("name", ""),
                "camera_type": data.get("camera_type", ""),
                "status": data.get("status", ""),
                "max_vehicles": max_vehicles,
            })
    if not devices:
        return []
    # 2. 批量读各设备统计 (一次 pipeline)
    stats_map: dict[str, dict] = {s["device_id"]: s for s in await get_all_device_stats()}
    # 3. 读各设备最新拥挤数据 (ROI 车辆数 + 车流速度)
    congestion_map: dict[str, dict] = {c["device_id"]: c for c in await latest_congestion()}
    # 4. 合并配置 + 统计 + 拥挤数据
    result = []
    for dev in devices:
        did = dev["device_id"]
        s = stats_map.get(did)
        row: dict = {**dev}
        if s:
            row.update(s)
        else:
            # 注册了但未产生事件的设备, 计数为 0
            row.update({
                "current_vehicles": 0,
                "current_persons": 0,
                "today_vehicle_in": 0,
                "today_vehicle_out": 0,
                "today_person_in": 0,
                "today_person_out": 0,
            })
        c = congestion_map.get(did)
        if c:
            row["roi_vehicles"] = c["roi_vehicles"]
            row["vehicle_flow_per_min"] = c["vehicle_flow_per_min"]
            row["congested"] = (
                row["max_vehicles"] is not None
                and row["max_vehicles"] > 0
                and c["roi_vehicles"] >= row["max_vehicles"]
                and c["vehicle_flow_per_min"] < settings.congestion_min_flow
            )
        else:
            row["roi_vehicles"] = 0
            row["vehicle_flow_per_min"] = 0.0
            row["congested"] = False
        result.append(row)
    return result


@router.get("/devices/{device_id}")
async def device_stats_one(device_id: str):
    """单个设备分别计数 (当前在场 + 今日累计 + 拥挤状态)."""
    redis = get_redis()
    data = await redis.hgetall(f"{_DEVICE_KEY_PREFIX}{device_id}")
    if not data:
        raise HTTPException(404, "device not found")
    stats = await get_device_stats(device_id)
    max_v_raw = data.get("max_vehicles", "")
    try:
        max_vehicles = int(max_v_raw) if max_v_raw else None
    except (TypeError, ValueError):
        max_vehicles = None
    congestion = await latest_congestion(device_id)
    c = congestion[0] if congestion else None
    row = {
        "device_id": device_id,
        "name": data.get("name", ""),
        "camera_type": data.get("camera_type", ""),
        "status": data.get("status", ""),
        "max_vehicles": max_vehicles,
        **stats,
        "roi_vehicles": c["roi_vehicles"] if c else 0,
        "vehicle_flow_per_min": c["vehicle_flow_per_min"] if c else 0.0,
    }
    row["congested"] = (
        max_vehicles is not None
        and max_vehicles > 0
        and row["roi_vehicles"] >= max_vehicles
        and row["vehicle_flow_per_min"] < settings.congestion_min_flow
    )
    return row


@router.post("/congestion", status_code=200)
async def congestion_report(body: CongestionIn):
    """AI 周期上报 ROI 内车辆数 + 每分钟车流量, 后端执行拥挤判定.

    拥挤条件: roi_vehicles >= 设备.max_vehicles 且 vehicle_flow_per_min < congestion_min_flow.
    判定为拥挤时触发 critical 告警; 解除时触发 info 告警 (带状态去抖).
    """
    redis = get_redis()
    dev = await redis.hgetall(f"{_DEVICE_KEY_PREFIX}{body.device_id}")
    if not dev:
        raise HTTPException(404, "device not found")
    return await record_congestion(
        body.device_id, body.roi_vehicles, body.vehicle_flow_per_min
    )


@router.get("/congestion")
async def congestion_latest(device_id: str = Query(None, description="可选: 指定设备ID")):
    """查询最新拥挤数据 (ROI 车辆数 + 每分钟车流量).

    - 不传 device_id: 返回所有上报过数据的设备
    - 传 device_id: 返回该设备 (无数据返回空列表)
    """
    return await latest_congestion(device_id)
