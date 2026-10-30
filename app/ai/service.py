"""AI 分析服务入口.

启动: python -m app.ai.service
对外: 注册/启停摄像头管道, WebSocket实时推送, 每路拉流 -> 检测跟踪 -> 越线计数 -> 推送事件到后端.
"""
import asyncio
import faulthandler
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Literal, Optional

import psutil
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
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


# ---------- 事件循环卡死看门狗 ----------
# 现场症状: 进程 100% CPU、日志突然全静默、HTTP 全部超时、GPU 空闲 —— 事件循环被
# 某个不 await 的忙循环占死, 事后无法复现也无法取栈。这里用一个独立线程给主循环
# 打心跳, 心跳停滞超过阈值就把**全部线程的 Python 栈**刷到 stderr(docker logs),
# 事后可直接定位到具体行。默认 30s 触发; AI_LOOP_WATCHDOG_S=0 可关闭。
_LOOP_BEAT = time.monotonic()
_WATCHDOG_DUMP_GAP_S = 300.0   # 两次转储的最小间隔, 避免刷爆日志


async def _loop_heartbeat() -> None:
    global _LOOP_BEAT
    while True:
        _LOOP_BEAT = time.monotonic()
        await asyncio.sleep(1.0)


def _start_loop_watchdog() -> None:
    """启动心跳协程 + 独立看门狗线程 (需在事件循环内调用)."""
    global _LOOP_BEAT
    try:
        stall_s = float(os.environ.get("AI_LOOP_WATCHDOG_S", "30") or 0)
    except ValueError:
        stall_s = 30.0
    try:
        faulthandler.enable()
        # kill -USR1 1 可手动转储 (无需等卡死)
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception:  # noqa: BLE001
        pass
    if stall_s <= 0:
        logger.info("事件循环看门狗已关闭 (AI_LOOP_WATCHDOG_S=0)")
        return

    _LOOP_BEAT = time.monotonic()
    asyncio.get_running_loop().create_task(_loop_heartbeat())
    state = {"last_dump": 0.0}

    def _watch() -> None:
        while True:
            time.sleep(2.0)
            stall = time.monotonic() - _LOOP_BEAT
            now = time.monotonic()
            if stall < stall_s or (now - state["last_dump"]) < _WATCHDOG_DUMP_GAP_S:
                continue
            state["last_dump"] = now
            try:
                sys.stderr.write(
                    f"\n[watchdog] 事件循环已停滞 {stall:.0f}s, 转储全部线程栈 "
                    f"(pid={os.getpid()}):\n"
                )
                sys.stderr.flush()
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_watch, name="loop-watchdog", daemon=True).start()
    logger.info(f"事件循环看门狗已启用 (停滞阈值 {stall_s:g}s, SIGUSR1 可手动转储)")



@asynccontextmanager
async def lifespan(app: FastAPI):
    global _simulator
    logger.info(f"AI 分析服务启动 (端口 {settings.ai_port})")
    _start_loop_watchdog()
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
    outbox = await event_outbox.snapshot()
    states: dict[str, int] = {}
    backends: dict[str, int] = {}
    if not mock:
        for pipeline in _pipelines.values():
            health_info = pipeline.health_snapshot()
            state = health_info['state']
            backend = health_info['decode_backend']
            states[state] = states.get(state, 0) + 1
            backends[backend] = backends.get(backend, 0) + 1
    degraded = (infer['mode'] == 'unavailable' and not mock) or bool(outbox.get('storage_error') or outbox.get('delivery_error') or outbox.get('coverage_error')) or any(state != 'processing' for state in states)
    return {
        "status": "degraded" if degraded else "ok",
        "service": "ai",
        "active_devices": len(_simulator.device_ids) if mock else len(_pipelines),
        "mock": mock,
        "event_outbox": outbox,
        "processing_states": states,
        "decode_backends": backends,
        "effective_config": {
            "requested_infer_mode": settings.infer_mode,
            "gpu_engines_per_device": settings.infer_engines_per_gpu,
            "event_delivery_protocol": settings.event_delivery_protocol,
            "business_timezone": settings.business_timezone,
            "inference_imgsz_current": int(get_rule("inference", "imgsz", default=settings.infer_imgsz)),
            "decode_backend_requested": settings.decode_backend,
            "decode_max_fps_next_connection": _decode_max_fps(),
            "ffmpeg_sampling_mode_next_connection": settings.decode_fps_sampling,
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


@app.get('/devices/{device_id}/coverage', tags=['ai'])
async def device_coverage(device_id: str, limit: int = Query(100, ge=1, le=1000)):
    return {'device_id': device_id, 'time_basis': 'local_watchdog_5s',
            'intervals_are_conservative': True,
            'gaps': await event_outbox.coverage_gaps(device_id, limit)}


@app.post("/devices", status_code=201, tags=["ai"])
async def register(dev: DeviceRegister):
    from ..common.counting_config import validate_counting_geometry
    if _simulator is not None:
        # Mock 模式兜底保护: 拒绝创建真实拉流管道, 防止与模拟器数据叠加双重计数
        raise HTTPException(409, "mock mode enabled: real pipelines are disabled (MOCK_ENABLED=true)")
    if dev.device_id in _pipelines:
        raise HTTPException(409, "device already running")
    if model_pool.mode == "legacy" and _pipelines:
        raise HTTPException(503, "legacy 模式仅支持单路；多路请使用批量推理模式")
    try:
        valid_line, valid_anchor, valid_roi = validate_counting_geometry(dev.line, dev.anchor, dev.roi)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if model_pool.mode == "unavailable":
        raise HTTPException(503, f"推理引擎不可用: {model_pool.reason}")
    line: tuple[Point, Point] = (
        tuple(valid_line[0]), tuple(valid_line[1]),
    )
    anchor: Optional[Point] = None
    if valid_anchor is not None:
        anchor = tuple(valid_anchor)
    roi: Optional[list[Point]] = None
    if valid_roi is not None:
        roi = [tuple(p) for p in valid_roi]
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
