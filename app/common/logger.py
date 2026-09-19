"""日志 (loguru). 统一进程内取 `logger`."""
import sys
from loguru import logger

from .config import settings

logger.remove()
# 控制台级别由 LOG_LEVEL 控制 (默认 INFO); 排查计数问题时设 LOG_LEVEL=DEBUG,
# 即可在 docker logs 里看到详细日志. 文件日志始终 DEBUG, 不受此影响.
logger.add(
    sys.stderr,
    level=settings.log_level.upper(),
    enqueue=True,
    backtrace=False,
    diagnose=False,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
)
logger.add(
    "logs/{time:YYYY-MM-DD}.log",
    rotation="100 MB",
    retention="30 days",
    level="DEBUG",
    enqueue=True,
    encoding="utf-8",
)

__all__ = ["logger"]
