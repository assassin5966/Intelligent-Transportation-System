"""规则告警引擎.

读取 configs/rules.yaml -> 评估实时指标 -> 触发告警 (Redis 5 分钟去重),
并持久化到 Redis List 供 /api/alerts 查询.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ...common.config import settings
from ...common.logger import logger
from ...common.redis_client import get_redis
from .realtime import get_stats

_rules_cache: Optional[list[dict]] = None
_rules_mtime: Optional[float] = None  # 规则文件修改时间, 变化时自动重载

_ALERTS_KEY = f"{settings.redis_prefix}:alerts"
_ALERT_SEQ_KEY = f"{settings.redis_prefix}:alert:seq"
_ALERT_MAX = 1000


async def persist_alert(alert: dict) -> dict:
    """持久化一条告警到 Redis List (分配自增 id, 保留最近 _ALERT_MAX 条).

    供 evaluate / evaluate_prediction / 异常上报端点复用, 统一持久化逻辑.
    """
    redis = get_redis()
    alert_id = await redis.incr(_ALERT_SEQ_KEY)
    alert["id"] = alert_id
    pipe = redis.pipeline()
    pipe.lpush(_ALERTS_KEY, json.dumps(alert))
    pipe.ltrim(_ALERTS_KEY, 0, _ALERT_MAX - 1)
    await pipe.execute()
    return alert


def load_rules(path: Optional[str] = None) -> list[dict]:
    """加载告警规则 (带缓存, 文件修改后自动重载, 无需重启)."""
    global _rules_cache, _rules_mtime
    p = Path(path or settings.rules_file)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = None
    if _rules_cache is None or (mtime is not None and _rules_mtime != mtime):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _rules_cache = data.get("rules", [])
        _rules_mtime = mtime
        logger.info(f"已加载 {len(_rules_cache)} 条告警规则")
    return _rules_cache


_predict_rules_cache: Optional[list[dict]] = None
_predict_rules_mtime: Optional[float] = None


def load_predict_rules(path: Optional[str] = None) -> list[dict]:
    """加载预测告警规则 (带缓存, 文件修改后自动重载, 无需重启)."""
    global _predict_rules_cache, _predict_rules_mtime
    p = Path(path or settings.rules_file)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = None
    if _predict_rules_cache is None or (mtime is not None and _predict_rules_mtime != mtime):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _predict_rules_cache = data.get("predict_rules", [])
        _predict_rules_mtime = mtime
    return _predict_rules_cache


async def evaluate() -> list[dict]:
    """评估当前实时指标, 触发满足条件的告警, 返回本次新触发列表."""
    stats = await get_stats()
    rules = load_rules()
    redis = get_redis()
    triggered: list[dict] = []

    for r in rules:
        value = stats.get(r["metric"], 0)
        threshold = r["threshold"]
        if value < threshold:
            continue
        dedup_key = f"{settings.redis_prefix}:alert:{r['id']}"
        if not await redis.set(dedup_key, "1", ex=300, nx=True):
            continue
        msg = r["message"].format(value=value, threshold=threshold)
        alert = {
            "rule_id": r["id"],
            "level": r["level"],
            "category": r.get("category", ""),
            "message": msg,
            "value": value,
            "threshold": threshold,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await persist_alert(alert)
        triggered.append(alert)
        logger.warning(f"[告警] [{r['level']}] {msg}")

    # 通过 WebSocket 推送新告警给前端
    for alert in triggered:
        try:
            from ..api.ws import broadcast_alert
            await broadcast_alert(alert)
        except Exception:  # noqa: BLE001
            pass  # WebSocket 推送失败不影响告警流程

    return triggered


async def evaluate_prediction(prediction: dict) -> list[dict]:
    """评估预测结果, 预测值超阈值时触发提前预警.

    prediction: predict_total_persons() 返回的字典, 包含 predicted_total 和 interval_minutes.
    """
    rules = load_predict_rules()
    if not rules:
        return []

    redis = get_redis()
    predicted_value = prediction.get("predicted_total", 0)
    predict_minutes = prediction.get("interval_minutes", settings.prediction_interval_minutes)
    triggered: list[dict] = []

    for r in rules:
        threshold = r["threshold"]
        if predicted_value < threshold:
            continue
        # 预测告警去重: 一个预测周期内同一规则只触发一次
        dedup_key = f"{settings.redis_prefix}:alert:{r['id']}"
        if not await redis.set(dedup_key, "1", ex=settings.prediction_interval_minutes * 60, nx=True):
            continue
        msg = r["message"].format(
            value=predicted_value,
            threshold=threshold,
            predict_minutes=predict_minutes,
        )
        alert = {
            "rule_id": r["id"],
            "level": r["level"],
            "category": r.get("category", ""),
            "message": msg,
            "value": predicted_value,
            "threshold": threshold,
            "predict_minutes": predict_minutes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await persist_alert(alert)
        triggered.append(alert)
        logger.warning(f"[预测告警] [{r['level']}] {msg}")

    # WebSocket 推送
    for alert in triggered:
        try:
            from ..api.ws import broadcast_alert
            await broadcast_alert(alert)
        except Exception:  # noqa: BLE001
            pass

    return triggered
