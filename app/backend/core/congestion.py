"""拥挤判断核心.

结合两项指标按双阈值判定设备是否拥挤:
  - 区域车辆个数 (ROI 内瞬时车辆数, AI 周期上报)
  - 车流速度 (最近 60 秒车辆跨线次数折算为每分钟车流量, AI 周期上报)

拥挤条件: 区域车辆数 >= 设备配置的最大车辆数 (max_vehicles) 且 车流速度 < congestion_min_flow.
状态带 onset/recovery 转移, 去重后持久化告警并 WebSocket 推送.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis
from ..core.alerts import persist_alert

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"
# 最新拥挤上报数据 (AI 周期写入)
_CONGESTION_DEV_PREFIX = f"{settings.redis_prefix}:congestion:device:"
# 拥挤状态 (onset/recovery 判定)
_CONGESTION_STATE_PREFIX = f"{settings.redis_prefix}:congestion:state:"


async def record_congestion(device_id: str, roi_vehicles: int, vehicle_flow_per_min: float) -> dict:
    """记录一次 AI 上报的拥挤数据, 执行双阈值拥挤判定, 返回判定结果.

    - roi_vehicles: ROI 内瞬时车辆个数
    - vehicle_flow_per_min: 每分钟车流量 (辆/分钟, 车流速度)
    """
    redis = get_redis()
    now_iso = datetime.now(timezone.utc).isoformat()
    data_key = f"{_CONGESTION_DEV_PREFIX}{device_id}"
    await redis.hset(
        data_key,
        mapping={
            "device_id": device_id,
            "roi_vehicles": roi_vehicles,
            "vehicle_flow_per_min": vehicle_flow_per_min,
            "updated_at": now_iso,
        },
    )
    await redis.expire(data_key, 7200)  # 保留 2 小时防泄漏

    # 设备未配置最大车辆数 -> 不判拥挤 (但保留上报数据供查询)
    dev_key = f"{_DEVICE_KEY_PREFIX}{device_id}"
    dev = await redis.hgetall(dev_key)
    max_vehicles_raw = dev.get("max_vehicles", "") if dev else ""
    try:
        max_vehicles = int(max_vehicles_raw) if max_vehicles_raw else 0
    except (TypeError, ValueError):
        max_vehicles = 0

    if max_vehicles <= 0:
        return {
            "device_id": device_id,
            "congested": None,
            "reason": "max_vehicles not configured",
            "max_vehicles": 0,
        }

    congested = (
        roi_vehicles >= max_vehicles
        and vehicle_flow_per_min < settings.congestion_min_flow
    )

    state_key = f"{_CONGESTION_STATE_PREFIX}{device_id}"
    prev_state = await redis.get(state_key)
    prev_congested = prev_state == "1"
    await redis.set(state_key, "1" if congested else "0", ex=86400)

    # 状态转移: 未拥挤 -> 拥挤 (onset); 拥挤 -> 未拥挤 (recovery)
    if congested and not prev_congested:
        await _trigger_alert(
            device_id, roi_vehicles, vehicle_flow_per_min, max_vehicles, "critical", "onset"
        )
    elif not congested and prev_congested:
        await _trigger_alert(
            device_id, roi_vehicles, vehicle_flow_per_min, max_vehicles, "info", "recovery"
        )

    return {
        "device_id": device_id,
        "congested": congested,
        "roi_vehicles": roi_vehicles,
        "vehicle_flow_per_min": vehicle_flow_per_min,
        "max_vehicles": max_vehicles,
    }


async def _trigger_alert(
    device_id: str,
    roi_vehicles: int,
    vehicle_flow_per_min: float,
    max_vehicles: int,
    level: str,
    phase: str,
) -> None:
    """持久化拥挤告警并 WebSocket 推送 (onset=critical, recovery=info)."""
    if phase == "onset":
        message = (
            f"设备 {device_id} 拥堵: 区域车辆 {roi_vehicles}/{max_vehicles} "
            f"且车流速度 {vehicle_flow_per_min:.1f} 辆/分钟 (低于 {settings.congestion_min_flow})"
        )
    else:
        message = (
            f"设备 {device_id} 拥堵解除: 区域车辆 {roi_vehicles} "
            f"车流速度 {vehicle_flow_per_min:.1f} 辆/分钟"
        )
    alert = {
        "rule_id": f"congestion_{device_id}",
        "level": level,
        "category": "congestion",
        "message": message,
        "value": roi_vehicles,
        "threshold": max_vehicles,
        "device_id": device_id,
        "vehicle_flow_per_min": vehicle_flow_per_min,
        "phase": phase,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await persist_alert(alert)
    logger.warning(f"[拥挤] [{phase}] {message}")

    try:
        from ..api.ws import broadcast_alert
        await broadcast_alert(alert)
    except Exception:  # noqa: BLE001
        pass


async def latest_congestion(device_id: Optional[str] = None) -> list[dict]:
    """读取最新拥挤上报数据.

    - 指定 device_id: 返回该设备 (无数据返回空列表).
    - 不指定: 返回所有上报过数据的设备.
    """
    redis = get_redis()
    if device_id:
        keys = [f"{_CONGESTION_DEV_PREFIX}{device_id}"]
    else:
        keys = [k async for k in redis.scan_iter(f"{_CONGESTION_DEV_PREFIX}*")]

    out: list[dict] = []
    for key in keys:
        data = await redis.hgetall(key)
        if not data:
            continue
        out.append({
            "device_id": data.get("device_id", ""),
            "roi_vehicles": int(data.get("roi_vehicles", 0)),
            "vehicle_flow_per_min": float(data.get("vehicle_flow_per_min", 0.0)),
            "updated_at": data.get("updated_at", ""),
        })
    return out
