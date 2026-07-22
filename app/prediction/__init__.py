"""时序预测模块 (Chronos).

供后端 main.py 挂载:
  - router        -> 挂到 /api/prediction
  - start/stop_scheduler -> lifespan 中启停
"""
import logging

try:
    from .router import router  # noqa: F401
    from .scheduler import start_scheduler, stop_scheduler  # noqa: F401
except Exception as e:  # noqa: BLE001
    logging.getLogger(__name__).warning(f"预测模块加载失败(后端仍可运行): {e}")

__all__ = ["router", "start_scheduler", "stop_scheduler"]
