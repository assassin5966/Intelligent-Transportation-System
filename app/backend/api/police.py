"""警力管理 + 分配 API.

区域注册/删除, 总警力设置, 分配优化触发, 方案查询.
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis
from ..core.allocator import (
    _get_regions,
    _get_total_officers,
    get_latest_plan,
    optimize_allocation,
)
from ..core.realtime import get_device_crowd

router = APIRouter(prefix="/api/police", tags=["police"])

_REGIONS_KEY = f"{settings.redis_prefix}:police:regions"
_TOTAL_KEY = f"{settings.redis_prefix}:police:total"


class RegionIn(BaseModel):
    id: str
    name: str
    center_x: float
    center_y: float
    device_id: str  # 关联设备, 用于读取该区域在場人数


class RegionOut(BaseModel):
    id: str
    name: str
    center_x: float
    center_y: float
    device_id: str
    current_officers: int = 0
    current_persons: int = 0


class TotalIn(BaseModel):
    total: int  # 总警力数 N


@router.post("/regions", status_code=201)
async def register_region(region: RegionIn):
    """注册警力区域."""
    redis = get_redis()
    data = {
        "name": region.name,
        "center_x": region.center_x,
        "center_y": region.center_y,
        "device_id": region.device_id,
        "current_officers": 0,
    }
    await redis.hset(_REGIONS_KEY, region.id, json.dumps(data, ensure_ascii=False))
    logger.info(f"注册警力区域: {region.id} ({region.name})")
    return {"id": region.id, "status": "registered"}


@router.get("/regions")
async def list_regions():
    """区域列表 (含当前人数和警力)."""
    regions = await _get_regions()
    result = []
    for r in regions:
        crowd = await get_device_crowd(r.get("device_id", ""))
        result.append({
            "id": r["region_id"],
            "name": r.get("name", r["region_id"]),
            "center_x": r.get("center_x", 0),
            "center_y": r.get("center_y", 0),
            "device_id": r.get("device_id", ""),
            "current_officers": r.get("current_officers", 0),
            "current_persons": crowd["current_persons"],
        })
    return result


@router.delete("/regions/{region_id}")
async def delete_region(region_id: str):
    """删除警力区域."""
    redis = get_redis()
    exists = await redis.hexists(_REGIONS_KEY, region_id)
    if not exists:
        raise HTTPException(404, "region not found")
    await redis.hdel(_REGIONS_KEY, region_id)
    logger.info(f"删除警力区域: {region_id}")
    return {"status": "deleted", "id": region_id}


@router.post("/total")
async def set_total(req: TotalIn):
    """设置总警力数."""
    if req.total < 0:
        raise HTTPException(400, "total must be >= 0")
    redis = get_redis()
    await redis.set(_TOTAL_KEY, req.total)
    logger.info(f"总警力数设置为: {req.total}")
    return {"total": req.total, "status": "ok"}


@router.get("/allocation")
async def get_allocation():
    """当前分配状态 (各区域当前警力 + 当前人数)."""
    regions = await _get_regions()
    total = await _get_total_officers()
    result = []
    for r in regions:
        crowd = await get_device_crowd(r.get("device_id", ""))
        result.append({
            "region_id": r["region_id"],
            "name": r.get("name", r["region_id"]),
            "current_officers": r.get("current_officers", 0),
            "current_persons": crowd["current_persons"],
        })
    return {"total_officers": total, "regions": result}


@router.post("/optimize")
async def optimize():
    """手动触发警力分配优化, 返回分配方案."""
    plan = await optimize_allocation()
    if plan is None:
        raise HTTPException(400, "无注册区域或总警力为 0, 无法优化")
    return plan


@router.get("/plan")
async def get_plan():
    """获取最近一次自动分配方案."""
    plan = await get_latest_plan()
    if plan is None:
        raise HTTPException(404, "暂无分配方案, 请先调用 POST /api/police/optimize")
    return plan
