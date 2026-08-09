"""预测 REST API: /api/prediction.

核心功能: 每 N 分钟基于历史 30 个 N 分钟区间的总人数序列, 预测下一个 N 分钟的总人数.
车流转人流: 每辆车随机 2~5 人, 加总到人流得到总人数序列 (专门设计, 引入随机性
以反映真实场景中每车承载人数的随机波动).
"""
import asyncio
import json
import random
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..common.config import settings
from ..common.redis_client import get_redis
from .chronos_model import ChronosPredictor
from .repository import load_interval_history

router = APIRouter(tags=["prediction"])

# Chronos 推理超时 (秒): 避免单次预测 hang 阻塞事件循环
_PREDICT_TIMEOUT = 60


def convert_vehicle_to_person(vehicle_series: list[float]) -> list[float]:
    """将车流序列转化为人流序列: 每辆车 random(min, max) 人.

    对每个区间, 每辆车独立采样一个 [min, max] 的整数人数, 求和得到该区间转化后的人数.
    随机性为专门设计: 真实场景中每车承载人数存在波动, 确定性期望值会低估方差,
    不利于 Chronos-2 对人流峰谷的时序预测.
    """
    lo = settings.vehicle_person_min
    hi = settings.vehicle_person_max
    converted = []
    for v in vehicle_series:
        n = int(v)
        if n <= 0:
            converted.append(0.0)
        else:
            converted.append(float(sum(random.randint(lo, hi) for _ in range(n))))
    return converted


async def predict_total_persons() -> dict:
    """预测下一个 N 分钟的总人数.

    1. 读取历史 30 个 N 分钟区间的人流/车流序列
    2. 车流转人流: 每辆车 random(2, 5) 人
    3. 总人数 = 人流 + 转化后车流
    4. 喂入 Chronos-2 预测下一个 N 分钟
    """
    person_series, vehicle_series, timestamps = await load_interval_history()

    # 车流转人流
    converted_vehicle = convert_vehicle_to_person(vehicle_series)

    # 总人数序列 = 人流 + 转化后车流
    total_series = [p + v for p, v in zip(person_series, converted_vehicle)]

    if not total_series:
        return {
            "predicted_total": 0,
            "interval_minutes": settings.prediction_interval_minutes,
            "series_length": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # Chronos-2 预测下一个 N 分钟 (horizon=1)
    try:
        forecast = await asyncio.wait_for(
            asyncio.to_thread(ChronosPredictor.instance().predict, total_series, 1),
            timeout=_PREDICT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "prediction timeout")

    predicted = forecast[0] if forecast else 0.0

    return {
        "predicted_total": int(round(predicted)),
        "interval_minutes": settings.prediction_interval_minutes,
        "series_length": len(total_series),
        "history": {
            "timestamps": timestamps,
            "person": person_series,
            "vehicle": vehicle_series,
            "converted_vehicle": converted_vehicle,
            "total": total_series,
        },
        "latest_interval": {
            "person": person_series[-1] if person_series else 0,
            "vehicle": vehicle_series[-1] if vehicle_series else 0,
            "converted_vehicle": converted_vehicle[-1] if converted_vehicle else 0,
            "total": total_series[-1] if total_series else 0,
        },
        "degraded": ChronosPredictor.instance().is_degraded,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---- 新 API: N 分钟总人数预测 ----


@router.get("/health")
async def health():
    predictor = ChronosPredictor.instance()
    return {
        "status": "ok",
        "service": "prediction",
        "degraded": predictor.is_degraded,
        "interval_minutes": settings.prediction_interval_minutes,
        "series_length": settings.prediction_series_length,
        "vehicle_person_range": [
            settings.vehicle_person_min,
            settings.vehicle_person_max,
        ],
    }


@router.post("/predict")
async def predict_total():
    """预测下一个 N 分钟的总人数 (车流转人流 + 人流加总), 并评估预测告警."""
    result = await predict_total_persons()
    # 预测告警联动: 预测值超阈值时触发提前预警
    try:
        from ..backend.core.alerts import evaluate_prediction
        await evaluate_prediction(result)
    except Exception:  # noqa: BLE001
        pass  # 告警评估失败不影响预测结果返回
    return result


@router.get("/latest")
async def latest():
    """获取缓存的最近一次预测结果."""
    redis = get_redis()
    raw = await redis.get(f"{settings.redis_prefix}:prediction:latest:total")
    if not raw:
        raise HTTPException(404, "no cached prediction, call POST /api/prediction/predict first")
    return json.loads(raw)
