"""后端 WebSocket 端点: 向前端大屏推送实时统计和告警.

消息类型:
  - stats:       实时统计 (全局 data + 每设备 devices, 含拥挤状态/经纬度)
  - alert:       新触发告警
  - prediction:  预测结果更新
  - police_plan: 警力分配方案更新
  - /ws/tracks:  轨迹标注通道 (转发 AI 服务 tracks/crossing_event, 供大屏视频叠加)
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Set

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...common.config import settings
from ..core.realtime import get_stats
from .stats import build_all_device_stats_rows

router = APIRouter(tags=["websocket"])

# 所有已连接的 WebSocket 客户端队列
_ws_clients: Set[asyncio.Queue] = set()

# 统计推送间隔 (秒)
_STATS_INTERVAL = 2.0


def register_ws_client(queue: asyncio.Queue) -> None:
    _ws_clients.add(queue)


def unregister_ws_client(queue: asyncio.Queue) -> None:
    _ws_clients.discard(queue)


async def broadcast(message: dict) -> None:
    """向所有已连接的前端客户端广播消息."""
    for queue in list(_ws_clients):
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass  # 队列满, 丢弃旧消息


async def broadcast_alert(alert: dict) -> None:
    """告警触发时推送给所有前端客户端."""
    await broadcast({"type": "alert", "data": alert})


async def broadcast_prediction(prediction: dict) -> None:
    """预测结果更新时推送给所有前端客户端."""
    await broadcast({"type": "prediction", "data": prediction})


async def broadcast_police(plan: dict) -> None:
    """警力分配方案更新时推送给所有前端客户端."""
    await broadcast({"type": "police_plan", "data": plan})


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    register_ws_client(queue)

    # 连接时立即推送一次当前统计 (全局 + 每设备完整明细)
    try:
        stats = await get_stats()
        devices = await build_all_device_stats_rows()
        await websocket.send_text(json.dumps({
            "type": "stats",
            "data": stats,
            "devices": devices,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, default=str))
    except Exception:
        pass

    async def sender():
        """从队列取消息发送给客户端."""
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=_STATS_INTERVAL)
                await websocket.send_text(json.dumps(message, default=str))
            except asyncio.TimeoutError:
                # 超时无消息 -> 推送最新统计 (全局 + 每设备完整明细)
                try:
                    stats = await get_stats()
                    devices = await build_all_device_stats_rows()
                    await websocket.send_text(json.dumps({
                        "type": "stats",
                        "data": stats,
                        "devices": devices,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, default=str))
                except Exception:
                    pass
            except WebSocketDisconnect:
                break

    async def receiver():
        """接收客户端消息 (心跳/订阅, 保持连接)."""
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break

    try:
        await asyncio.gather(sender(), receiver())
    finally:
        unregister_ws_client(queue)


@router.websocket("/ws/tracks")
async def tracks_ws_endpoint(websocket: WebSocket):
    """轨迹标注通道: 按需转发 AI 分析服务的 tracks/crossing_event 给大屏.

    保持「前端只连业务后端 8000」的架构约定: 前端连本端点, 后端开一条到
    AI 服务 /ws 的上游连接做透传. 打开视频面板时才建立连接, 关闭即断开,
    无常驻转发任务; AI 未启动/不可达时向前端发 error 消息便于提示.
    """
    await websocket.accept()
    ai_ws_url = settings.ai_service_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    try:
        upstream = await websockets.connect(ai_ws_url, open_timeout=5)
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "error", "message": f"AI 分析服务不可达 ({ai_ws_url}): {e}"}))
        await websocket.close()
        return

    stop = asyncio.Event()

    async def pump():
        """上游 AI -> 前端: 原样透传消息 (tracks/crossing_event 已是前端所需 JSON)."""
        try:
            async for msg in upstream:
                await websocket.send_text(msg)
        except Exception:
            pass
        finally:
            stop.set()

    async def receiver():
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()

    pump_task = asyncio.create_task(pump())
    try:
        await receiver()
    finally:
        stop.set()
        pump_task.cancel()
        try:
            await upstream.close()
        except Exception:
            pass
