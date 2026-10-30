"""路口数据卡配置 API (路口信息 + 数据卡样式 增删改查).

供 static/intersection-config.html 调用, 管理 MySQL intersection_info 表;
后续用路口数据卡给设备信息做路口维度分类.

接口:
    GET    /api/intersections          路口列表 (含 card_style)
    POST   /api/intersections          新增 (name 唯一)
    PUT    /api/intersections/{iid}    更新 (未传字段保留原值)
    DELETE /api/intersections/{iid}    删除
MySQL 未启用 (MYSQL_ENABLED=false) 时返回 503.
"""
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...common import intersection_info
from ...common.config import settings
from ...common.logger import logger

router = APIRouter(prefix="/api/intersections", tags=["intersections"])

LaneCount = Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
LaneType = Literal["single", "dual"]
BadgeType = Literal["入口", "出口", "出入口"]
Orientation = Literal["EW", "NS"]
Rotation = Literal[0, 90, 180, 270]


def _require_mysql() -> None:
    if not settings.mysql_enabled:
        raise HTTPException(503, "MySQL 未启用 (MYSQL_ENABLED=false), 无法管理路口信息")


async def _ensure_table() -> None:
    """确保 intersection_info 表可用 (建表 + 空表种子导入, 幂等; 后端早于 MySQL 就绪时自愈)."""
    try:
        await intersection_info.ensure_ready()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"intersection_info 表自愈失败(将按原逻辑报错): {e}")


class IntersectionIn(BaseModel):
    """新增路口."""
    name: str = Field(..., min_length=1, max_length=255, description="路口名称 (唯一)")
    lane_count: LaneCount = Field(4, description="车道数量 (1~10)")
    lane_width: Optional[float] = Field(None, ge=1, le=20, description="车道宽度 (米), 示意图车道宽度联动")
    lane_type: LaneType = Field("dual", description="车道类型 (single=单向, dual=双向)")
    direction: Optional[str] = Field(None, max_length=16, description="行驶方向描述 (如 向北)")
    badge: Optional[BadgeType] = Field(None, description="出入口分类 (入口/出口/出入口)")
    orientation: Orientation = Field("EW", description="方向样式 (EW=东西方向/NS=南北方向)")
    rotation: Rotation = Field(0, description="数据卡旋转角度 (0/90/180/270)")
    scenic: Optional[str] = Field(None, description="所属景区 (大同古城/云冈石窟/悬空寺)")
    longitude: Optional[float] = Field(None, ge=-180, le=180, description="经度")
    latitude: Optional[float] = Field(None, ge=-90, le=90, description="纬度")
    card_style: Optional[dict] = Field(None, description="数据卡样式 (车道顺序/车道方向/便道/计数线/徽章等)")
    remark: Optional[str] = Field(None, max_length=255, description="备注")
    hidden: bool = Field(False, description="数据卡是否在大屏隐藏 (true=隐藏/false=显示)")


class IntersectionUpdate(BaseModel):
    """更新路口 (未传字段保留原值)."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    lane_count: Optional[LaneCount] = None
    lane_width: Optional[float] = Field(None, ge=1, le=20, description="车道宽度 (米)")
    lane_type: Optional[LaneType] = None
    direction: Optional[str] = Field(None, max_length=16)
    badge: Optional[BadgeType] = None
    orientation: Optional[Orientation] = None
    rotation: Optional[Rotation] = None
    scenic: Optional[str] = Field(None, description="所属景区 (大同古城/云冈石窟/悬空寺)")
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    card_style: Optional[dict] = None
    remark: Optional[str] = Field(None, max_length=255)
    hidden: Optional[bool] = Field(None, description="数据卡是否在大屏隐藏 (true=隐藏/false=显示)")


@router.get("")
async def list_intersections():
    """路口列表 (配置页表格)."""
    _require_mysql()
    await _ensure_table()
    try:
        return await intersection_info.list_all()
    except Exception as e:  # noqa: BLE001
        logger.error(f"查询路口列表失败: {e}")
        raise HTTPException(503, f"MySQL 查询失败: {e}") from e


@router.get("/scenic-options")
async def list_scenic_options():
    """全局景区选项 (运维工具各页面右上角的统一查询条件).

    选项 = 基线 (大同古城/云冈石窟/悬空寺) ∪ device_info.region ∪ intersection_info.scenic。
    设备信息表的「区域」与路口的「景区」在库里各存各列, 前端统一展示为「景区」下拉。
    """
    _require_mysql()
    await _ensure_table()
    try:
        return await intersection_info.scenic_options()
    except Exception as e:  # noqa: BLE001
        logger.error(f"查询全局景区选项失败: {e}")
        raise HTTPException(503, f"MySQL 查询失败: {e}") from e


@router.get("/{iid}")
async def get_intersection(iid: int):
    """路口详情."""
    _require_mysql()
    await _ensure_table()
    try:
        row = await intersection_info.fetch(iid)
    except Exception as e:  # noqa: BLE001
        logger.error(f"查询路口失败: {e}")
        raise HTTPException(503, f"MySQL 查询失败: {e}") from e
    if not row:
        raise HTTPException(404, f"路口不存在: id={iid}")
    return row


@router.post("", status_code=201)
async def create_intersection(body: IntersectionIn):
    """新增路口 (name 唯一)."""
    _require_mysql()
    await _ensure_table()
    try:
        iid = await intersection_info.create(
            body.name,
            lane_count=body.lane_count,
            lane_width=body.lane_width,
            lane_type=body.lane_type,
            direction=body.direction,
            badge=body.badge,
            orientation=body.orientation,
            rotation=body.rotation,
            scenic=body.scenic,
            longitude=body.longitude,
            latitude=body.latitude,
            card_style=body.card_style,
            remark=body.remark,
            hidden=body.hidden,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error(f"新增路口失败: {e}")
        raise HTTPException(503, f"MySQL 写入失败: {e}") from e
    return {"status": "created", "id": iid, "name": body.name.strip()}


@router.put("/{iid}")
async def update_intersection(iid: int, body: IntersectionUpdate):
    """更新路口 (未传字段保留原值)."""
    _require_mysql()
    await _ensure_table()
    fields = body.model_dump(exclude_unset=True)
    try:
        ok = await intersection_info.update(iid, fields)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error(f"更新路口失败: {e}")
        raise HTTPException(503, f"MySQL 写入失败: {e}") from e
    if not ok:
        raise HTTPException(404, f"路口不存在: id={iid}")
    return {"status": "updated", "id": iid}


@router.delete("/{iid}")
async def delete_intersection(iid: int):
    """删除路口."""
    _require_mysql()
    await _ensure_table()
    try:
        ok = await intersection_info.delete(iid)
    except Exception as e:  # noqa: BLE001
        logger.error(f"删除路口失败: {e}")
        raise HTTPException(503, f"MySQL 删除失败: {e}") from e
    if not ok:
        raise HTTPException(404, f"路口不存在: id={iid}")
    return {"status": "deleted", "id": iid}
