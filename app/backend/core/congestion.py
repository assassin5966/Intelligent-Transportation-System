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

from ...common.business_rules import get_rule
from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis
from ..core.alerts import persist_alert

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"
# 最新拥挤上报数据 (AI 周期写入)
_CONGESTION_DEV_PREFIX = f"{settings.redis_prefix}:congestion:device:"
# 拥挤状态 (onset/recovery 判定)
_CONGESTION_STATE_PREFIX = f"{settings.redis_prefix}:congestion:state:"


def _parse_int(raw) -> int:
    """Redis hash 字段解析为 int; 空值/非法返回 0."""
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


def _clamped_factor(value: float, threshold: float, active: bool) -> float:
    """某指标相对阈值的饱和度因子 (0-1); 未启用维度返回 0."""
    if not active or threshold <= 0:
        return 0.0
    return min(1.0, value / threshold)


def evaluate_congestion(
    roi_vehicles: int,
    vehicle_flow_per_min: float,
    roi_persons: int,
    person_flow_per_min: float,
    max_vehicles: int,
    max_persons: int,
    min_flow: float,
    person_min_flow: float,
    vehicle_weight: float = 0.5,
    person_weight: float = 0.5,
    threshold: float = 0.5,
) -> dict:
    """计算设备拥挤度 (车辆/人流双维度 + 加权综合).

    - vehicle_congested / person_congested: 各维度双阈值布尔
      (数量 >= 阈值 且 速度 < 速度下限).
    - vehicle_score / person_score: 各维度连续拥挤度 (0-1),
      数量饱和度与速度因子各半加权.
    - congestion_score: 加权综合拥挤度 (0-1).
    - congested: 综合布尔. 仅配置单维度阈值时与对应维度布尔一致 (兼容原行为);
      两维度均配置 (人车混合) 时按权重加权, congested = score >= threshold.
    """
    vehicle_active = max_vehicles is not None and max_vehicles > 0
    person_active = max_persons is not None and max_persons > 0

    vehicle_congested = (
        vehicle_active
        and roi_vehicles >= max_vehicles
        and vehicle_flow_per_min < min_flow
    )
    person_congested = (
        person_active
        and roi_persons >= max_persons
        and person_flow_per_min < person_min_flow
    )

    vehicle_count = _clamped_factor(roi_vehicles, max_vehicles, vehicle_active)
    # 维度未配置阈值时不参与拥挤度: 速度因子须为 0,
    # 否则 1.0 - _clamped_factor(..., active=False)=1.0 会让该维度虚报 0.5 分
    vehicle_flow = 1.0 - _clamped_factor(vehicle_flow_per_min, min_flow, vehicle_active) if vehicle_active else 0.0
    person_count = _clamped_factor(roi_persons, max_persons, person_active)
    person_flow = 1.0 - _clamped_factor(person_flow_per_min, person_min_flow, person_active) if person_active else 0.0
    vehicle_score = 0.5 * vehicle_count + 0.5 * vehicle_flow
    person_score = 0.5 * person_count + 0.5 * person_flow

    if vehicle_active and person_active:
        total_w = vehicle_weight + person_weight
        w_v = vehicle_weight / total_w if total_w > 0 else 0.5
        w_p = person_weight / total_w if total_w > 0 else 0.5
        score = w_v * vehicle_score + w_p * person_score
        congested = score >= threshold
    elif vehicle_active:
        score = vehicle_score
        congested = vehicle_congested
    elif person_active:
        score = person_score
        congested = person_congested
    else:
        score = 0.0
        congested = False

    return {
        "vehicle_congested": vehicle_congested,
        "person_congested": person_congested,
        "vehicle_score": round(vehicle_score, 3),
        "person_score": round(person_score, 3),
        "congestion_score": round(score, 3),
        "congested": congested,
    }


