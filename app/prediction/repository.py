"""预测数据访问: 从 Redis 读取 N 分钟区间历史序列."""
from datetime import datetime, timedelta
from typing import Optional

from ..common.business_rules import get_rule
from ..common.config import settings
from ..common.redis_client import get_redis


def _interval_key(dt: datetime, interval: int) -> str:
    """N 分钟区间 key (向下取整到区间起点)."""
    minute = (dt.minute // interval) * interval
    interval_start = dt.replace(minute=minute, second=0, microsecond=0)
    return f"{settings.redis_prefix}:realtime:interval:{interval_start.strftime('%Y%m%d%H%M')}"


async def load_interval_history(
    series_length: Optional[int] = None,
) -> tuple[list[float], list[float], list[str]]:
    """读取近 series_length 个 N 分钟区间的人流/车流序列.

    返回 (person_series, vehicle_series, timestamps), 按时间升序.
    每个元素为该 N 分钟区间内的进出总量 (in + out).
    空区间补 0, 保持规则采样.
    """
    interval = int(get_rule("prediction", "interval_minutes", default=settings.prediction_interval_minutes))
    if series_length is None:
        series_length = int(get_rule("prediction", "series_length", default=settings.prediction_series_length))
    redis = get_redis()
    now = datetime.now()
    pipe = redis.pipeline()
    for i in range(series_length):
        t = now - timedelta(minutes=interval * i)
        key = _interval_key(t, interval)
        pipe.hgetall(key)
    results = await pipe.execute()

    person_series: list[float] = []
    vehicle_series: list[float] = []
    timestamps: list[str] = []
    # results[0] = 最近区间, results[-1] = 最早; 反转为时间升序
    for i, data in enumerate(reversed(results)):
        person_total = int(data.get("person_in", 0)) + int(data.get("person_out", 0))
        vehicle_total = int(data.get("vehicle_in", 0)) + int(data.get("vehicle_out", 0))
        person_series.append(float(person_total))
        vehicle_series.append(float(vehicle_total))
        # 计算对应的时间戳
        t = now - timedelta(minutes=interval * (series_length - 1 - i))
        timestamps.append(_interval_key(t, interval).split(":")[-1])
    return person_series, vehicle_series, timestamps
