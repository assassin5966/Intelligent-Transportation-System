"""业务后端 FastAPI 入口.

启动: uvicorn app.backend.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..common.config import settings
from ..common.logger import logger
from ..common.redis_client import close_redis
from .api import alerts, config_rules, device_info, devices, events, hardware, intersection, location, police, stats, ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("业务后端启动")
    # Mock/WVP 互斥保护: mock 模式下模拟器已生成全量数据, 若 WVP 同步同时运行
    # 会拉入真实设备并启流推理, 与模拟数据叠加造成双重计数, 故强制关闭 WVP.
    if settings.mock_enabled and settings.wvp_enabled:
        settings.wvp_enabled = False
        logger.warning("[Mock] MOCK_ENABLED=true: 已强制关闭 WVP 同步 (WVP_ENABLED 配置被忽略)")
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

    # 实时位置 Pulsar 消费 (对接《实时位置数据接入规范 V1.0》)
    location_started = False
    try:
        from .core.gps_location import start_consumer

        # start_consumer 为同步函数(内部建 Pulsar 连接+起守护线程), 放到线程避免阻塞事件循环
        location_started = await asyncio.to_thread(start_consumer)
        if location_started:
            logger.info("实时位置 Pulsar 消费已启动")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"实时位置消费未启动: {e}")

    archive_started = False
    try:
        from .core.archive_scheduler import start_scheduler as start_archive

        await start_archive()
        archive_started = True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MySQL 归档调度器未启动: {e}")

    # 设备信息表 (名称/经纬度/分类): MySQL 建表 + 空表种子导入 (排除云冈类) + 加载内存缓存.
    # 展示链路经 geo_by_name/category_by_name 读缓存; MySQL 未启用时回落到 JSON, 不阻塞启动.
    # MySQL 首次初始化可能慢于后端 (最多重试约 60s), 失败不置就绪位, 运维 API 调用时会自愈.
    if settings.mysql_enabled:
        last_err: Exception | None = None
        for attempt in range(1, 13):
            try:
                from ..common import device_info as device_info_store

                await device_info_store.ensure_ready()
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(
                    f"设备信息表初始化失败(第 {attempt}/12 次, 等待 MySQL 就绪): {e}"
                )
                await asyncio.sleep(5)
        if last_err is not None:
            logger.warning(f"设备信息表初始化最终失败: {last_err} (展示链路将回落 JSON)")

    # 路口数据卡表 (intersection_info): 建表 + 空表种子导入 (9 张路口卡 + 32 槽位绑定),
    # 再把设备按名称最长匹配回填所属路口 (仅填 NULL 行). 失败不阻塞启动:
    # 配置页 API 首次请求会经 ensure_ready() 自愈, 大屏则退回设备聚合兜底布局.
    if settings.mysql_enabled:
        try:
            from ..common import device_info as device_info_store
            from ..common import intersection_info as intersection_store

            await intersection_store.ensure_ready()
            await device_info_store.backfill_intersection_ids()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"路口表初始化/设备路口回填失败 (不影响启动): {e}")

    if settings.event_ledger_enabled and settings.mysql_enabled:
        from .core.event_ledger import event_ledger
        event_ledger.start()
    elif settings.event_ledger_enabled:
        logger.error('EVENT_LEDGER_ENABLED=true 需要 MYSQL_ENABLED=true，事件账本不可用')

    yield

    if settings.event_ledger_enabled and settings.mysql_enabled:
        from .core.event_ledger import event_ledger
        await event_ledger.stop()

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
    if location_started:
        try:
            from .core.gps_location import stop_consumer

            await asyncio.to_thread(stop_consumer)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"停止实时位置消费失败: {e}")
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
app.include_router(device_info.router)
app.include_router(intersection.router)
app.include_router(police.router)
app.include_router(ws.router)
app.include_router(config_rules.router)
app.include_router(location.router)
app.include_router(hardware.router)

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
