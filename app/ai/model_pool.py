"""模型池: 每张 GPU 一个批量推理引擎, 设备按"最少负载"均摊到各卡.

解决的三类现场问题:
  P1 CUDA context 频繁切换 —— 模型实例数从 "每路一个" 降到 "每卡一个"
     (32 路 -> 8 个), 且每个实例只被自己的单线程 executor 触碰.
  P2 异步实例化资源管理 —— 所有模型在 lifespan 启动阶段一次性加载/预热/自检
     (asyncio.to_thread), 不再在推理 worker 线程里懒加载 CUDA context;
     设备下线时 release() 释放槽位, 服务关闭时 shutdown() 真正回收显存.
  P3 GPU 负载不均衡 —— acquire() 用 min(engines, key=(设备数, 卡号)) 选卡,
     32 路严格均摊为每卡 4 路.

降级策略 (任一命中即整体走 legacy = 改动前的行为, 可一键回滚):
  - 配置 infer_mode=legacy;
  - torch.cuda.is_available() == False (CPU 部署);
  - 可见卡数为 0;
  - 任一引擎加载/预热/自检失败 (TrackerApiError / RuntimeError).
legacy 模式下 acquire() 恒返回 None, 调用方 (DevicePipeline) 退回本地
ByteTracker + inference_scheduler, 行为与优化前完全一致.
"""
import asyncio
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
    """一路设备对某张卡某个槽位的占用凭证 (随设备生命周期存在)."""

    gpu_id: int
    slot: int
    engine: GpuInferEngine


class ModelPool:
    """显存级资源池: 管理 8 张卡上的 GpuInferEngine 与槽位分配."""

    def __init__(self) -> None:
        self.mode: str = "legacy"          # gpu_batch | legacy
        self.reason: str = "not_initialized"  # 降级原因 (供 /health 展示)
        self._engines: list = []
        self._leases: dict = {}            # device_id -> Lease
        self._initialized = False

    # ---- 生命周期 ----

    async def initialize(self) -> None:
        """服务对外前完成: 探测 CUDA -> 每卡加载引擎 -> 自检 -> 失败整体降级."""
        if self._initialized:
            return
        self._initialized = True

        if str(settings.infer_mode).lower() == "legacy":
            self.mode = "legacy"
            self.reason = "infer_mode=legacy (配置强制)"
            logger.warning("推理模式: legacy (配置强制, 每路独立模型实例)")
            return

        ok, detail = self._probe_cuda()
        if not ok:
            self.mode = "legacy"
            self.reason = detail
            logger.warning(f"推理模式: legacy ({detail})")
            return

        gpus = self._target_gpus(detail)
        if not gpus:
            self.mode = "legacy"
            self.reason = "无可用 GPU 卡"
            logger.warning("推理模式: legacy (无可用 GPU 卡)")
            return

        slots = max(1, int(settings.infer_slots_per_gpu))
        timeout_s = max(0.0, int(settings.infer_batch_timeout_ms) / 1000.0)

        t0 = time.monotonic()
        engine = None
        try:
            for gpu_id in gpus:
                engine = GpuInferEngine(
                    gpu_id,
                    model_path=settings.yolo_model,
                    slots=slots,
                    batch_timeout_s=timeout_s,
                    queue_max_batches=int(settings.infer_queue_max_batches),
                    tracker_cfg=TRACKER_CFG,
                    imgsz=int(settings.infer_imgsz),
                    half=bool(settings.infer_half),
                )
                # 同步加载放在线程内, 避免阻塞事件循环 (加载含 CUDA 初始化, 秒级)
                await asyncio.to_thread(engine.start_blocking)
                engine.attach_loop()
                self._engines.append(engine)
                engine = None  # 已纳入池管理, 由 _shutdown_engines 负责回收
        except Exception as e:  # noqa: BLE001  (加载/预热/自检任一失败即整体降级)
            # 自检/预热/加载任一失败: 整体降级, 已建的引擎就地回收, 不留半成品
            self.reason = f"引擎自检/加载失败: {type(e).__name__}: {e}"
            logger.warning(f"推理模式: legacy ({self.reason})")
            # 失败那台尚未入池, 需单独停掉: 它可能已把模型搬到显存 (start_blocking
            # 中途抛错), 不回收会一直占着显存直到进程退出
            await self._stop_engine(engine)
            await self._shutdown_engines()
            self.mode = "legacy"
            return

        self.mode = "gpu_batch"
        self.reason = "ok"
        logger.info(
            f"推理模式: gpu_batch | {len(self._engines)} 卡 x {slots} 槽位 | "
            f"加载总耗时 {time.monotonic() - t0:.1f}s | "
            f"imgsz={settings.infer_imgsz} half={settings.infer_half} "
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
            logger.warning(f"[GPU {engine.gpu_id}] 引擎停止异常: {e}")

    # ---- 槽位分配 ----

    def acquire(self, device_id: str) -> Optional[Lease]:
        """为新设备分配槽位; legacy 模式或全部槽位耗尽返回 None (调用方回退 legacy)."""
        if self.mode != "gpu_batch":
            return None
        existing = self._leases.get(device_id)
        if existing is not None:
            return existing

        candidates = [e for e in self._engines if e.has_capacity()]
        if not candidates:
            logger.warning(
                f"GPU 槽位已满 ({len(self._engines)} 卡), {device_id} 降级为 legacy 独立实例"
            )
            return None

        # 最少负载优先, 同负载取小卡号 -> 32 路在 8 卡上严格均摊 (每卡 4 路)
        engine = min(candidates, key=lambda e: (e.device_count, e.gpu_id))
        slot = engine.assign(device_id)
        lease = Lease(gpu_id=engine.gpu_id, slot=slot, engine=engine)
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
