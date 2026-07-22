"""Redis 异步客户端 (实时状态: 当前车辆/人员数量、今日累计、告警)."""
import redis.asyncio as aioredis

from .config import settings

_pool: aioredis.ConnectionPool | None = None


def _get_pool() -> aioredis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            settings.redis_url, decode_responses=True, max_connections=32
        )
    return _pool


def get_redis() -> aioredis.Redis:
    """获取 Redis 客户端 (复用连接池)."""
    return aioredis.Redis(connection_pool=_get_pool())


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
