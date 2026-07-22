"""日志 (loguru). 统一进程内取 `logger`."""
import sys
from loguru import logger

logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
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
