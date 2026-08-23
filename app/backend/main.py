"""业务后端 FastAPI 入口.

启动: uvicorn app.backend.main:app --host 0.0.0.0 --port 8000
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..common.config import settings
from ..common.logger import logger
from ..common.redis_client import close_redis
from .api import alerts, config_rules, devices, events, police, stats, ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("业务后端启动")
    scheduler_started = False
    heartbeat_started = False
    try:
        from ..prediction import start_scheduler

        await start_scheduler()
        scheduler_started = True
        logger.info("时序预测调度器已启动")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"预测调度器未启动: {e}")

    try:
        from .core.heartbeat import start_checker

        await start_checker()
        heartbeat_started = True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"离线检测未启动: {e}")

    police_started = False
    try:
        # 加载警力初始配置 (区域+总警力, 仅新增不覆盖 API 配置)
        from .core.allocator import seed_from_config

        await seed_from_config()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"警力配置加载失败: {e}")

    try:
        from .core.police_scheduler import start_scheduler as start_police

        await start_police()
        police_started = True
        logger.info("警力分配调度器已启动")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"警力分配调度器未启动: {e}")

    wvp_started = False
    try:
        from .core.wvp_sync import start_syncer as start_wvp

        await start_wvp()
        wvp_started = True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"WVP 同步未启动: {e}")

    archive_started = False
    try:
        from .core.archive_scheduler import start_scheduler as start_archive

        await start_archive()
        archive_started = True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MySQL 归档调度器未启动: {e}")

    yield

    if archive_started:
        try:
            from .core.archive_scheduler import stop_scheduler as stop_archive

            await stop_archive()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"停止 MySQL 归档调度器失败: {e}")
    if wvp_started:
        try:
            from .core.wvp_sync import stop_syncer as stop_wvp

            await stop_wvp()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"停止 WVP 同步失败: {e}")
    if police_started:
        try:
            from .core.police_scheduler import stop_scheduler as stop_police

            await stop_police()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"停止警力分配调度器失败: {e}")
    if heartbeat_started:
        try:
            from .core.heartbeat import stop_checker

            await stop_checker()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"停止离线检测失败: {e}")
    if scheduler_started:
        try:
            from ..prediction import stop_scheduler

            await stop_scheduler()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"停止预测调度器失败: {e}")
    try:
        from .core.wvp_client import close_wvp_client

        await close_wvp_client()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"关闭 WVP 客户端失败: {e}")
    try:
        from ..common.mysql_client import close_mysql

        await close_mysql()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"关闭 MySQL 连接池失败: {e}")
    await close_redis()
    logger.info("业务后端关闭")


app = FastAPI(
    title="智慧古城车辆人流监管平台 - 业务后端",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: 通过环境变量 CORS_ORIGINS 配置允许的来源 (逗号分隔), 生产环境应限定具体前端域名
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stats.router)
app.include_router(events.router)
app.include_router(alerts.router)
app.include_router(devices.router)
app.include_router(police.router)
app.include_router(ws.router)
app.include_router(config_rules.router)

# 时序预测路由 (挂载到 /api/prediction, 模块缺失则跳过)
try:
    from ..prediction import router as pred_router

    app.include_router(pred_router, prefix="/api/prediction")
    logger.info("已挂载预测路由 /api/prediction")
except Exception as e:  # noqa: BLE001
    logger.warning(f"预测路由未挂载: {e}")


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "service": "backend"}


# ---------- 静态文件 (运维工具页面) ----------
import os  # noqa: E402

_static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