async def record_congestion(
    device_id: str,
    roi_vehicles: int,
    vehicle_flow_per_min: float,
    person_flow_per_min: float = 0.0,
    roi_persons: int = 0,
) -> dict:
    """记录一次 AI 上报的拥挤数据, 执行双维度加权拥挤判定, 返回判定结果.

    - roi_vehicles: ROI 内瞬时车辆个数
    - roi_persons: ROI 内瞬时人员个数
    - vehicle_flow_per_min: 每分钟车流量 (辆/分钟, 车流速度)
    - person_flow_per_min: 每分钟人流量 (人/分钟, 人流速度)
    """
    redis = get_redis()
    now_iso = datetime.now(timezone.utc).isoformat()
    data_key = f"{_CONGESTION_DEV_PREFIX}{device_id}"
    await redis.hset(
        data_key,
        mapping={
            "device_id": device_id,
            "roi_vehicles": roi_vehicles,
            "roi_persons": roi_persons,
            "vehicle_flow_per_min": vehicle_flow_per_min,
            "person_flow_per_min": person_flow_per_min,
            "updated_at": now_iso,
        },
    )
    await redis.expire(data_key, 7200)  # 保留 2 小时防泄漏

    # 设备未配置拥挤阈值 -> 不判拥挤 (但保留上报数据供查询)
    dev_key = f"{_DEVICE_KEY_PREFIX}{device_id}"
    dev = await redis.hgetall(dev_key)
    max_vehicles = _parse_int(dev.get("max_vehicles", ""))
    max_persons = _parse_int(dev.get("max_persons", ""))
    if max_vehicles <= 0 and max_persons <= 0:
        return {
            "device_id": device_id,
            "congested": None,
            "reason": "no congestion threshold configured",
            "max_vehicles": max_vehicles,
            "max_persons": max_persons,
        }

    # 阈值与权重热重载 (business_rules.yaml 修改后无需重启)
    min_flow = float(get_rule("congestion", "congestion_min_flow", default=settings.congestion_min_flow))
    person_min_flow = float(get_rule("congestion", "person_congestion_min_flow", default=settings.person_congestion_min_flow))
    vehicle_weight = float(get_rule("congestion", "congestion_vehicle_weight", default=settings.congestion_vehicle_weight))
    person_weight = float(get_rule("congestion", "congestion_person_weight", default=settings.congestion_person_weight))
    threshold = float(get_rule("congestion", "congestion_threshold", default=settings.congestion_threshold))

    result = evaluate_congestion(
        roi_vehicles, vehicle_flow_per_min,
        roi_persons, person_flow_per_min,
        max_vehicles, max_persons,
        min_flow, person_min_flow,
        vehicle_weight, person_weight, threshold,
    )
    congested = result["congested"]

    state_key = f"{_CONGESTION_STATE_PREFIX}{device_id}"
    prev_state = await redis.get(state_key)
    prev_congested = prev_state == "1"
    await redis.set(state_key, "1" if congested else "0", ex=86400)

    # 状态转移: 未拥挤 -> 拥挤 (onset); 拥挤 -> 未拥挤 (recovery)
    if congested and not prev_congested:
        await _trigger_alert(
            device_id, roi_vehicles, roi_persons, vehicle_flow_per_min,
            person_flow_per_min, max_vehicles, max_persons, result,
            "critical", "onset",
        )
    elif not congested and prev_congested:
        await _trigger_alert(
            device_id, roi_vehicles, roi_persons, vehicle_flow_per_min,
            person_flow_per_min, max_vehicles, max_persons, result,
            "info", "recovery",
        )

    return {
        "device_id": device_id,
        "congested": congested,
        "roi_vehicles": roi_vehicles,
        "roi_persons": roi_persons,
        "vehicle_flow_per_min": vehicle_flow_per_min,
        "person_flow_per_min": person_flow_per_min,
        "max_vehicles": max_vehicles,
        "max_persons": max_persons,
        **result,
    }


async def _trigger_alert(
    device_id: str,
    roi_vehicles: int,
    roi_persons: int,
    vehicle_flow_per_min: float,
    person_flow_per_min: float,
    max_vehicles: int,
    max_persons: int,
    result: dict,
    level: str,
    phase: str,
) -> None:
    """持久化拥挤告警并 WebSocket 推送 (onset=critical, recovery=info).

    消息按触发维度区分: 车辆拥堵 / 人流拥堵 / 人车混合拥堵.
    """
    min_flow = float(get_rule("congestion", "congestion_min_flow", default=settings.congestion_min_flow))
    person_min_flow = float(get_rule("congestion", "person_congestion_min_flow", default=settings.person_congestion_min_flow))
    threshold = float(get_rule("congestion", "congestion_threshold", default=settings.congestion_threshold))
    score = result.get("congestion_score", 0.0)
    mixed = max_vehicles > 0 and max_persons > 0

    if phase == "onset":
        if mixed:
            message = (
                f"设备 {device_id} 人车混合拥堵: 综合拥挤度 {score:.2f} "
                f"(车辆 {roi_vehicles}/{max_vehicles} 人流 {roi_persons}/{max_persons})"
            )
        elif max_persons > 0:
            message = (
                f"设备 {device_id} 人流拥堵: 区域人数 {roi_persons}/{max_persons} "
                f"且人流速度 {person_flow_per_min:.1f} 人/分钟 (低于 {person_min_flow})"
            )
        else:
            message = (
                f"设备 {device_id} 拥堵: 区域车辆 {roi_vehicles}/{max_vehicles} "
                f"且车流速度 {vehicle_flow_per_min:.1f} 辆/分钟 (低于 {min_flow})"
            )
    else:
        if mixed:
            message = f"设备 {device_id} 拥堵解除: 综合拥挤度 {score:.2f}"
        elif max_persons > 0:
            message = (
                f"设备 {device_id} 人流拥堵解除: 区域人数 {roi_persons} "
                f"人流速度 {person_flow_per_min:.1f} 人/分钟"
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
        "value": score,
        "threshold": threshold if mixed else (max_persons if max_persons > 0 else max_vehicles),
        "device_id": device_id,
        "roi_vehicles": roi_vehicles,
        "roi_persons": roi_persons,
        "vehicle_flow_per_min": vehicle_flow_per_min,
        "person_flow_per_min": person_flow_per_min,
        "max_vehicles": max_vehicles,
        "max_persons": max_persons,
        "congestion_score": score,
        "vehicle_congested": result.get("vehicle_congested", False),
        "person_congested": result.get("person_congested", False),
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
            "roi_persons": int(data.get("roi_persons", 0)),
            "vehicle_flow_per_min": float(data.get("vehicle_flow_per_min", 0.0)),
            "person_flow_per_min": float(data.get("person_flow_per_min", 0.0)),
            "updated_at": data.get("updated_at", ""),
        })
    return out
