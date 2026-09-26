#!/usr/bin/env python3
"""GPU 批量推理基准 (1→8→32 路): 有效 fps / 帧延迟 / 均批 / 逐卡显存.

用途: 对齐《GPU推理性能优化技术方案》第二节的量化目标 (单路有效 fps ≥ 8,
8 路单批 ≤ 150ms, 各卡设备数与显存均衡), 并验证"路数翻倍不出现排队雪崩".

不需要真实视频流: 用合成帧直接压 `GpuInferEngine.submit` 路径, 把
"组批 + 推理 + 跟踪" 本身的性能从拉流/解码中隔离出来.

在 GPU 容器内运行 (需要 CUDA; compose 已把 scripts/ 挂到 /app/scripts):
  docker compose --env-file .env.gpu -f docker-compose.gpu.yml exec -T ai \\
      python scripts/zbny-bench-gpu-infer.py --levels 1,4,8,16,32

参数:
  --levels      逐级压测的路数 (逗号分隔), 默认 1,8,32
  --rounds      每级正式测量轮数 (每轮各路各提交 1 帧), 默认 30
  --warmup      预热轮数 (不计入统计), 默认 3
  --gpus        参与压测的卡号 (逗号分隔), 默认全部可见卡
  --short-side  合成帧短边 (默认取 settings.infer_imgsz, 模拟预降采样后的输入)
"""
import argparse
import asyncio
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

# 注: app.* 的导入延迟到函数内 (见 _build_engines) —— app.ai.tracker 依赖 ultralytics,
# 在 CPU 容器里导入即报错, 会让"无 CUDA 直接 SKIP"的友好退出路径失效.


def _make_frame(short_side: int) -> np.ndarray:
    """合成一张 16:9 帧 (短边 = short_side).

    固定随机种子: 同一级内所有路复用同一张帧, 既省内存, 也保证各轮检测数一致
    (基准只为对比吞吐/延迟, 不关心检测内容).
    """
    h = int(short_side)
    w = int(round(short_side * 16 / 9))
    rng = np.random.default_rng(0)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * len(ordered))) - 1))
    return ordered[idx]


async def _build_engines(gpus: list, per_gpu: int) -> list:
    from app.ai.gpu_engine import GpuInferEngine
    from app.ai.tracker import TRACKER_CFG
    from app.common.config import settings

    engines = []
    for gpu_id in gpus:
        engine = GpuInferEngine(
            gpu_id,
            model_path=settings.yolo_model,
            # 槽位必须 >= 每卡路数, 否则 assign 取不到空槽位
            slots=per_gpu,
            batch_timeout_s=max(0.0, int(settings.infer_batch_timeout_ms) / 1000.0),
            queue_max_batches=int(settings.infer_queue_max_batches),
            tracker_cfg=TRACKER_CFG,
            imgsz=int(settings.infer_imgsz),
            half=bool(settings.infer_half),
        )
        await asyncio.to_thread(engine.start_blocking)
        engine.attach_loop()
        engines.append(engine)
    return engines


async def run_level(level: int, gpus: list, rounds: int, warmup: int,
                    short_side: int) -> dict:
    """压测单一级别 (共 level 路, 均摊到 gpus 上), 返回本级的统计结果."""
    from app.ai.gpu_engine import EngineBusy

    per_gpu = math.ceil(level / len(gpus))
    engines = await _build_engines(gpus, per_gpu)
    frame = _make_frame(short_side)

    # 路 -> 引擎: 轮流分配, 与 ModelPool 的"最少负载"结果一致 (每卡差 <= 1)
    devices = []  # [(engine, device_id)]
    for i in range(level):
        engine = engines[i % len(engines)]
        dev = f"bench-{i}"
        engine.assign(dev)
        devices.append((engine, dev))

    def submit_all():
        return [
            engine.submit(dev, frame, (1.0, 1.0)) for engine, dev in devices
        ]

    async def one_round() -> tuple:
        t0 = time.monotonic()
        outs = await asyncio.gather(*submit_all(), return_exceptions=True)
        elapsed = time.monotonic() - t0
        latencies, batches, busy = [], [], 0
        for out in outs:
            if isinstance(out, EngineBusy):
                busy += 1
                continue
            if isinstance(out, BaseException):
                raise out
            latencies.append(out.wait_ms + out.infer_ms)
            batches.append(out.batch_size)
        return elapsed, latencies, batches, busy

    # 预热: 首轮含 lazy 初始化与 cudnn 算法选择, 不计入
    for _ in range(max(0, warmup)):
        await one_round()

    round_ms, lat_all, batch_all, busy_total = [], [], [], 0
    vram_peak = 0.0
    try:
        import torch
        for engine in engines:
            torch.cuda.reset_peak_memory_stats(engine.gpu_id)
    except Exception:  # noqa: BLE001
        torch = None

    for _ in range(max(1, rounds)):
        elapsed, latencies, batches, busy = await one_round()
        round_ms.append(elapsed * 1000.0)
        lat_all.extend(latencies)
        batch_all.extend(batches)
        busy_total += busy

    if torch is not None:
        vram_peak = max(
            (torch.cuda.max_memory_allocated(e.gpu_id) / 1024 / 1024 for e in engines),
            default=0.0,
        )

    result = {
        "level": level,
        "per_gpu": per_gpu,
        "gpus": len(engines),
        "rounds": rounds,
        "fps": (level * rounds) / max(sum(round_ms) / 1000.0, 1e-9),
        "round_ms_p50": _percentile(round_ms, 50),
        "round_ms_p95": _percentile(round_ms, 95),
        "lat_p50": _percentile(lat_all, 50),
        "lat_p95": _percentile(lat_all, 95),
        "batch_mean": statistics.fmean(batch_all) if batch_all else 0.0,
        "batch_max": max(batch_all) if batch_all else 0,
        "vram_peak_mb": vram_peak,
        "busy_drops": busy_total,
        "engines": [e.snapshot() for e in engines],
    }

    for engine in engines:
        await engine.stop()
    return result


