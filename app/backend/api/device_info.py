"""设备点位信息运维 API (名称/经纬度/分类 增删改查).

独立于展示链路: 仅运维页面 (static/device-info.html) 调用, 管理 MySQL
device_info 表 (设备信息已从 data/device_geo.json / data/device_category.json
迁入; 云冈类设备暂不入库). 展示链路读取同一内存缓存, CRUD 后立即生效.

接口:
    GET    /api/device-info          设备信息列表 (运维表格)
    POST   /api/device-info          新增设备信息 (name 唯一, 去空格匹配)
    PUT    /api/device-info/{name}   更新设备信息 (未传字段保留原值)
    DELETE /api/device-info/{name}   删除设备信息
MySQL 未启用 (MYSQL_ENABLED=false) 时返回 503.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...common import device_info
from ...common.config import settings
from ...common.logger import logger

router = APIRouter(prefix="/api/device-info", tags=["device-info"])


def _require_mysql() -> None:
    if not settings.mysql_enabled:
        raise HTTPException(503, "MySQL 未启用 (MYSQL_ENABLED=false), 无法管理设备信息")


class DeviceInfoIn(BaseModel):
    """新增设备信息."""
    name: str = Field(..., min_length=1, max_length=255, description="设备名称 (唯一, 匹配时忽略空格)")
    point_id: Optional[str] = Field(None, max_length=64, description="点位编号 (如 GAJK-2648)")
    category: Optional[str] = Field(None, max_length=64, description="点位分类")
    longitude: Optional[float] = Field(None, ge=-180, le=180, description="经度")
    latitude: Optional[float] = Field(None, ge=-90, le=90, description="纬度")
    status: Optional[str] = Field(None, max_length=16, description="验证状态 (已验证/待验证)")
    region: Optional[str] = Field(None, max_length=64, description="区域 (如 大同古城)")


class DeviceInfoUpdate(BaseModel):
    """更新设备信息 (未传字段保留原值)."""
    point_id: Optional[str] = Field(None, max_length=64)
    category: Optional[str] = Field(None, max_length=64)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    status: Optional[str] = Field(None, max_length=16)
    region: Optional[str] = Field(None, max_length=64)


@router.get("")
async def list_info():
    """设备信息列表 (运维页面表格)."""
    _require_mysql()
    try:
        return await device_info.list_all()
    except Exception as e:  # noqa: BLE001
        logger.error(f"查询设备信息列表失败: {e}")
        raise HTTPException(503, f"MySQL 查询失败: {e}") from e


@router.post("", status_code=201)
async def create_info(body: DeviceInfoIn):
    """新增设备信息 (name 唯一, 同名覆盖)."""
    _require_mysql()
    if device_info.is_yungang(body.name, body.point_id):
        raise HTTPException(400, "云冈类设备暂不支持入库")
    try:
        norm = await device_info.upsert(
            body.name,
            point_id=body.point_id,
            category=body.category,
            longitude=body.longitude,
            latitude=body.latitude,
            status=body.status,
            region=body.region,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error(f"新增设备信息失败: {e}")
        raise HTTPException(503, f"MySQL 写入失败: {e}") from e
    await _trigger_wvp_sync()
    return {"status": "created", "name": norm}


@router.put("/{name}")
async def update_info(name: str, body: DeviceInfoUpdate):
    """更新设备信息 (未传字段保留原值)."""
    _require_mysql()
    try:
        existing = await device_info.fetch(name)
    except Exception as e:  # noqa: BLE001
        logger.error(f"查询设备信息失败: {e}")
        raise HTTPException(503, f"MySQL 查询失败: {e}") from e
    if not existing:
        raise HTTPException(404, f"设备信息不存在: {name}")
    try:
        norm = await device_info.upsert(
            name,
            point_id=body.point_id if body.point_id is not None else existing["point_id"],
            category=body.category if body.category is not None else existing["category"],
            longitude=body.longitude if body.longitude is not None else existing["longitude"],
            latitude=body.latitude if body.latitude is not None else existing["latitude"],
            status=body.status if body.status is not None else existing["status"],
            region=body.region if body.region is not None else existing.get("region"),
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"更新设备信息失败: {e}")
        raise HTTPException(503, f"MySQL 写入失败: {e}") from e
    await _trigger_wvp_sync()
    return {"status": "updated", "name": norm}


@router.delete("/{name}")
async def delete_info(name: str):
    """删除设备信息."""
    _require_mysql()
    try:
        deleted = await device_info.delete(name)
    except Exception as e:  # noqa: BLE001
        logger.error(f"删除设备信息失败: {e}")
        raise HTTPException(503, f"MySQL 删除失败: {e}") from e
    if not deleted:
        raise HTTPException(404, f"设备信息不存在: {device_info.normalize(name)}")
    await _trigger_wvp_sync()
    return {"status": "deleted", "name": device_info.normalize(name)}


async def _trigger_wvp_sync() -> None:
    """设备信息注册/变更后及时触发一次 WVP 同步 (后台任务, 不阻塞响应).

    新注册设备若与 WVP 在线通道同名, 同步后即入 Redis 设备表并可按需启流;
    未启用 WVP 或同步失败均静默, 不影响 CRUD 响应.
    """
    if not settings.wvp_enabled:
        return
    import asyncio

    async def _run() -> None:
        try:
            from ..core.wvp_sync import sync_once

            await sync_once()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"设备信息变更后 WVP 同步失败: {e}")

    asyncio.create_task(_run())
