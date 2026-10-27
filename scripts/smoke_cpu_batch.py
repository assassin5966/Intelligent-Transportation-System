"""cpu_batch 全链路冒烟: 加载 -> 自检 -> 多路组批 -> 释放, 直接跑在宿主机.

用法: python3 scripts/smoke_cpu_batch.py
"""
import asyncio
import os
import sys

os.environ["INFER_MODE"] = "cpu_batch"
os.environ["INFER_CPU_ENGINES"] = "2"
os.environ["INFER_IMGSZ"] = "640"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


async def main() -> int:
    from app.ai.model_pool import model_pool

    await model_pool.initialize()
    snap = model_pool.snapshot()
    print(f"\n[1] 模式={snap['mode']} 原因={snap['reason']} 引擎数={len(snap['engines'])}")
    for e in snap["engines"]:
        print(f"    {e['engine_id']}: device={e.get('device', '?')} 槽位={e.get('slots', '?')}")

    assert snap["mode"] == "cpu_batch", "应为 cpu_batch"

    # 三路设备分配槽位 (2 引擎, 验证最少负载分配)
    leases = {}
    for dev in ("cam-A", "cam-B", "cam-C"):
        lease = model_pool.acquire(dev)
        assert lease is not None, f"{dev} 未分配到槽位"
        leases[dev] = lease
        print(f"[2] {dev} -> {lease.engine_id} 槽位{lease.slot}")

    # 构造带移动目标的图, 三路并发提交 (验证跨设备组批: 应凑成 batch>1)
    async def submit_one(dev: str, lease, x: int):
        frame = np.zeros((1080, 1920, 3), np.uint8)
        frame[400:520, x:x + 160] = 255  # 亮块当"目标"
        outcome = await lease.engine.submit(dev, frame, (1080 / 1920, 1.0))
        print(f"[3] {dev} submit -> raw_tracks={len(outcome.raw_tracks)} "
              f"batch={outcome.batch_size} wait={outcome.wait_ms:.0f}ms infer={outcome.infer_ms:.0f}ms")

    t0 = asyncio.get_event_loop().time()
    xs = {"cam-A": 300, "cam-B": 600, "cam-C": 900}
    await asyncio.gather(*(submit_one(d, leases[d], xs[d]) for d in leases))
    print(f"[3] 三路并发组批总耗时 {asyncio.get_event_loop().time() - t0:.2f}s")

    # 释放后再分配, 验证槽位回收
    model_pool.release("cam-A")
    lease = model_pool.acquire("cam-D")
    assert lease is not None
    print(f"[4] 释放 cam-A 后 cam-D -> {lease.engine_id} 槽位{lease.slot}")

    await model_pool.shutdown()
    print("\n[5] 引擎已停止回收, 冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
