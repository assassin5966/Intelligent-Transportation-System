"""AI 分析服务入口.

启动: python -m app.ai.service
对外: 注册/启停摄像头管道, WebSocket实时推送, 每路拉流 -> 检测跟踪 -> 越线计数 -> 推送事件到后端.
"""
import asyncio
import json
import re
import shutil
import subprocess
import time
from typing import Literal, Optional

import psutil
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..common.config import settings
from ..common.business_rules import get_rule
from ..common.logger import logger
from .counter import Point
from .mock_simulator import MockSimulator
from .model_pool import model_pool
from .event_outbox import event_outbox
from .pipeline import _WS_QUEUE_MAX, DevicePipeline, register_ws_client, unregister_ws_client
from .stream import _decode_max_fps, _decode_target_short_side

_pipelines: dict[str, DevicePipeline] = {}
_simulator: Optional[MockSimulator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _simulator
    logger.info(f"AI 分析服务启动 (端口 {settings.ai_port})")
    await event_outbox.start()
    if settings.mock_enabled:
        # Mock 模式: 不拉流不推理, 由模拟器按真实规律生成事件 (验证前端/联调用)
        _simulator = MockSimulator()
        await _simulator.start()
        logger.info("[Mock] MOCK_ENABLED=true: 拉流/推理已禁用, 数据由模拟器生成")
    else:
        # 模型池在对外提供注册接口前完成加载/预热/自检:
        # 避免"首个设备注册时才建 CUDA context"引发的并发加载卡死与首帧毛刺
        await model_pool.initialize()
        snap = model_pool.snapshot()
        logger.info(
            f"推理模式={snap['mode']} ({snap['reason']}) 引擎={len(snap['engines'])} 显存="
            + (", ".join(f"{e['engine_id']}:{e['vram_mb']}MB" for e in snap['engines']) or "无")
        )
    yield
    if _simulator is not None:
        await _simulator.stop()
        _simulator = None
    for p in list(_pipelines.values()):
        await p.stop()
    _pipelines.clear()
    await model_pool.shutdown()
    await event_outbox.stop()


app = FastAPI(title="AI 分析服务", version="0.1.0", lifespan=lifespan)


class DeviceRegister(BaseModel):
    device_id: str
    stream_url: str
    line: list[list[float]]
    anchor: Optional[list[float]] = None
    count_only: Optional[Literal["enter", "exit"]] = None  # None=双向, "enter"=只计Enter, "exit"=只计Exit
    camera_type: Optional[Literal["vehicle", "person"]] = None  # None=全部检测, vehicle/person
    roi: Optional[list[list[float]]] = None  # ROI 多边形顶点 [[x,y],...], >=3 个, 归一化
    gb_device_id: Optional[str] = None  # 国标设备ID (WVP 同步设备填写, 启用流地址自动刷新)
    gb_channel_id: Optional[str] = None  # 国标通道ID (WVP 同步设备填写)


@app.get("/health", tags=["system"])
async def health():
    mock = _simulator is not None
    infer = model_pool.snapshot()
    return {
        "status": "ok",
        "service": "ai",
        "active_devices": len(_simulator.device_ids) if mock else len(_pipelines),
        "mock": mock,
        "event_outbox": await event_outbox.snapshot(),
        "effective_config": {
            "requested_infer_mode": settings.infer_mode,
            "inference_imgsz_current": int(get_rule("inference", "imgsz", default=settings.infer_imgsz)),
            "decode_backend_requested": settings.decode_backend,
            "decode_max_fps_next_connection": _decode_max_fps(),
            "decode_short_side_next_connection": _decode_target_short_side(),
        },
        # 推理模式与每卡负载 (运维可见): selftest_failed 表示已整体降级 legacy
        "infer": {
            "mode": "mock" if mock else infer["mode"],
            "reason": infer["reason"],
            "selftest_failed": infer["mode"] == "unavailable" and "自检" in infer["reason"],
            "engines": infer["engines"],
        },
    }


@app.get("/devices", tags=["ai"])
async def list_devices():
    if _simulator is not None:
        # Mock 模式: 返回模拟设备列表 (无真实 pipeline)
        return [
            {"device_id": d, "stream_url": "", "running": True, "mock": True}
            for d in _simulator.device_ids
        ]
    return [
        {
            "device_id": d,
            "stream_url": p.stream_url,
            "running": p.running,
            "processing_health": p.health_snapshot(),
            "infer_mode": p.mode,
            "engine_id": p.lease.engine_id if p.lease is not None else None,
        }
        for d, p in _pipelines.items()
    ]


@app.post("/devices", status_code=201, tags=["ai"])
async def register(dev: DeviceRegister):
    if _simulator is not None:
        # Mock 模式兜底保护: 拒绝创建真实拉流管道, 防止与模拟器数据叠加双重计数
        raise HTTPException(409, "mock mode enabled: real pipelines are disabled (MOCK_ENABLED=true)")
    if dev.device_id in _pipelines:
        raise HTTPException(409, "device already running")
    if model_pool.mode == "legacy" and _pipelines:
        raise HTTPException(503, "legacy 模式仅支持单路；多路请使用批量推理模式")
    if len(dev.line) != 2:
        raise HTTPException(400, "line must be 2 points [[x1,y1],[x2,y2]]")
    if model_pool.mode == "unavailable":
        raise HTTPException(503, f"推理引擎不可用: {model_pool.reason}")
    line: tuple[Point, Point] = (
        (float(dev.line[0][0]), float(dev.line[0][1])),
        (float(dev.line[1][0]), float(dev.line[1][1])),
    )
    anchor: Optional[Point] = None
    if dev.anchor and len(dev.anchor) == 2:
        anchor = (float(dev.anchor[0]), float(dev.anchor[1]))
    roi: Optional[list[Point]] = None
    if dev.roi is not None:
        if len(dev.roi) < 3:
            raise HTTPException(400, "roi must have >=3 points [[x,y],...]")
        roi = [(float(p[0]), float(p[1])) for p in dev.roi]
    # 槽位分配: gpu_batch 模式下返回该路所属卡与槽位; legacy 模式返回 None
    lease = model_pool.acquire(dev.device_id)
    if lease is None and model_pool.mode != "legacy":
        # 批量模式容量已满: 绝不回退 legacy —— 那会为每路各建一份模型,
        # 单卡 32 路时把显存/算力打满 (现场推理从 44ms/批 恶化到 600ms/批).
        raise HTTPException(
            503,
            f"推理容量已满 ({model_pool.mode}), 拒绝降级为 legacy 独立实例: {dev.device_id}",
        )
    try:
        p = DevicePipeline(
            dev.device_id,
            dev.stream_url,
            line,
            anchor,
            dev.count_only,
            dev.camera_type,
            roi,
            enable_url_refresh=bool(dev.gb_device_id),
            lease=lease,
        )
    except Exception:
        model_pool.release(dev.device_id)  # 构造失败不留悬空槽位
        raise
    p.start()
    _pipelines[dev.device_id] = p
    return {
        "device_id": dev.device_id,
        "status": "started",
        "readiness": "pending_first_frame",
        "infer_mode": p.mode,
        "engine_id": lease.engine_id if lease is not None else None,
    }


@app.delete("/devices/{device_id}", tags=["ai"])
async def stop_device(device_id: str):
    p = _pipelines.pop(device_id, None)
    if p is None:
        raise HTTPException(404, "device not found")
    await p.stop()
    # 槽位归还模型池: 跟踪状态随设备销毁, 该卡设备集变化后由下一路复用
    model_pool.release(device_id)
    return {"device_id": device_id, "status": "stopped"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # 有界队列: 慢客户端积压到上限后由广播侧丢最旧, 防止内存无限增长
    queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
    register_ws_client(queue)
    logger.info("WebSocket 客户端已连接")
    
    async def receiver():
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    
    async def sender():
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
                await websocket.send_text(json.dumps(message))
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                break
    
    try:
        await asyncio.gather(receiver(), sender())
    finally:
        unregister_ws_client(queue)
        logger.info("WebSocket 客户端已断开")


# ---------- 硬件监控采集 (供后端 /api/hardware 代理) ----------
_cpu_percent_prev: list = []  # [time, cpu_times]


def _cpu_percent() -> Optional[float]:
    """两次调用间的 CPU 使用率 (容器 /proc 即宿主机视角)."""
    now, times = time.time(), psutil.cpu_times()
    if not _cpu_percent_prev:
        _cpu_percent_prev[:] = [now, times]
        return None
    dt = now - _cpu_percent_prev[0]
    prev = _cpu_percent_prev[1]
    if dt <= 0:
        return None
    busy = (
        (times.user - prev.user) + (times.system - prev.system)
        + (getattr(times, "iowait", 0) - getattr(prev, "iowait", 0))
        + (getattr(times, "irq", 0) - getattr(prev, "irq", 0))
        + (getattr(times, "softirq", 0) - getattr(prev, "softirq", 0))
        + (getattr(times, "steal", 0) - getattr(prev, "steal", 0))
    )
    total = sum(
        getattr(times, f, 0) - getattr(prev, f, 0)
        for f in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    )
    _cpu_percent_prev[:] = [now, times]
    return round(max(0.0, min(100.0, 100.0 * busy / total)), 1) if total > 0 else None


def _nvidia_gpu() -> dict:
    """nvidia-smi 采集 GPU 指标; 无卡/驱动异常时返回 available=False."""
    fields = [
        "name", "temperature.gpu", "utilization.gpu", "memory.used", "memory.total",
        "power.draw", "power.limit", "clocks.current.sm", "clocks.max.sm",
        "clocks.current.memory", "pstate", "fan.speed",
    ]
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
        ).stdout.strip()
        gpus = [dict(zip(fields, [v.strip() for v in line.split(",")])) for line in out.splitlines()]
        procs = []
        try:
            pout = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=6,
            ).stdout.strip()
            for line in pout.splitlines():
                pid, name, mem = [v.strip() for v in line.split(",")]
                procs.append({"pid": int(pid), "name": name, "mem_mb": float(mem)})
        except Exception:  # noqa: BLE001
            pass
        # 降频/制动标志 (T4 散热与供电问题哨兵)
        flags = None
        try:
            q = subprocess.run(
                ["nvidia-smi", "-q", "-d", "PERFORMANCE"],
                capture_output=True, text=True, timeout=6,
            ).stdout

            def _act(key: str) -> Optional[str]:
                m = re.search(rf"{key}\s*:\s*(.+)", q)
                return m.group(1).strip() if m else None

            flags = {
                "hw_thermal_slowdown": _act("HW Thermal Slowdown"),
                "hw_power_brake_slowdown": _act("HW Power Brake Slowdown"),
                "sw_thermal_slowdown": _act("SW Thermal Slowdown"),
                "sw_power_cap": _act("SW Power Cap"),
            }
        except Exception:  # noqa: BLE001
            pass
        return {"available": bool(gpus), "gpus": gpus, "processes": procs, "flags": flags, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "gpus": [], "processes": [], "flags": None, "error": str(e)}


