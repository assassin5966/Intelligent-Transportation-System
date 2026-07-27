"""业务后端 FastAPI 入口.

启动: uvicorn app.backend.main:app --host 0.0.0.0 --port 8000
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..common.config import settings
from ..common.logger import logger
from ..common.redis_client import close_redis
from .api import alerts, devices, events, stats


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("业务后端启动")
    scheduler_started = False
    try:
        from ..prediction import start_scheduler

        await start_scheduler()
        scheduler_started = True
        logger.info("时序预测调度器已启动")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"预测调度器未启动: {e}")

    yield

    if scheduler_started:
        try:
            from ..prediction import stop_scheduler

            await stop_scheduler()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"停止预测调度器失败: {e}")
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
