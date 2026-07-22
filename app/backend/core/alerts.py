"""规则告警引擎.

读取 configs/rules.yaml -> 评估实时指标 -> 触发告警落库 (Redis 5 分钟去重).
"""
from pathlib import Path

import yaml

from ...common.config import settings
from ...common.db import AsyncSessionLocal
from ...common.logger import logger
from ...common.models import Alert
from ...common.redis_client import get_redis
from .realtime import get_stats

_rules_cache: list[dict] | None = None


def load_rules(path: str | None = None) -> list[dict]:
    """加载告警规则 (带缓存)."""
    global _rules_cache
    if _rules_cache is None:
        p = Path(path or settings.rules_file)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _rules_cache = data.get("rules", [])
        logger.info(f"已加载 {len(_rules_cache)} 条告警规则")
    return _rules_cache


async def evaluate() -> list[dict]:
    """评估当前实时指标, 触发满足条件的告警, 返回本次新触发列表."""
    stats = await get_stats()
    rules = load_rules()
    redis = get_redis()
    triggered: list[dict] = []
    new_alerts: list[Alert] = []

    for r in rules:
        value = stats.get(r["metric"], 0)
        threshold = r["threshold"]
        if value < threshold:
            continue
        # Redis 去重: 同一规则 5 分钟内只告警一次
        dedup_key = f"{settings.redis_prefix}:alert:{r['id']}"
        if not await redis.set(dedup_key, "1", ex=300, nx=True):
            continue
        msg = r["message"].format(value=value, threshold=threshold)
        new_alerts.append(
            Alert(
                level=r["level"],
                category=r["category"],
                message=msg,
                value=float(value),
                threshold=float(threshold),
            )
        )
        triggered.append({"rule_id": r["id"], "level": r["level"], "message": msg})

    if new_alerts:
        async with AsyncSessionLocal() as session:
            session.add_all(new_alerts)
            await session.commit()
        for a in new_alerts:
            logger.warning(f"[告警] [{a.level}] {a.message}")

    return triggered
