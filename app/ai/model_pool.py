"""模型池: 每张 GPU (或 CPU 引擎池) 一个批量推理引擎, 设备按"最少负载"均摊.

解决的三类现场问题:
  P1 CUDA context 频繁切换 —— 模型实例数从 "每路一个" 降到 "每卡一个"
     (32 路 -> 8 个), 且每个实例只被自己的单线程 executor 触碰.
  P2 异步实例化资源管理 —— 所有模型在 lifespan 启动阶段一次性加载/预热/自检
     (asyncio.to_thread), 不再在推理 worker 线程里懒加载 CUDA context;
     设备下线时 release() 释放槽位, 服务关闭时 shutdown() 真正回收显存.
  P3 GPU 负载不均衡 —— acquire() 用 min(engines, key=(设备数, 引擎号)) 选卡,
     32 路严格均摊为每卡 4 路.

三种批量模式 (infer_mode 决定, 降级链 auto: gpu_batch -> cpu_batch -> legacy):
  gpu_batch —— 每张卡一个引擎 (device=cuda:N), 8 卡真并行.
  cpu_batch —— 无 GPU 部署的批量路径: 起 infer_cpu_engines 个 CPU 引擎
     (device=cpu, FP32), 复用与 GPU 完全相同的批量/槽位/组批逻辑.
     CPU 算力受限, 批量大不省算力 (近似线性开销), 引擎数主要影响路间隔离;
     torch 算子线程数按 核数/引擎数 设置 (进程级, 初始化时一次性设定).
  legacy —— 每路独立模型实例 (改动前行为), 仅作最终兜底.

降级策略 (打醒目 WARNING, /health 可见原因):
  - 配置 infer_mode=legacy: 直接 legacy;
  - 显式指定 gpu_batch/cpu_batch: 引擎失败只回退 legacy, 不静默换挡;
  - auto: CUDA 不可用/引擎自检失败 -> 自动转 cpu_batch; CPU 引擎也失败 -> legacy.
legacy 模式下 acquire() 恒返回 None, 调用方 (DevicePipeline) 退回本地
ByteTracker + inference_scheduler, 行为与优化前完全一致.
"""
import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional

from ..common.config import settings
from ..common.logger import logger

from .gpu_engine import GpuInferEngine
from .tracker import TRACKER_CFG

__all__ = ["Lease", "ModelPool"]


@dataclass(frozen=True)
class Lease:
    """一路设备对某个批量引擎某槽位的占用凭证 (随设备生命周期存在)."""

    mode: str          # gpu_batch | cpu_batch (取分配时刻的池模式)
    engine_id: str     # "gpu0".."gpu7" / "cpu0".."cpuN"
    slot: int
    engine: GpuInferEngine


