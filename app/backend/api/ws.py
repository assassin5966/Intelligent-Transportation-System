"""后端 WebSocket 端点: 向前端大屏推送实时统计和告警.

消息类型:
  - stats:       实时统计 (当前车辆/人员/今日累计/活跃设备)
  - alert:       新触发告警
  - prediction:  预测结果更新
  - police_plan: 警力分配方案更新
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.realtime import get_all_device_stats, get_stats

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

    # 连接时立即推送一次当前统计 (含按设备明细)
    try:
        stats = await get_stats()
        devices = await get_all_device_stats()
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
                # 超时无消息 -> 推送最新统计 (含按设备明细)
                try:
                    stats = await get_stats()
                    devices = await get_all_device_stats()
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
