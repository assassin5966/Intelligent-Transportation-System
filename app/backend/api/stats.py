"""实时统计 API."""
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel, Field

from ...common.business_rules import get_rule
from ...common.config import settings
from ...common.redis_client import get_redis
from ...schemas.events import RealtimeStats
from ..core.realtime import (
    get_stats,
    get_device_stats,
    get_all_device_stats,
)
from ..core.congestion import record_congestion, latest_congestion, evaluate_congestion
from .devices import geo_by_name, category_by_name

router = APIRouter(prefix="/api/stats", tags=["stats"])

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"


class CongestionIn(BaseModel):
    """AI 周期上报的拥挤判断输入."""
    device_id: str = Field(..., description="设备ID")
    roi_vehicles: int = Field(0, ge=0, description="ROI 内瞬时车辆个数")
    roi_persons: int = Field(0, ge=0, description="ROI 内瞬时人员个数")
    vehicle_flow_per_min: float = Field(0.0, ge=0, description="每分钟车流量 (辆/分钟)")
    person_flow_per_min: float = Field(0.0, ge=0, description="每分钟人流量 (人/分钟)")


@router.get("/realtime", response_model=RealtimeStats)
async def realtime():
    """当前车辆/人员数量、今日累计、活跃设备数 (全局加总)."""
    return RealtimeStats(**await get_stats())


@router.get("/devices")
async def device_stats():
    """各设备分别计数 (当前在场 + 今日累计 + 当前小时内车流/人流 + 拥挤状态 + 经纬度).

    返回所有注册设备的统计; 未产生事件的设备计数为 0.
    """
    return await build_all_device_stats_rows()


async def build_all_device_stats_rows() -> list[dict]:
    """构建所有注册设备的完整统计行 (配置 + 计数 + 拥挤 + 经纬度).

    同时供 REST GET /api/stats/devices 与后端 WebSocket /ws 的 `devices` 字段复用,
    保证前端订阅 WebSocket 即可拿全: 设备信息 / 人流 / 车流 / 拥挤状态 / 经纬度.
    """
    redis = get_redis()
    now = datetime.now()
    # 1. 读所有注册设备配置 (含名称/类型/状态/拥挤阈值/经纬度)
    devices: list[dict] = []
    async for key in redis.scan_iter(f"{_DEVICE_KEY_PREFIX}*"):
        data = await redis.hgetall(key)
        if data:
            max_v_raw = data.get("max_vehicles", "")
            max_p_raw = data.get("max_persons", "")
            try:
                max_vehicles = int(max_v_raw) if max_v_raw else None
            except (TypeError, ValueError):
                max_vehicles = None
            try:
                max_persons = int(max_p_raw) if max_p_raw else None
            except (TypeError, ValueError):
                max_persons = None
            lng, lat = geo_by_name(data.get("name", ""))
            devices.append({
                "device_id": data.get("id", ""),
                "name": data.get("name", ""),
                "camera_type": data.get("camera_type", ""),
                "status": data.get("status", ""),
                "max_vehicles": max_vehicles,
                "max_persons": max_persons,
                "longitude": lng,
                "latitude": lat,
                "category": category_by_name(data.get("name", "")),
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
                "hour": now.hour,
                "hour_vehicle_in": 0,
                "hour_vehicle_out": 0,
                "hour_person_in": 0,
                "hour_person_out": 0,
            })
        c = congestion_map.get(did)
        if c:
            row["roi_vehicles"] = c["roi_vehicles"]
            row["roi_persons"] = c.get("roi_persons", 0)
            row["vehicle_flow_per_min"] = c["vehicle_flow_per_min"]
            row["person_flow_per_min"] = c["person_flow_per_min"]
            row.update(await _congestion_result(row, c))
        else:
            row["roi_vehicles"] = 0
            row["roi_persons"] = 0
            row["vehicle_flow_per_min"] = 0.0
            row["person_flow_per_min"] = 0.0
            row["congested"] = False
            row["vehicle_congested"] = False
            row["person_congested"] = False
            row["vehicle_score"] = 0.0
            row["person_score"] = 0.0
            row["congestion_score"] = 0.0
        result.append(row)
    return result


async def _congestion_result(row: dict, c: dict) -> dict:
    """基于设备拥挤阈值与最近一次上报数据计算拥挤判定结果 (双维度 + 加权).

    与后端 record_congestion 使用同一评分函数 evaluate_congestion,
    保证 REST/WS 展示的 congested 与告警判定一致.
    """
    min_flow = float(get_rule("congestion", "congestion_min_flow", default=settings.congestion_min_flow))
    person_min_flow = float(get_rule("congestion", "person_congestion_min_flow", default=settings.person_congestion_min_flow))
    vehicle_weight = float(get_rule("congestion", "congestion_vehicle_weight", default=settings.congestion_vehicle_weight))
    person_weight = float(get_rule("congestion", "congestion_person_weight", default=settings.congestion_person_weight))
    threshold = float(get_rule("congestion", "congestion_threshold", default=settings.congestion_threshold))
    return evaluate_congestion(
        c["roi_vehicles"], c["vehicle_flow_per_min"],
        c.get("roi_persons", 0), c["person_flow_per_min"],
        row.get("max_vehicles") or 0, row.get("max_persons") or 0,
        min_flow, person_min_flow,
        vehicle_weight, person_weight, threshold,
    )


