"""预测 REST API: /api/prediction."""
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..common.config import settings
from ..common.redis_client import get_redis
from .chronos_model import ChronosPredictor
from .repository import load_history

router = APIRouter(tags=["prediction"])


class PredictIn(BaseModel):
    metric: str  # vehicle | person
    horizon: int  # 15 / 30 / 45 / 60 (小时; 基于逐小时历史序列预测未来 horizon 小时)


@router.get("/health")
async def health():
    return {"status": "ok", "service": "prediction"}


@router.post("/predict")
async def predict(req: PredictIn):
    if req.metric not in ("vehicle", "person"):
        raise HTTPException(400, "metric must be 'vehicle' or 'person'")
    if req.horizon not in (15, 30, 45, 60):
        raise HTTPException(400, "horizon must be 15/30/45/60")
    history = await load_history(req.metric)
    forecast = ChronosPredictor.instance().predict(history, req.horizon)
    return {
        "metric": req.metric,
        "horizon": req.horizon,
        "forecast": forecast,
        "history_length": len(history),
        "generated_at": datetime.utcnow().isoformat(),
    }


@router.get("/latest")
async def latest(metric: str = Query(...)):
    redis = get_redis()
    raw = await redis.get(f"{settings.redis_prefix}:prediction:latest:{metric}")
    if not raw:
        raise HTTPException(404, "no cached prediction")
    return json.loads(raw)
