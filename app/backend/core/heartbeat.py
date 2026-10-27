"""摄像头离线检测: 定期检查设备心跳, 超时标记离线并触发告警.

AI 服务每 30 秒发送一次心跳; 后端每 30 秒检查一次, 超过 90 秒无心跳视为离线.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis

_task: Optional[asyncio.Task] = None

# 离线判定阈值 (秒): 3 个心跳周期无响应
_OFFLINE_THRESHOLD = 90
# 检查间隔 (秒)
_CHECK_INTERVAL = 30

_DEVICE_KEY_PREFIX = f"{settings.redis_prefix}:device:"
_ALERTS_KEY = f"{settings.redis_prefix}:alerts"
_ALERT_SEQ_KEY = f"{settings.redis_prefix}:alert:seq"
_ALERT_MAX = 1000


async def _check_devices() -> None:
    """检查所有注册设备的心跳, 标记离线/在线并触发告警."""
    redis = get_redis()
    now = datetime.now(timezone.utc)
    found = []
    async for key in redis.scan_iter(f"{_DEVICE_KEY_PREFIX}*"):
        data = await redis.hgetall(key)
        if data:
            found.append(data)

    for dev in found:
        device_id = dev.get("id", "")
        if not device_id:
            continue

        last_hb_raw = dev.get("last_heartbeat", "")
        status = dev.get("status", "registered")

        # 未收到过心跳的新设备不判定离线
        if not last_hb_raw:
            continue

        try:
            last_hb = datetime.fromisoformat(last_hb_raw)
        except ValueError:
            continue

        offline_seconds = (now - last_hb).total_seconds()

        if offline_seconds > _OFFLINE_THRESHOLD and status != "offline":
            # 标记离线
            await redis.hset(_DEVICE_KEY_PREFIX + device_id, "status", "offline")
            await _trigger_offline_alert(device_id, offline_seconds)
            logger.warning(f"[离线] 设备 {device_id} 已离线 ({offline_seconds:.0f}s 无心跳)")

        elif offline_seconds <= _OFFLINE_THRESHOLD and status == "offline":
            # 恢复在线
            await redis.hset(_DEVICE_KEY_PREFIX + device_id, "status", "online")
            logger.info(f"[恢复] 设备 {device_id} 已恢复在线")


async def _trigger_offline_alert(device_id: str, offline_seconds: float) -> None:
    """触发摄像头离线告警 (5 分钟去重)."""
    redis = get_redis()
    dedup_key = f"{settings.redis_prefix}:alert:device_offline:{device_id}"
    if not await redis.set(dedup_key, "1", ex=300, nx=True):
        return  # 5 分钟内已告警过, 跳过

    alert = {
        "rule_id": f"device_offline_{device_id}",
        "level": "critical",
        "category": "device_offline",
        "message": f"摄像头 {device_id} 离线 ({offline_seconds:.0f}s 无心跳)",
        "value": offline_seconds,
        "threshold": _OFFLINE_THRESHOLD,
        "device_id": device_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    alert_id = await redis.incr(_ALERT_SEQ_KEY)
    alert["id"] = alert_id
    pipe = redis.pipeline()
    pipe.lpush(_ALERTS_KEY, json.dumps(alert))
    pipe.ltrim(_ALERTS_KEY, 0, _ALERT_MAX - 1)
    await pipe.execute()

    # WebSocket 推送
    try:
        from ..api.ws import broadcast_alert
        await broadcast_alert(alert)
    except Exception:  # noqa: BLE001
        pass


async def _loop() -> None:
    while True:
        try:
            await _check_devices()
        except Exception as e:  # noqa: BLE001
            logger.error(f"心跳检查异常: {e}")
        await asyncio.sleep(_CHECK_INTERVAL)


async def start_checker() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())
        logger.info(f"摄像头离线检测已启动 (每 {_CHECK_INTERVAL}s 检查, 离线阈值 {_OFFLINE_THRESHOLD}s)")


async def stop_checker() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        logger.info("摄像头离线检测已停止")