@app.get("/hardware", tags=["system"])
def hardware():
    """AI 视频推理硬件快照: GPU / 主机 / 推理引擎 / AI 进程."""
    # GPU
    gpu = _nvidia_gpu()
    # 主机 (容器共享内核, /proc 即宿主机数据)
    cpu_model = None
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            m = re.search(r"model name\s*:\s*(.+)", f.read())
            cpu_model = m.group(1).strip() if m else None
    except Exception:  # noqa: BLE001
        pass
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    load1, load5, load15 = psutil.getloadavg()
    disks = {}
    for label, path in (("/", "/"), ("data", "/home/dt/smart-transport")):
        try:
            u = shutil.disk_usage(path)
            disks[label] = {
                "total_gb": round(u.total / 2**30, 1), "used_gb": round(u.used / 2**30, 1),
                "percent": round(100.0 * u.used / u.total, 1),
            }
        except Exception:  # noqa: BLE001
            pass
    host = {
        "cpu_model": cpu_model,
        "cpu_logical": psutil.cpu_count(logical=True),
        "cpu_percent": _cpu_percent(),
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "mem_total_gb": round(mem.total / 2**30, 1), "mem_used_gb": round(mem.used / 2**30, 1),
        "mem_percent": mem.percent,
        "swap_percent": swap.percent,
        "disks": disks,
        "uptime_s": int(time.time() - psutil.boot_time()),
    }
    # 推理引擎
    try:
        infer = model_pool.snapshot()
    except Exception as e:  # noqa: BLE001
        infer = {"mode": "unknown", "reason": str(e), "engines": []}
    infer_cfg = {
        "capacity": getattr(settings, "infer_capacity", None),
        "slots_max": getattr(settings, "infer_slots_max", None),
        "model": getattr(settings, "yolo_model", None),
        "decode_max_fps": getattr(settings, "decode_max_fps", None),
    }
    # AI 进程
    try:
        proc = psutil.Process()
        with proc.oneshot():
            ai_proc = {
                "rss_mb": round(proc.memory_info().rss / 2**20, 1),
                "threads": proc.num_threads(),
                "uptime_s": int(time.time() - proc.create_time()),
            }
    except Exception:  # noqa: BLE001
        ai_proc = None
    return {
        "ts": time.time(),
        "gpu": gpu,
        "host": host,
        "infer": {"mode": infer.get("mode"), "reason": infer.get("reason"),
                  "engines": infer.get("engines", []), "config": infer_cfg},
        "active_devices": len(_simulator.device_ids) if _simulator is not None else len(_pipelines),
        "mock": _simulator is not None,
        "ai_process": ai_proc,
    }


if __name__ == "__main__":
    uvicorn.run("app.ai.service:app", host="0.0.0.0", port=settings.ai_port)
