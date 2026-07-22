"""AI 分析服务入口.

启动: python -m app.ai.service
对外: 注册/启停摄像头管道, 每路拉流 -> 检测跟踪 -> 越线计数 -> 推送事件到后端.
"""
import asyncio

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..common.config import settings
from ..common.logger import logger
from .counter import Point
from .pipeline import DevicePipeline

_pipelines: dict[str, DevicePipeline] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"AI 分析服务启动 (端口 {settings.ai_port})")
    yield
    for p in list(_pipelines.values()):
        await p.stop()
    _pipelines.clear()


app = FastAPI(title="AI 分析服务", version="0.1.0", lifespan=lifespan)


class DeviceRegister(BaseModel):
    device_id: str
    stream_url: str
    line: list[list[float]]  # [[x1, y1], [x2, y2]] 计数线两端点


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "service": "ai", "active_devices": len(_pipelines)}


@app.get("/devices", tags=["ai"])
async def list_devices():
    return [
        {"device_id": d, "stream_url": p.stream_url, "running": p.running}
        for d, p in _pipelines.items()
    ]


@app.post("/devices", status_code=201, tags=["ai"])
async def register(dev: DeviceRegister):
    if dev.device_id in _pipelines:
        raise HTTPException(409, "device already running")
    if len(dev.line) != 2:
        raise HTTPException(400, "line must be 2 points [[x1,y1],[x2,y2]]")
    line: tuple[Point, Point] = (
        (float(dev.line[0][0]), float(dev.line[0][1])),
        (float(dev.line[1][0]), float(dev.line[1][1])),
    )
    p = DevicePipeline(dev.device_id, dev.stream_url, line)
    p.start()
    _pipelines[dev.device_id] = p
    return {"device_id": dev.device_id, "status": "started"}


@app.delete("/devices/{device_id}", tags=["ai"])
async def stop_device(device_id: str):
    p = _pipelines.pop(device_id, None)
    if p is None:
        raise HTTPException(404, "device not found")
    await p.stop()
    return {"device_id": device_id, "status": "stopped"}


if __name__ == "__main__":
    uvicorn.run("app.ai.service:app", host="0.0.0.0", port=settings.ai_port)
