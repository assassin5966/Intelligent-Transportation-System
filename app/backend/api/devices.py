"""设备管理 API (注册/列表/删除, 含越线计数线配置)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.db import get_session
from ...common.models import Device
from ..crud import delete_device, list_devices, upsert_device

router = APIRouter(prefix="/api/devices", tags=["devices"])


class DeviceIn(BaseModel):
    id: str
    name: str
    stream_url: str
    line_coords: str | None = None  # JSON, e.g. "[[x1,y1],[x2,y2]]"


@router.get("")
async def list_(session: AsyncSession = Depends(get_session)):
    return await list_devices(session)


@router.post("", status_code=201)
async def register(
    dev: DeviceIn, session: AsyncSession = Depends(get_session)
):
    device = Device(
        id=dev.id,
        name=dev.name,
        stream_url=dev.stream_url,
        line_coords=dev.line_coords,
        status="registered",
    )
    saved = await upsert_device(session, device)
    return {"id": saved.id, "status": saved.status}


@router.delete("/{device_id}")
async def remove(device_id: str, session: AsyncSession = Depends(get_session)):
    ok = await delete_device(session, device_id)
    if not ok:
        raise HTTPException(404, "device not found")
    return {"status": "deleted", "id": device_id}