@router.get("/devices/{device_id}")
async def device_stats_one(device_id: str):
    """单个设备分别计数 (当前在场 + 今日累计 + 当前小时内车流/人流 + 拥挤状态)."""
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
    max_p_raw = data.get("max_persons", "")
    try:
        max_persons = int(max_p_raw) if max_p_raw else None
    except (TypeError, ValueError):
        max_persons = None
    congestion = await latest_congestion(device_id)
    c = congestion[0] if congestion else None
    lng, lat = geo_by_name(data.get("name", ""))
    row = {
        "device_id": device_id,
        "name": data.get("name", ""),
        "camera_type": data.get("camera_type", ""),
        "status": data.get("status", ""),
        "max_vehicles": max_vehicles,
        "max_persons": max_persons,
        "longitude": lng,
        "latitude": lat,
        "category": category_by_name(data.get("name", "")),
        **stats,
        "roi_vehicles": c["roi_vehicles"] if c else 0,
        "roi_persons": c.get("roi_persons", 0) if c else 0,
        "vehicle_flow_per_min": c["vehicle_flow_per_min"] if c else 0.0,
        "person_flow_per_min": c["person_flow_per_min"] if c else 0.0,
    }
    if c:
        row.update(await _congestion_result(row, c))
    else:
        row["congested"] = False
        row["vehicle_congested"] = False
        row["person_congested"] = False
        row["vehicle_score"] = 0.0
        row["person_score"] = 0.0
        row["congestion_score"] = 0.0
    return row


@router.post("/congestion", status_code=200)
async def congestion_report(body: CongestionIn):
    """AI 周期上报 ROI 内车辆/人员数 + 每分钟车/人流量, 后端执行拥挤判定.

    车辆拥挤: roi_vehicles >= 设备.max_vehicles 且 vehicle_flow_per_min < congestion_min_flow.
    人流拥挤: roi_persons >= 设备.max_persons 且 person_flow_per_min < person_congestion_min_flow.
    人车混合: 加权拥挤度 >= congestion_threshold.
    判定为拥挤时触发 critical 告警; 解除时触发 info 告警 (带状态去抖).
    """
    redis = get_redis()
    dev = await redis.hgetall(f"{_DEVICE_KEY_PREFIX}{body.device_id}")
    if not dev:
        raise HTTPException(404, "device not found")
    return await record_congestion(
        body.device_id,
        body.roi_vehicles,
        body.vehicle_flow_per_min,
        body.person_flow_per_min,
        body.roi_persons,
    )


@router.get("/hourly/history")
async def hourly_history(
    device_id: str = Query(None, description="可选: 指定设备ID; 缺省返回所有设备合计(全局)"),
    start_date: str = Query(..., description="开始时间 (YYYY-MM-DD 或 YYYY-MM-DD:HH)"),
    end_date: str = Query(..., description="结束时间 (YYYY-MM-DD 或 YYYY-MM-DD:HH)"),
):
    """从 MySQL 查询长期小时级车流/人流量, 返回时间范围总计 (长期报表).

    数据由归档调度器定时从 Redis 落库到 hourly_traffic 表, 不受 Redis 30 天
    保留窗口限制. 支持天+时精确范围, 例如 start_date=2026-08-01:01 到
    end_date=2026-08-22:08 (闭区间). 只传日期则按整日查询. 不传 device_id 时
    聚合所有设备 (全局汇总). 响应仅返回整个时间范围的总计 total, 不含逐小时明细.
    """
    try:
        start_dt, start_hour = _parse_hourly_bound(start_date, 0)
        end_dt, end_hour = _parse_hourly_bound(end_date, 23)
    except ValueError as e:  # noqa: BLE001
        raise HTTPException(422, f"时间格式错误 (应为 YYYY-MM-DD 或 YYYY-MM-DD:HH): {e}") from e
    if (start_dt, start_hour) > (end_dt, end_hour):
        raise HTTPException(422, "开始时间不能晚于结束时间")

    if not settings.mysql_enabled:
        raise HTTPException(503, "MySQL 归档未启用 (MYSQL_ENABLED=false)")
    try:
        from ...common.mysql_client import ensure_table, query_hourly

        await ensure_table()
        rows = await query_hourly(device_id, start_dt, start_hour, end_dt, end_hour)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"MySQL 查询失败: {e}") from e

    total = {"vehicle_in": 0, "vehicle_out": 0, "person_in": 0, "person_out": 0}
    records = []
    for r in rows:
        records.append({
            "stat_date": r["stat_date"].isoformat() if hasattr(r["stat_date"], "isoformat") else str(r["stat_date"]),
            "hour": int(r["hour"]),
            "vehicle_in": int(r["vehicle_in"]),
            "vehicle_out": int(r["vehicle_out"]),
            "person_in": int(r["person_in"]),
            "person_out": int(r["person_out"]),
        })
        total["vehicle_in"] += r["vehicle_in"]
        total["vehicle_out"] += r["vehicle_out"]
        total["person_in"] += r["person_in"]
        total["person_out"] += r["person_out"]
    return {
        "device_id": device_id,
        "start": f"{start_dt.isoformat()}:{start_hour:02d}",
        "end": f"{end_dt.isoformat()}:{end_hour:02d}",
        "records": records,
        "total": total,
    }


def _parse_hourly_bound(value: str, default_hour: int) -> tuple[date, int]:
    """解析时间边界: 支持 YYYY-MM-DD (取 default_hour) 或 YYYY-MM-DD:HH."""
    value = value.strip()
    if ":" in value:
        date_part, hour_part = value.rsplit(":", 1)
        hour = int(hour_part)
        if not 0 <= hour <= 23:
            raise ValueError("hour 需在 0-23 之间")
        return datetime.strptime(date_part, "%Y-%m-%d").date(), hour
    return datetime.strptime(value, "%Y-%m-%d").date(), default_hour
