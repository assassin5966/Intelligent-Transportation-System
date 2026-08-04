"""预测定时调度: 每 N 分钟做一次总人数预测, 缓存到 Redis.

预测逻辑:
  1. 读取历史 30 个 N 分钟区间的人流/车流序列
  2. 车流转人流: 每辆车 random(2, 5) 人
  3. 总人数 = 人流 + 转化后车流
  4. 喂入 Chronos-2 预测下一个 N 分钟
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from ..common.config import settings
from ..common.logger import logger
from ..common.redis_client import get_redis
from .router import predict_total_persons

_task: Optional[asyncio.Task] = None


async def _run_once() -> None:
    """执行一次总人数预测, 缓存结果到 Redis, 并评估预测告警."""
    redis = get_redis()
    try:
        result = await predict_total_persons()
        payload = json.dumps(result, ensure_ascii=False)
        await redis.set(
            f"{settings.redis_prefix}:prediction:latest:total",
            payload,
            ex=settings.prediction_interval_minutes * 60 * 4,  # 缓存 4 个区间
        )
        logger.info(
            f"预测已更新: 预计下 {settings.prediction_interval_minutes} 分钟总人数 "
            f"{result['predicted_total']} (序列长度 {result['series_length']}, "
            f"降级={'是' if result.get('degraded') else '否'})"
        )

        # 预测结果通过 WebSocket 推送给前端
        try:
            from ..backend.api.ws import broadcast_prediction
            await broadcast_prediction(result)
        except Exception:  # noqa: BLE001
            pass

        # 预测告警联动: 预测值超阈值时触发提前预警
        try:
            from ..backend.core.alerts import evaluate_prediction
            await evaluate_prediction(result)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"预测告警评估失败: {e}")

    except Exception as e:  # noqa: BLE001
        logger.error(f"总人数预测失败: {e}")


async def _loop() -> None:
    """每 N 分钟执行一次预测."""
    interval_seconds = settings.prediction_interval_minutes * 60
    while True:
        await _run_once()
        await asyncio.sleep(interval_seconds)


async def start_scheduler() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())
        logger.info(
            f"预测调度器已启动 (每 {settings.prediction_interval_minutes} 分钟一次, "
            f"序列长度 {settings.prediction_series_length})"
        )


async def stop_scheduler() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        logger.info("预测调度器已停止")
