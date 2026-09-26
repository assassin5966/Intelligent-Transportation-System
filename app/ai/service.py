"""AI 分析服务入口.

启动: python -m app.ai.service
对外: 注册/启停摄像头管道, WebSocket实时推送, 每路拉流 -> 检测跟踪 -> 越线计数 -> 推送事件到后端.
"""
import asyncio
import json
from typing import Literal, Optional

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..common.config import settings
from ..common.logger import logger
from .counter import Point
from .mock_simulator import MockSimulator
from .model_pool import model_pool
from .pipeline import _WS_QUEUE_MAX, DevicePipeline, register_ws_client, unregister_ws_client

_pipelines: dict[str, DevicePipeline] = {}
_simulator: Optional[MockSimulator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _simulator
    logger.info(f"AI 分析服务启动 (端口 {settings.ai_port})")
    if settings.mock_enabled:
        # Mock 模式: 不拉流不推理, 由模拟器按真实规律生成事件 (验证前端/联调用)
        _simulator = MockSimulator()
        await _simulator.start()
        logger.info("[Mock] MOCK_ENABLED=true: 拉流/推理已禁用, 数据由模拟器生成")
    else:
        # 模型池在对外提供注册接口前完成加载/预热/自检:
        # 避免"首个设备注册时才建 CUDA context"引发的并发加载卡死与首帧毛刺
        await model_pool.initialize()
        snap = model_pool.snapshot()
        logger.info(
            f"推理模式={snap['mode']} ({snap['reason']}) "
            f"引擎={len(snap['engines'])} 卡 显存="
            + (", ".join(f"gpu{e['gpu_id']}:{e['vram_mb']}MB" for e in snap['engines']) or "无")
        )
    yield
    if _simulator is not None:
        await _simulator.stop()
        _simulator = None
    for p in list(_pipelines.values()):
        await p.stop()
    _pipelines.clear()
    await model_pool.shutdown()


app = FastAPI(title="AI 分析服务", version="0.1.0", lifespan=lifespan)


class DeviceRegister(BaseModel):
    device_id: str
    stream_url: str
    line: list[list[float]]
    anchor: Optional[list[float]] = None
    count_only: Optional[Literal["enter", "exit"]] = None  # None=双向, "enter"=只计Enter, "exit"=只计Exit
    camera_type: Optional[Literal["vehicle", "person"]] = None  # None=全部检测, vehicle/person
    roi: Optional[list[list[float]]] = None  # ROI 多边形顶点 [[x,y],...], >=3 个, 归一化
    gb_device_id: Optional[str] = None  # 国标设备ID (WVP 同步设备填写, 启用流地址自动刷新)
    gb_channel_id: Optional[str] = None  # 国标通道ID (WVP 同步设备填写)


@app.get("/health", tags=["system"])
async def health():
    mock = _simulator is not None
    infer = model_pool.snapshot()
    return {
        "status": "ok",
        "service": "ai",
        "active_devices": len(_simulator.device_ids) if mock else len(_pipelines),
        "mock": mock,
        # 推理模式与每卡负载 (运维可见): selftest_failed 表示已整体降级 legacy
        "infer": {
            "mode": "mock" if mock else infer["mode"],
            "reason": infer["reason"],
            "selftest_failed": infer["mode"] == "legacy" and infer["reason"].startswith("引擎自检"),
            "engines": infer["engines"],
        },
    }


@app.get("/devices", tags=["ai"])
async def list_devices():
    if _simulator is not None:
        # Mock 模式: 返回模拟设备列表 (无真实 pipeline)
        return [
            {"device_id": d, "stream_url": "", "running": True, "mock": True}
            for d in _simulator.device_ids
        ]
    return [
        {
            "device_id": d,
            "stream_url": p.stream_url,
            "running": p.running,
            "infer_mode": p.mode,
            "gpu_id": p.lease.gpu_id if p.lease is not None else None,
        }
        for d, p in _pipelines.items()
    ]


@app.post("/devices", status_code=201, tags=["ai"])
async def register(dev: DeviceRegister):
    if _simulator is not None:
        # Mock 模式兜底保护: 拒绝创建真实拉流管道, 防止与模拟器数据叠加双重计数
        raise HTTPException(409, "mock mode enabled: real pipelines are disabled (MOCK_ENABLED=true)")
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
    # 槽位分配: gpu_batch 模式下返回该路所属卡与槽位; legacy 模式返回 None
    lease = model_pool.acquire(dev.device_id)
    try:
        p = DevicePipeline(
            dev.device_id,
            dev.stream_url,
            line,
            anchor,
            dev.count_only,
            dev.camera_type,
            roi,
            enable_url_refresh=bool(dev.gb_device_id),
            lease=lease,
        )
    except Exception:
        model_pool.release(dev.device_id)  # 构造失败不留悬空槽位
        raise
    p.start()
    _pipelines[dev.device_id] = p
    return {
        "device_id": dev.device_id,
        "status": "started",
        "infer_mode": p.mode,
        "gpu_id": lease.gpu_id if lease is not None else None,
    }


@app.delete("/devices/{device_id}", tags=["ai"])
async def stop_device(device_id: str):
    p = _pipelines.pop(device_id, None)
    if p is None:
        raise HTTPException(404, "device not found")
    await p.stop()
    # 槽位归还模型池: 跟踪状态随设备销毁, 该卡设备集变化后由下一路复用
    model_pool.release(device_id)
    return {"device_id": device_id, "status": "stopped"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # 有界队列: 慢客户端积压到上限后由广播侧丢最旧, 防止内存无限增长
    queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
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