class ModelPool:
    """批量推理资源池: 管理 GPU 卡 / CPU 引擎池与槽位分配."""

    def __init__(self) -> None:
        self.mode: str = "legacy"          # gpu_batch | cpu_batch | legacy
        self.reason: str = "not_initialized"  # 降级原因 (供 /health 展示)
        self._engines: list = []
        self._leases: dict = {}            # device_id -> Lease
        self._initialized = False

    # ---- 生命周期 ----

    async def initialize(self) -> None:
        """服务对外前完成: 探测 CUDA -> 逐引擎加载 -> 自检 -> 按链路降级."""
        if self._initialized:
            return
        self._initialized = True

        requested = str(settings.infer_mode).lower()

        if requested == "legacy":
            self._set_legacy("infer_mode=legacy (配置强制)")
            return

        if requested == "cpu_batch":
            await self._init_cpu(explicit=True)
            return

        # auto / gpu_batch: 先试 GPU
        ok, detail = self._probe_cuda()
        gpus = self._target_gpus(detail) if ok else []
        if not gpus:
            reason = detail if not ok else "无可用 GPU 卡"
            if requested == "gpu_batch":
                self._set_legacy(f"{reason} (gpu_batch 显式指定, 不降 CPU)")
                return
            logger.warning(f"推理模式: GPU 不可用 ({reason}), 自动转 cpu_batch")
        else:
            slots = max(1, int(settings.infer_slots_per_gpu))
            timeout_s = max(0.0, int(settings.infer_batch_timeout_ms) / 1000.0)
            specs = [
                (f"gpu{g}", f"cuda:{g}", bool(settings.infer_half))
                for g in gpus
            ]
            try:
                await self._start_engines(specs, "gpu_batch", slots, timeout_s)
                return
            except Exception as e:  # noqa: BLE001
                reason = f"GPU 引擎加载/自检失败: {type(e).__name__}: {e}"
                if requested == "gpu_batch":
                    self._set_legacy(f"{reason} (gpu_batch 显式指定, 不降 CPU)")
                    return
                logger.warning(f"推理模式: {reason}, 自动转 cpu_batch")

        await self._init_cpu(explicit=False)

    def _set_legacy(self, reason: str) -> None:
        self.mode = "legacy"
        self.reason = reason
        logger.warning(f"推理模式: legacy ({reason})")

    async def _init_cpu(self, *, explicit: bool) -> None:
        """启动 CPU 批量引擎; 失败回退 legacy (explicit 语义只影响提示文案)."""
        try:
            import torch
            cores = os.cpu_count() or 4
            n = max(1, min(int(settings.infer_cpu_engines), cores))
            threads = int(settings.infer_cpu_threads) or max(1, cores // n)
            # torch 算子线程数为进程级全局: 每引擎批内算子用它, 多引擎并发时
            # 总线程数 ≈ 引擎数 × threads ≈ 核数, 不超订
            torch.set_num_threads(threads)
            slots = max(1, int(settings.infer_slots_per_gpu))
            timeout_s = max(0.0, int(settings.infer_batch_timeout_ms) / 1000.0)
            specs = [(f"cpu{i}", "cpu", False) for i in range(n)]
            await self._start_engines(specs, "cpu_batch", slots, timeout_s)
            logger.info(
                f"CPU 推理: {n} 引擎 x {threads} 算子线程 (可见核数 {cores}, "
                f"imgsz={settings.infer_imgsz}; CPU 部署建议 imgsz<=640 且拉子码流)"
            )
        except Exception as e:  # noqa: BLE001
            self._set_legacy(
                f"CPU 引擎加载/自检失败: {type(e).__name__}: {e}"
                + (" (cpu_batch 显式指定)" if explicit else "")
            )

    async def _start_engines(
        self, specs: list, mode: str, slots: int, timeout_s: float
    ) -> None:
        """逐引擎加载/预热/自检; 任一失败整体抛错 (已建引擎就地回收, 不留半成品)."""
        t0 = time.monotonic()
        engine = None
        try:
            for engine_id, device, half in specs:
                engine = GpuInferEngine(
                    engine_id,
                    device=device,
                    model_path=settings.yolo_model,
                    slots=slots,
                    batch_timeout_s=timeout_s,
                    queue_max_batches=int(settings.infer_queue_max_batches),
                    tracker_cfg=TRACKER_CFG,
                    imgsz=int(settings.infer_imgsz),
                    half=half,
                )
                # 同步加载放在线程内, 避免阻塞事件循环 (加载含模型初始化, 秒级)
                await asyncio.to_thread(engine.start_blocking)
                engine.attach_loop()
                self._engines.append(engine)
                engine = None  # 已纳入池管理, 由 _shutdown_engines 负责回收
        except Exception:  # noqa: BLE001
            # 失败那台尚未入池, 需单独停掉 (可能已占用显存/内存), 再清空整体
            await self._stop_engine(engine)
            await self._shutdown_engines()
            raise

        self.mode = mode
        self.reason = "ok"
        logger.info(
            f"推理模式: {mode} | {len(self._engines)} 引擎 x {slots} 槽位 | "
            f"加载总耗时 {time.monotonic() - t0:.1f}s | "
            f"imgsz={settings.infer_imgsz} "
            f"batch_window={settings.infer_batch_timeout_ms}ms"
        )

    async def shutdown(self) -> None:
        await self._shutdown_engines()
        self._leases.clear()
        self.mode = "legacy"
        self.reason = "stopped"

    async def _shutdown_engines(self) -> None:
        engines, self._engines = self._engines, []
        for engine in engines:
            await self._stop_engine(engine)

    async def _stop_engine(self, engine) -> None:
        """停止单个引擎并回收其显存 (未 attach_loop 的半成品引擎也能安全回收)."""
        if engine is None:
            return
        try:
            await engine.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{engine.engine_id}] 引擎停止异常: {e}")

    # ---- 槽位分配 ----

    def acquire(self, device_id: str) -> Optional[Lease]:
        """为新设备分配槽位; legacy 模式或全部槽位耗尽返回 None (调用方回退 legacy)."""
        if self.mode == "legacy":
            return None
        existing = self._leases.get(device_id)
        if existing is not None:
            return existing

        candidates = [e for e in self._engines if e.has_capacity()]
        if not candidates:
            logger.warning(
                f"推理引擎槽位已满 ({len(self._engines)} 引擎), {device_id} 降级为 legacy 独立实例"
            )
            return None

        # 最少负载优先, 同负载取小引擎号 -> 32 路在 8 卡上严格均摊 (每卡 4 路)
        engine = min(candidates, key=lambda e: (e.device_count, e.engine_id))
        slot = engine.assign(device_id)
        lease = Lease(mode=self.mode, engine_id=engine.engine_id, slot=slot, engine=engine)
        self._leases[device_id] = lease
        return lease

    def release(self, device_id: str) -> None:
        """设备下线: 释放槽位 (跟踪状态随设备销毁, 避免污染下一路)."""
        lease = self._leases.pop(device_id, None)
        if lease is None:
            return
        lease.engine.unassign(device_id)

    # ---- CUDA 探测 ----

    def _probe_cuda(self) -> tuple:
        try:
            import torch
        except Exception as e:  # noqa: BLE001
            return False, f"torch 不可用: {e}"
        if not torch.cuda.is_available():
            return False, "CUDA 不可用 (CPU 部署或驱动缺失)"
        count = torch.cuda.device_count()
        if count <= 0:
            return False, "可见 CUDA 设备数为 0"
        names = []
        for i in range(count):
            try:
                names.append(torch.cuda.get_device_name(i))
            except Exception:  # noqa: BLE001
                names.append("unknown")
        return True, {"count": count, "names": names}

    def _target_gpus(self, detail: dict) -> list:
        """解析 infer_gpu_devices ("0,1,2"); 空=全部可见卡; 非法值忽略并告警."""
        count = int(detail.get("count", 0))
        raw = str(settings.infer_gpu_devices or "").strip()
        if not raw:
            return list(range(count))
        out = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                gpu_id = int(part)
            except ValueError:
                logger.warning(f"infer_gpu_devices 含非法项 {part!r}, 已忽略")
                continue
            if 0 <= gpu_id < count:
                out.append(gpu_id)
            else:
                logger.warning(f"infer_gpu_devices 卡号 {gpu_id} 超出可见范围 0-{count - 1}, 已忽略")
        return out

    # ---- 可观测性 ----

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "leased": len(self._leases),
            "engines": [e.snapshot() for e in self._engines],
        }


# 进程级单例 (由 app/ai/service.py 的 lifespan 初始化/关闭)
model_pool = ModelPool()
