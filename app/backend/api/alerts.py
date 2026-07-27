"""告警 API."""
import json

from fastapi import APIRouter, Query

from ...common.config import settings
from ...common.redis_client import get_redis

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("")
async def list_(limit: int = Query(100, ge=1, le=1000)):
    """告警列表 (Redis List 保留最近 1000 条, 按时间倒序)."""
    redis = get_redis()
    raw = await redis.lrange(f"{settings.redis_prefix}:alerts", 0, limit - 1)
    return [json.loads(item) for item in raw]
