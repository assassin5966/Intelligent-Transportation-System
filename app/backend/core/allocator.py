"""警力区域分配算法 (三阶段).

阶段 1: 需求计算 — 当前人数 × α + 预测人数 × β
阶段 2: 目标分配 — 比例分配 + 最小保障, 取整修正
阶段 3: 调度规划 — 贪心最近优先, 移动上限控制
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ...common.business_rules import get_rule
from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis
from .realtime import get_all_device_crowds

_REGIONS_KEY = f"{settings.redis_prefix}:police:regions"
_TOTAL_KEY = f"{settings.redis_prefix}:police:total"
_PLAN_KEY = f"{settings.redis_prefix}:police:plan:latest"

# 设备配置 key (含 name 等注册信息, 见 devices.py)
_DEVICE_CFG_PREFIX = f"{settings.redis_prefix}:device:"
# 设备经纬度映射缓存: 名称(去空格) -> (longitude, latitude) (来自 data/device_geo.json)
_GEO_FILE = Path(__file__).resolve().parents[3] / "data" / "device_geo.json"
_geo_cache: Optional[dict] = None


def _load_device_geo() -> dict:
    """加载 data/device_geo.json (名称去空格), 返回 {名称: (经度, 纬度)}."""
    global _geo_cache
    if _geo_cache is None:
        try:
            raw = json.loads(_GEO_FILE.read_text(encoding="utf-8")).get("devices", {})
            _geo_cache = {
                name.replace(" ", ""): (d["longitude"], d["latitude"])
                for name, d in raw.items()
                if d.get("longitude") is not None and d.get("latitude") is not None
            }
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"读取 device_geo.json 失败: {e}")
            _geo_cache = {}
    return _geo_cache


async def _device_geo_map(device_ids) -> dict[str, tuple[float, float]]:
    """读取设备名称并匹配 device_geo.json, 返回 {device_id: (longitude, latitude)}."""
    redis = get_redis()
    geo = _load_device_geo()
    result: dict[str, tuple[float, float]] = {}
    for did in device_ids:
        data = await redis.hgetall(f"{_DEVICE_CFG_PREFIX}{did}")
        if not data:
            continue
        name = data.get("name", "")
        if isinstance(name, bytes):
            name = name.decode()
        pt = geo.get(name.replace(" ", ""))
        if pt:
            result[did] = pt
    return result


async def _get_regions() -> list[dict]:
    """读取所有注册区域."""
    redis = get_redis()
    raw = await redis.hgetall(_REGIONS_KEY)
    regions = []
    for rid, val in raw.items():
        rid_str = rid if isinstance(rid, str) else rid.decode()
        try:
            region = json.loads(val if isinstance(val, str) else val.decode())
            region["region_id"] = rid_str
            regions.append(region)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"区域 {rid_str} 数据损坏, 跳过")
    return regions


async def _get_total_officers() -> int:
    """读取总警力数."""
    redis = get_redis()
    val = await redis.get(_TOTAL_KEY)
    if val is None:
        return 0
    return int(val)


async def seed_from_config(path: Optional[str] = None) -> dict:
    """从 configs/police.yaml 加载警力初始配置 (区域 + 总警力), 写入 Redis.

    仅当 Redis 中尚无对应数据时写入 (HSETNX / SETNX), 不覆盖 API 动态配置
    (POST /api/police/regions、POST /api/police/total). 返回加载统计.
    """
    p = Path(path or settings.police_config_file)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except OSError as e:
        logger.warning(f"警力配置文件读取失败: {e}")
        return {"total": 0, "regions": 0}

    redis = get_redis()

    # 总警力: 仅当未设置时写入
    total_added = 0
    total = data.get("total")
    if total is not None:
        ok = await redis.set(_TOTAL_KEY, int(total), nx=True)
        if ok:
            total_added = 1
            logger.info(f"[警力配置] 从 {p.name} 设置总警力: {total}")

    # 区域: 逐个 HSETNX, 仅当该区域 id 不存在时写入
    regions_added = 0
    for region in data.get("regions", []) or []:
        rid = region.get("id")
        if not rid:
            continue
        payload = json.dumps(
            {
                "name": region.get("name", rid),
                "longitude": region.get("longitude"),
                "latitude": region.get("latitude"),
                "center_x": float(region.get("center_x", 0)),
                "center_y": float(region.get("center_y", 0)),
                "device_id": region.get("device_id", ""),
                "current_officers": 0,
            },
            ensure_ascii=False,
        )
        ok = await redis.hsetnx(_REGIONS_KEY, rid, payload)
        if ok:
            regions_added += 1
            logger.info(f"[警力配置] 从 {p.name} 注册区域: {rid} ({region.get('name', rid)})")

    if regions_added or total_added:
        logger.info(f"[警力配置] 加载完成: 区域 {regions_added}, 总警力 {total_added} (仅新增)")
    else:
        logger.info("[警力配置] 无新增项 (Redis 已有数据, 以 API 配置为准)")
    return {"total": total_added, "regions": regions_added}


async def _get_predicted_total() -> float:
    """读取 Chronos-2 预测缓存的总人数."""
    redis = get_redis()
    raw = await redis.get(f"{settings.redis_prefix}:prediction:latest:total")
    if not raw:
        return 0.0
    try:
        data = json.loads(raw)
        return float(data.get("predicted_total", 0))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0.0


def _euclidean(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """两点欧氏距离."""
    return math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)


def _compute_targets(
    demands: dict[str, float],
    current_officers: dict[str, int],
    total_officers: int,
    min_per_region: int,
) -> dict[str, int]:
    """阶段 2: 比例分配 + 最小保障 + 取整修正."""
    m = len(demands)
    if m == 0:
        return {}
    if total_officers < m:
        # 警力不足: 按 demand 降序分配, 每区域最多 1 人
        sorted_ids = sorted(demands, key=lambda r: demands[r], reverse=True)
        targets: dict[str, int] = {rid: 0 for rid in demands}
        for i in range(total_officers):
            targets[sorted_ids[i]] = 1
        return targets

    # 钳制 base: 确保 m * base <= total_officers, 避免 remaining<0 时取整修正死循环
    base = min(min_per_region, total_officers // m)
    remaining = total_officers - m * base
    total_demand = sum(demands.values())

    if total_demand <= 0:
        # 无需求: 均匀分配
        per = remaining // m
        extra = remaining % m
        sorted_ids = sorted(demands)
        targets = {rid: base + per for rid in demands}
        for i in range(extra):
            targets[sorted_ids[i]] += 1
        return targets

    # 比例分配
    targets = {}
    for rid, demand in demands.items():
        targets[rid] = base + round(remaining * demand / total_demand)

    # 取整修正: Σ target 可能 != total_officers
    diff = total_officers - sum(targets.values())
    if diff != 0:
        sorted_ids = sorted(demands, key=lambda r: demands[r], reverse=abs(diff) > 0)
        idx = 0
        while diff > 0:
            targets[sorted_ids[idx % m]] += 1
            diff -= 1
            idx += 1
        while diff < 0:
            rid = sorted_ids[idx % m]
            if targets[rid] > base:
                targets[rid] -= 1
                diff += 1
            idx += 1

    return targets


def _plan_movements(
    targets: dict[str, int],
    current: dict[str, int],
    centers: dict[str, tuple[float, float]],
    move_limit: int,
) -> list[dict]:
    """阶段 3: 贪心最近优先调度."""
    surplus: dict[str, int] = {}
    deficit: dict[str, int] = {}
    for rid in targets:
        diff = current.get(rid, 0) - targets[rid]
        if diff > 0:
            surplus[rid] = diff
        elif diff < 0:
            deficit[rid] = -diff

    if not surplus or not deficit:
        return []

    # 计算所有 (surplus, deficit) 对的距离, 升序排列
    pairs = []
    for s_id, s_count in surplus.items():
        for d_id, d_count in deficit.items():
            dist = _euclidean(centers[s_id], centers[d_id])
            pairs.append((dist, s_id, d_id, min(s_count, d_count)))
    pairs.sort(key=lambda x: x[0])

    # 贪心分配
    movements: list[dict] = []
    remaining_limit = move_limit
    for dist, s_id, d_id, _ in pairs:
        if remaining_limit <= 0:
            break
        if surplus[s_id] <= 0 or deficit[d_id] <= 0:
            continue
        actual = min(surplus[s_id], deficit[d_id], remaining_limit)
        if actual > 0:
            movements.append({
                "from_region": s_id,
                "to_region": d_id,
                "count": actual,
                "distance": round(dist, 1),
            })
            surplus[s_id] -= actual
            deficit[d_id] -= actual
            remaining_limit -= actual

    return movements


async def optimize_allocation() -> Optional[dict]:
    """执行三阶段警力分配算法, 返回分配方案."""
    regions = await _get_regions()
    if not regions:
        logger.warning("无注册区域, 跳过警力分配")
        return None

    total_officers = await _get_total_officers()
    if total_officers <= 0:
        logger.warning("总警力数为 0, 跳过警力分配")
        return None

    # 读取各设备在場人数
    device_crowds = await get_all_device_crowds()
    # 摄像头就近归区: 每台设备归属距离最近的区域中心 (最近邻分类), 区域人数 =
    # 归属该区域的摄像头人数之和; 区域无归属设备且显式绑定设备有数据时兜底.
    device_geo = await _device_geo_map(device_crowds.keys())
    region_geo = {
        r["region_id"]: (float(r.get("longitude") or 0.0), float(r.get("latitude") or 0.0))
        for r in regions
    }
    crowds: dict[str, int] = {r["region_id"]: 0 for r in regions}
    for did, (lon, lat) in device_geo.items():
        best_rid = min(
            region_geo,
            key=lambda k: (region_geo[k][0] - lon) ** 2 + (region_geo[k][1] - lat) ** 2,
        )
        crowds[best_rid] += device_crowds.get(did, {}).get("current_persons", 0)
    predicted_total = await _get_predicted_total()

    # 警力算法参数热重载 (business_rules.yaml 修改后无需重启)
    alpha = float(get_rule("police", "demand_weight_current", default=settings.police_demand_weight_current))
    beta = float(get_rule("police", "demand_weight_predict", default=settings.police_demand_weight_predict))
    min_per_region = int(get_rule("police", "min_per_region", default=settings.police_min_per_region))
    movement_ratio = float(get_rule("police", "movement_ratio", default=settings.police_movement_ratio))

    # 阶段 1: 需求计算
    demands: dict[str, float] = {}
    predicted_crowds: dict[str, float] = {}
    centers: dict[str, tuple[float, float]] = {}
    current_officers: dict[str, int] = {}
    total_crowd = 0.0

    for r in regions:
        rid = r["region_id"]
        centers[rid] = (float(r["center_x"]), float(r["center_y"]))
        current_officers[rid] = int(r.get("current_officers", 0))

        # 兜底: 区域无归属摄像头时, 若显式绑定 device_id 有数据则使用
        if crowds[rid] == 0:
            bound = r.get("device_id", "")
            crowds[rid] = device_crowds.get(bound, {}).get("current_persons", 0)
        total_crowd += crowds[rid]

    # 按占比分摊预测值
    for r in regions:
        rid = r["region_id"]
        if total_crowd > 0:
            share = crowds[rid] / total_crowd
        else:
            share = 1.0 / len(regions)
        predicted_crowds[rid] = predicted_total * share
        # 需求 = α × 当前 + β × 预测
        if total_crowd == 0 and predicted_total == 0:
            demands[rid] = 1.0  # 无数据时均匀分配
        else:
            demands[rid] = alpha * crowds[rid] + beta * predicted_crowds[rid]

    # 阶段 2: 目标分配
    targets = _compute_targets(
        demands, current_officers, total_officers, min_per_region
    )

    # 阶段 3: 调度规划
    move_limit = math.ceil(total_officers * movement_ratio)
    movements = _plan_movements(targets, current_officers, centers, move_limit)

    # 评分
    m = len(regions)
    total_demand = sum(demands.values()) or 1.0
    coverage = sum(
        demands[rid] / max(1, targets[rid]) for rid in targets
    ) / m
    max_dist = max(
        (_euclidean(centers[mv["from_region"]], centers[mv["to_region"]])
         for mv in movements),
        default=1.0,
    ) or 1.0
    total_move_dist = sum(mv["distance"] * mv["count"] for mv in movements)
    efficiency = 1.0 - total_move_dist / (total_officers * max_dist) if total_officers > 0 else 1.0
    score = 0.7 * min(coverage, 1.0) + 0.3 * max(0.0, efficiency)

    # 组装区域结果
    region_results = []
    for r in sorted(regions, key=lambda x: demands[x["region_id"]], reverse=True):
        rid = r["region_id"]
        region_results.append({
            "region_id": rid,
            "name": r.get("name", rid),
            "device_id": r.get("device_id", ""),
            "current_crowd": crowds[rid],
            "predicted_crowd": round(predicted_crowds[rid], 1),
            "demand": round(demands[rid], 1),
            "current_officers": current_officers[rid],
            "target_officers": targets[rid],
            "delta": targets[rid] - current_officers[rid],
        })

    plan = {
        "total_officers": total_officers,
        "regions": region_results,
        "movements": movements,
        "summary": {
            "total_movements": sum(mv["count"] for mv in movements),
            "coverage_score": round(min(coverage, 1.0), 3),
            "efficiency_score": round(max(0.0, efficiency), 3),
            "overall_score": round(score, 3),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    # 缓存方案到 Redis (TTL 随预测周期热重载)
    redis = get_redis()
    ttl = int(get_rule("prediction", "interval_minutes", default=settings.prediction_interval_minutes)) * 60 * 2
    await redis.set(_PLAN_KEY, json.dumps(plan, ensure_ascii=False), ex=ttl)

    # 更新各区域 current_officers 为 target (下一轮的"当前分配")
    pipe = redis.pipeline()
    for r in regions:
        rid = r["region_id"]
        r["current_officers"] = targets[rid]
        pipe.hset(_REGIONS_KEY, rid, json.dumps(r, ensure_ascii=False))
    await pipe.execute()

    logger.info(
        f"警力分配完成: {m} 区域, {total_officers} 人, "
        f"调动 {plan['summary']['total_movements']} 人, "
        f"评分 {plan['summary']['overall_score']}"
    )
    return plan


async def get_latest_plan() -> Optional[dict]:
    """读取最近一次分配方案."""
    redis = get_redis()
    raw = await redis.get(_PLAN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
