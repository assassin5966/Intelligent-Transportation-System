"""GPS 实时位置接入 (Apache Pulsar 消费) — 对接《实时位置数据接入规范 V1.0》.

数据流::

    Pulsar(location_realtime_v1) --消费--> 解析校验 --> Redis(最新位置) --> API / SSE 推送

- 消费端使用 pulsar-client 同步 API, 跑在独立守护线程, 不阻塞 FastAPI 事件循环.
- 每条消息按规范 JSON 字段解析为 LocationPoint, 写入 Redis:
      sc:location:device:{deviceId} = JSON(最新位置), TTL = location_ttl_seconds
      sc:location:devices          = Set<deviceId> (供列表查询)
- 同时 publish 到频道 sc:location:updates, 供 /api/location/stream (SSE) 实时推送.

说明: pulsar 客户端在此模块内**懒加载** (start_consumer 内部 import),
避免在 pulsar-client 未安装且 pulsar_enabled=false 时拖累后端启动.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from ...common.config import settings
from ...common.logger import logger

# ---------------------------------------------------------------------------
# Redis key 约定
# ---------------------------------------------------------------------------
_LOCATION_DEVICE_PREFIX = f"{settings.redis_prefix}:location:device:"
_LOCATION_DEVICES_KEY = f"{settings.redis_prefix}:location:devices"
_LOCATION_UPDATES_CHANNEL = f"{settings.redis_prefix}:location:updates"

_DEVICE_TYPES = {"HANDHELD_RADIO", "BODY_CAMERA", "MOTORCYCLE_PATROL"}


# ---------------------------------------------------------------------------
# 数据模型 (规范 1. 数据结构描述; 坐标 WGS84)
# ---------------------------------------------------------------------------
class LocationPoint(BaseModel):
    """实时位置消息 (规范 V1.0). 除 receivedAt 外均为规范字段."""

    id: Optional[int] = Field(None, description="主键唯一值")
    deviceId: str = Field(..., description="设备编号")
    deviceType: Optional[str] = Field(None, description="设备类型")
    longitude: float = Field(..., description="经度 WGS84")
    latitude: float = Field(..., description="纬度 WGS84")
    speed: Optional[float] = Field(None, description="速度 m/s")
    heading: Optional[float] = Field(None, description="方向(正北0,顺时针)")
    altitude: Optional[float] = Field(None, description="海拔 m")
    positioningTime: Optional[str] = Field(None, description="定位时间 yyyy-MM-dd HH:mm:ss")
    extension: Optional[str] = Field(None, description="扩展信息")
    # 接入层补充 (非规范字段)
    receivedAt: Optional[str] = Field(None, description="后端接收时间 ISO8601(UTC)")


# ---------------------------------------------------------------------------
# Pulsar 消费 (同步客户端, 独立线程)
# ---------------------------------------------------------------------------
_consumer_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_pulsar_client = None
_pulsar_consumer = None
_sync_redis = None


def _get_sync_redis():
    """消费线程内使用的同步 Redis 客户端 (避免在后台线程 await 异步客户端)."""
    global _sync_redis
    if _sync_redis is None:
        import redis as redis_sync

        _sync_redis = redis_sync.from_url(settings.redis_url, decode_responses=True)
    return _sync_redis


def _safe_ack(msg) -> None:
    try:
        if _pulsar_consumer is not None:
            _pulsar_consumer.acknowledge(msg)
    except Exception:  # noqa: BLE001
        pass


def _handle_message(msg) -> None:
    """处理一条 Pulsar 消息: 解码 -> 校验 -> 写 Redis + 发布更新."""
    try:
        text = msg.data().decode("utf-8")
    except Exception:  # noqa: BLE001
        logger.warning("[Location] 消息非 UTF-8, 跳过")
        _safe_ack(msg)
        return
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"[Location] 消息非 JSON, 跳过: {text[:200]}")
        _safe_ack(msg)
        return
    try:
        point = LocationPoint(**raw)
    except ValidationError as e:
        logger.warning(f"[Location] 字段校验失败, 跳过: {e}")
        _safe_ack(msg)
        return

    point.receivedAt = datetime.now(timezone.utc).isoformat()
    payload = point.model_dump_json()

    try:
        r = _get_sync_redis()
        device_key = _LOCATION_DEVICE_PREFIX + point.deviceId
        r.set(device_key, payload, ex=settings.location_ttl_seconds)
        r.sadd(_LOCATION_DEVICES_KEY, point.deviceId)
        r.publish(_LOCATION_UPDATES_CHANNEL, payload)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Location] 写入 Redis 失败: {e}")
    _safe_ack(msg)


def _consume_loop() -> None:
    logger.info("[Location] Pulsar 消费线程启动")
    while not _stop_event.is_set():
        try:
            msg = _pulsar_consumer.receive(timeout_millis=1000)
        except Exception as e:  # noqa: BLE001
            # 连接异常/超时: 打印一次并继续 (pulsar-client 自带重连)
            logger.debug(f"[Location] 接收消息异常(将重试): {e}")
            continue
        if msg is not None:
            _handle_message(msg)
    logger.info("[Location] Pulsar 消费线程退出")


def start_consumer() -> bool:
    """启动 Pulsar 消费者 (独立线程). 返回是否成功启动.

    pulsar_enabled=false 或连接失败时不抛异常, 仅记录日志, 不影响后端启动.
    """
    global _consumer_thread, _pulsar_client, _pulsar_consumer
    if not settings.pulsar_enabled:
        logger.info("[Location] pulsar_enabled=false, 跳过 Pulsar 位置消费")
        return False
    if _consumer_thread and _consumer_thread.is_alive():
        return True
    _stop_event.clear()
    # 懒加载 pulsar, 避免未安装时影响后端启动
    import pulsar  # noqa: WPS433

    try:
        auth = pulsar.AuthenticationToken(settings.pulsar_token) if settings.pulsar_token else None
        _pulsar_client = pulsar.Client(
            settings.pulsar_service_url,
            authentication=auth,
        )
        _pulsar_consumer = _pulsar_client.subscribe(
            settings.pulsar_topic,
            settings.pulsar_subscription,
            consumer_type=pulsar.ConsumerType.Shared,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Location] Pulsar 客户端初始化失败, 位置接入未启用: {e}")
        return False
    _consumer_thread = threading.Thread(
        target=_consume_loop, name="gps-location-consumer", daemon=True
    )
    _consumer_thread.start()
    logger.info(
        f"[Location] 已订阅 Pulsar 主题 {settings.pulsar_topic} "
        f"(订阅={settings.pulsar_subscription}, Shared)"
    )
    return True


def stop_consumer() -> None:
    """停止消费者并释放连接."""
    _stop_event.set()
    global _pulsar_consumer, _pulsar_client
    try:
        if _pulsar_consumer is not None:
            _pulsar_consumer.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if _pulsar_client is not None:
            _pulsar_client.close()
    except Exception:  # noqa: BLE001
        pass
    _pulsar_consumer = None
    _pulsar_client = None


# ---------------------------------------------------------------------------
# 读取 (异步, 供 API / SSE 使用)
# ---------------------------------------------------------------------------
def _decode(v) -> str:
    return v if isinstance(v, str) else v.decode("utf-8", "ignore")


async def _device_ids() -> list[str]:
    from ...common.redis_client import get_redis

    redis = get_redis()
    raw_ids = await redis.smembers(_LOCATION_DEVICES_KEY)
    return [_decode(i) for i in raw_ids]


async def get_location(device_id: str) -> Optional[LocationPoint]:
    """读取单个设备最新位置; 不存在返回 None."""
    from ...common.redis_client import get_redis

    redis = get_redis()
    raw = await redis.get(_LOCATION_DEVICE_PREFIX + device_id)
    if not raw:
        return None
    try:
        return LocationPoint(**json.loads(_decode(raw)))
    except (json.JSONDecodeError, ValidationError):
        return None


async def get_all_locations(device_type: Optional[str] = None) -> list[LocationPoint]:
    """读取所有设备最新位置; device_type 非空时按设备类型过滤."""
    ids = await _device_ids()
    if not ids:
        return []
    from ...common.redis_client import get_redis

    redis = get_redis()
    pipe = redis.pipeline()
    for did in ids:
        pipe.get(_LOCATION_DEVICE_PREFIX + did)
    raw_list = await pipe.execute()

    out: list[LocationPoint] = []
    for raw in raw_list:
        if not raw:
            continue
        try:
            point = LocationPoint(**json.loads(_decode(raw)))
        except (json.JSONDecodeError, ValidationError):
            continue
        if device_type and point.deviceType != device_type:
            continue
        out.append(point)
    return out
