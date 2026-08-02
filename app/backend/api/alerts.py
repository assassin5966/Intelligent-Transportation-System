"""告警 API."""
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis
from ..core.alerts import persist_alert

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("")
async def list_(limit: int = Query(100, ge=1, le=1000)):
    """告警列表 (Redis List 保留最近 1000 条, 按时间倒序)."""
    redis = get_redis()
    raw = await redis.lrange(f"{settings.redis_prefix}:alerts", 0, limit - 1)
    return [json.loads(item) for item in raw]


# ---- 视频异常上报 (AI -> 后端) ----

_ANOMALY_LABELS = {
    "black_screen": "黑屏",
    "flower_screen": "花屏",
}

_VALID_ANOMALY_TYPES = set(_ANOMALY_LABELS)
_VALID_PHASES = {"onset", "recovery"}


class AnomalyAlertIn(BaseModel):
    device_id: str = Field(..., description="设备ID")
    anomaly_type: str = Field(..., description="black_screen | flower_screen")
    phase: str = Field("onset", description="onset=异常开始 | recovery=恢复正常")
    scores: Optional[dict] = Field(None, description="检测信号分数 (亮度/噪声/相关性等)")


@router.post("/anomaly", status_code=201)
async def report_anomaly(body: AnomalyAlertIn):
    """接收 AI 服务上报的视频异常, 去重后持久化为告警并 WebSocket 推送.

    - 同设备同异常同 phase 在 anomaly_cooldown_seconds 内只落一条 (Redis NX 去重).
    - onset=critical, recovery=info; 前端经 GET /api/alerts 与后端 /ws 即可看到.
    """
    if body.anomaly_type not in _VALID_ANOMALY_TYPES:
        raise HTTPException(400, f"anomaly_type 必须为 {sorted(_VALID_ANOMALY_TYPES)}")
    if body.phase not in _VALID_PHASES:
        raise HTTPException(400, f"phase 必须为 {sorted(_VALID_PHASES)}")

    redis = get_redis()
    # 冷却去重: 兜底防管道重启/重复上报刷屏 (管道层已做状态转移去抖)
    dedup_key = (
        f"{settings.redis_prefix}:alert:anomaly:"
        f"{body.device_id}:{body.anomaly_type}:{body.phase}"
    )
    if not await redis.set(dedup_key, "1", ex=settings.anomaly_cooldown_seconds, nx=True):
        return {"status": "deduplicated", "device_id": body.device_id}

    label = _ANOMALY_LABELS[body.anomaly_type]
    is_onset = body.phase == "onset"
    level = "critical" if is_onset else "info"
    if is_onset:
        message = f"设备 {body.device_id} 检测到{label}异常"
    else:
        message = f"设备 {body.device_id} {label}异常已恢复正常"

    alert = {
        "rule_id": f"video_{body.anomaly_type}",
        "level": level,
        "category": "video_anomaly",
        "message": message,
        "device_id": body.device_id,
        "anomaly_type": body.anomaly_type,
        "phase": body.phase,
        "scores": body.scores,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await persist_alert(alert)
    logger.warning(f"[视频异常] [{level}] {message}")

    # WebSocket 推送给前端
    try:
        from .ws import broadcast_alert
        await broadcast_alert(alert)
    except Exception:  # noqa: BLE001
        pass  # 推送失败不影响落库

    return {"status": "ok", "alert": alert}
