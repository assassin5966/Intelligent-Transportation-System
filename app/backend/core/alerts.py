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
        alert_id = await redis.incr(_ALERT_SEQ_KEY)
        alert["id"] = alert_id
        pipe = redis.pipeline()
        pipe.lpush(_ALERTS_KEY, json.dumps(alert))
        pipe.ltrim(_ALERTS_KEY, 0, _ALERT_MAX - 1)
        await pipe.execute()
        triggered.append(alert)
        logger.warning(f"[告警] [{r['level']}] {msg}")

    return triggered
