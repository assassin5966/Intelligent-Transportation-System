"""预测数据访问: 从 Redis 读取逐小时历史序列."""
from datetime import datetime, timedelta
from typing import Optional

from ..common.config import settings
from ..common.redis_client import get_redis


async def load_history(metric: str, hours: Optional[int] = None) -> list[float]:
    """读取近 hours 小时的逐小时序列 (供 Chronos 时序预测).

    metric: "vehicle" -> 车辆进出总量, "person" -> 人员进出总量.
    返回按时间升序的每小时总量列表 (空小时补 0, 保持规则采样便于模型识别周期).
    """
    if hours is None:
        hours = settings.prediction_history_hours
    redis = get_redis()
    now = datetime.now()
    pipe = redis.pipeline()
    for h in range(hours):
        t = now - timedelta(hours=h)
        key = f"{settings.redis_prefix}:realtime:hourly:{t.strftime('%Y%m%d%H')}"
        pipe.hgetall(key)
    results = await pipe.execute()

    history: list[float] = []
    # results[0] = 最近一小时, results[-1] = hours 小时前; 反转为时间升序
    for data in reversed(results):
        if metric == "vehicle":
            total = int(data.get("vehicle_in", 0)) + int(data.get("vehicle_out", 0))
        else:
            total = int(data.get("person_in", 0)) + int(data.get("person_out", 0))
        history.append(float(total))
    return history
