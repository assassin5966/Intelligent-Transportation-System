"""预测定时调度: 每小时对 vehicle/person 各做一次预测, 缓存到 Redis."""
import asyncio
import json
from datetime import datetime

from ..common.config import settings
from ..common.db import AsyncSessionLocal
from ..common.logger import logger
from ..common.redis_client import get_redis
from .chronos_model import ChronosPredictor
from .repository import load_history

_task: asyncio.Task | None = None


async def _run_once() -> None:
    async with AsyncSessionLocal() as session:
        redis = get_redis()
        for metric in ("vehicle", "person"):
            try:
                history = await load_history(session, metric)
                if not history:
                    continue
                forecast = ChronosPredictor.instance().predict(
                    history, settings.prediction_horizon
                )
                payload = json.dumps(
                    {
                        "metric": metric,
                        "horizon": settings.prediction_horizon,
                        "forecast": forecast,
                        "generated_at": datetime.utcnow().isoformat(),
                    }
                )
                await redis.set(
                    f"{settings.redis_prefix}:prediction:latest:{metric}",
                    payload,
                    ex=7200,
                )
                logger.info(
                    f"预测已更新: {metric} horizon={settings.prediction_horizon} "
                    f"len={len(forecast)}"
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"预测失败 {metric}: {e}")


async def _loop() -> None:
    while True:
        await _run_once()
        await asyncio.sleep(3600)


async def start_scheduler() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())
        logger.info("预测调度器已启动 (每小时一次)")


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
