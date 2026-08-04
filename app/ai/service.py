"""AI 分析服务入口.

启动: python -m app.ai.service
对外: 注册/启停摄像头管道, WebSocket实时推送, 每路拉流 -> 检测跟踪 -> 越线计数 -> 推送事件到后端.
"""
import asyncio
import json
from typing import Optional

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..common.config import settings
from ..common.logger import logger
from .counter import Point
from .pipeline import DevicePipeline, register_ws_client, unregister_ws_client

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
    line: list[list[float]]
    anchor: Optional[list[float]] = None
    count_only: Optional[str] = None  # None=双向, "enter"=只计Enter, "exit"=只计Exit
    camera_type: Optional[str] = None  # None=全部检测, "vehicle"=只检测机动车, "person"=只检测人流(含非机动车)
    roi: Optional[list[list[float]]] = None  # ROI 多边形顶点 [[x,y],...], >=3 个, 归一化
    gb_device_id: Optional[str] = None  # 国标设备ID (WVP 同步设备填写, 启用流地址自动刷新)
    gb_channel_id: Optional[str] = None  # 国标通道ID (WVP 同步设备填写)


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
    anchor: Optional[Point] = None
    if dev.anchor and len(dev.anchor) == 2:
        anchor = (float(dev.anchor[0]), float(dev.anchor[1]))
    roi: Optional[list[Point]] = None
    if dev.roi is not None:
        if len(dev.roi) < 3:
            raise HTTPException(400, "roi must have >=3 points [[x,y],...]")
        roi = [(float(p[0]), float(p[1])) for p in dev.roi]
    p = DevicePipeline(
        dev.device_id,
        dev.stream_url,
        line,
        anchor,
        dev.count_only,
        dev.camera_type,
        roi,
        enable_url_refresh=bool(dev.gb_device_id),
    )
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    register_ws_client(queue)
    logger.info("WebSocket 客户端已连接")
    
    async def receiver():
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    
    async def sender():
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
                await websocket.send_text(json.dumps(message))
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                break
    
    try:
        await asyncio.gather(receiver(), sender())
    finally:
        unregister_ws_client(queue)
        logger.info("WebSocket 客户端已断开")


if __name__ == "__main__":
    uvicorn.run("app.ai.service:app", host="0.0.0.0", port=settings.ai_port)