def _print_level(r: dict) -> None:
    print(f"\n=== 路数 {r['level']} ({r['gpus']} 卡 x {r['per_gpu']} 路, {r['rounds']} 轮) ===")
    print(f"  有效 fps      : {r['fps']:.2f} (总吞吐, 含全部路)")
    print(f"  单路帧延迟    : P50 {r['lat_p50']:.1f}ms / P95 {r['lat_p95']:.1f}ms"
          f"   (等待组批 + 推理)")
    print(f"  单轮墙钟耗时  : P50 {r['round_ms_p50']:.1f}ms / P95 {r['round_ms_p95']:.1f}ms")
    print(f"  批大小        : 均值 {r['batch_mean']:.2f} / 最大 {r['batch_max']}")
    print(f"  峰值显存(单卡): {r['vram_peak_mb']:.0f} MB")
    if r["busy_drops"]:
        print(f"  EngineBusy 丢帧: {r['busy_drops']} (队列饱和, 需下调路数或增大 queue_max_batches)")
    for e in r["engines"]:
        print(f"  - GPU {e['gpu_id']}: 设备={e['devices']} 批次数={e['batches']} "
              f"末批推理={e['last_infer_ms']}ms 显存={e['vram_mb']}MB")


def _print_summary(results: list) -> None:
    print("\n=== 汇总 ===")
    header = f"{'路数':>6}{'每卡路数':>10}{'有效fps':>10}{'帧延迟P50':>12}{'帧延迟P95':>12}{'均批':>8}{'峰值显存MB':>12}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(f"{r['level']:>6}{r['per_gpu']:>10}{r['fps']:>10.2f}"
              f"{r['lat_p50']:>12.1f}{r['lat_p95']:>12.1f}"
              f"{r['batch_mean']:>8.2f}{r['vram_peak_mb']:>12.0f}")
    print("\n对照目标: 单路帧延迟 P50 ≤ 130ms (由有效 fps ≥ 8 反推); "
          "均批应等于每卡路数 (组批阈值=本卡设备数); 8 路单批推理 ≤ 150ms")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GPU 批量推理基准 (离线合成帧, 不含拉流/解码)"
    )
    parser.add_argument("--levels", default="1,8,32", help="逐级路数, 逗号分隔")
    parser.add_argument("--rounds", type=int, default=30, help="每级测量轮数")
    parser.add_argument("--warmup", type=int, default=3, help="每级预热轮数")
    parser.add_argument("--gpus", default="", help="参与压测的卡号, 逗号分隔; 空=全部可见卡")
    parser.add_argument("--short-side", type=int, default=0,
                        help="合成帧短边; 默认取 settings.infer_imgsz")
    args = parser.parse_args()

    try:
        import torch
    except ImportError:
        print("SKIP: 未安装 torch, 本脚本需在 GPU 容器内运行")
        return 0
    if not torch.cuda.is_available():
        print("SKIP: CUDA 不可用 (CPU 部署自动走 legacy), 本脚本需在 GPU 容器内运行")
        return 0

    visible = torch.cuda.device_count()
    if args.gpus.strip():
        gpus = [int(x) for x in args.gpus.split(",") if x.strip() != ""]
        bad = [g for g in gpus if g < 0 or g >= visible]
        if bad:
            print(f"ERROR: 卡号 {bad} 超出可见范围 (可见 {visible} 张)")
            return 2
    else:
        gpus = list(range(visible))

    levels = [int(x) for x in args.levels.split(",") if x.strip() != ""]

    from app.common.config import settings

    short_side = args.short_side or int(settings.infer_imgsz)

    print(f"推理模式基准: 卡={gpus} | imgsz={settings.infer_imgsz} half={settings.infer_half} "
          f"batch_window={settings.infer_batch_timeout_ms}ms | 合成帧短边={short_side}")

    async def run_all() -> list:
        out = []
        for level in levels:
            out.append(await run_level(level, gpus, args.rounds, args.warmup, short_side))
            _print_level(out[-1])
        return out

    results = asyncio.run(run_all())
    _print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
