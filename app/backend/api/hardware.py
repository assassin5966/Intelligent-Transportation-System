"""硬件监控 API — 代理 AI 分析服务的 /hardware 采集端点.

数据在 AI 容器内采集 (nvidia-smi + psutil), 容器 /proc 即宿主机视角;
AI 服务离线时返回 ai_online=false, 前端渲染离线态而不报错.
"""
from __future__ import annotations

from fastapi import APIRouter
import httpx

from ...common.config import settings

router = APIRouter(prefix="/api/hardware", tags=["hardware"])


@router.get("")
async def hardware_snapshot():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ai_service_url}/hardware")
            resp.raise_for_status()
            data = resp.json()
            data["ai_online"] = True
            return data
    except Exception as e:  # noqa: BLE001
        return {"ai_online": False, "error": str(e), "ts": 0}
