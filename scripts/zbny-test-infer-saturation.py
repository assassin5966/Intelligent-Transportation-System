"""模拟 8 路并发推理, 验证 InferBusy 丢帧与饱和统计符合预期.

不依赖 cv2/ultralytics: 推理函数用 time.sleep 模拟 CPU 耗时,
按真实 DevicePipeline._process_frame 的调用路径走 inference_scheduler.

场景:
  workers=2, max_queued=1 -> 池容量 3
  8 路 pipeline 同时以 25fps 节奏送帧, 每帧推理耗时 30ms (模拟 CPU 推理)
  池容量 3 * (1/0.03) = 100 fps 处理上限, 8 路 * 25fps = 200 fps 需求
  -> 必然出现饱和丢弃, 且各路丢帧分布大致均匀 (不会某一路被饿死)

验证点:
  1. InferBusy 丢弃数 > 0 (饱和生效)
  2. 丢弃数 + 完成帧数 = 总送帧数 (对账无黑洞)
  3. inference_scheduler.queue_drop_count 与测试侧统计一致
  4. 成功路径各路均分推理机会 (最大/最小 <= 2, 无饿死)
  5. 实测吞吐 <= workers/单帧耗时 (并发确实被限制在 CPU 核数内)

运行: python3 scripts/zbny-test-infer-saturation.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 环境变量必须先于 app.common.config 导入
os.environ["INFER_MAX_WORKERS"] = "2"
os.environ["INFER_MAX_QUEUED"] = "1"

# logger 文件日志写 logs/ (宿主机上该目录归容器 root 所有时不可写):
# 测试进程切到临时工作目录, 让 logs/ 落在可写位置, 不影响被测逻辑
_TMP_DIR = os.path.join("/tmp", "zbny-test-infer-saturation")
os.makedirs(_TMP_DIR, exist_ok=True)
os.chdir(_TMP_DIR)

import app.ai.inference_scheduler as sched
from app.ai.inference_scheduler import InferBusy, run_inference

WORKERS = 2
DEVICES = 8
FPS = 25
FRAME_INTERVAL = 1.0 / FPS
INFER_COST = 0.03  # 模拟单帧推理耗时 30ms
TEST_SECONDS = 3.0


def fake_infer(frame_id: int) -> int:
    """模拟 CPU 推理: 固定耗时, 返回帧号供对账."""
    time.sleep(INFER_COST)
    return frame_id


async def device_loop(device_id: int, deadline: float):
    """单路设备协程: 模拟 pipeline 主循环的送帧节奏与 InferBusy 处理."""
    sent = 0
    busy = 0
    ok = 0
    while time.monotonic() < deadline:
        sent += 1
        try:
            await run_inference(fake_infer, sent)
            ok += 1
        except InferBusy:
            busy += 1
        await asyncio.sleep(FRAME_INTERVAL)  # 25fps 送帧节奏
    return device_id, sent, ok, busy


async def main():
    deadline = time.monotonic() + TEST_SECONDS
    t0 = time.monotonic()
    results = await asyncio.gather(*[device_loop(i, deadline) for i in range(DEVICES)])
    elapsed = time.monotonic() - t0

    total_sent = total_ok = total_busy = 0
    print(f"{'device':>6} {'sent':>6} {'inferred':>9} {'busy_dropped':>13}")
    for dev, sent, ok, busy in sorted(results):
        print(f"{dev:>6} {sent:>6} {ok:>9} {busy:>13}")
        total_sent += sent
        total_ok += ok
        total_busy += busy

    print("-" * 40)
    print(f"总送帧: {total_sent}, 推理完成: {total_ok}, 饱和丢弃: {total_busy}")
    print(f"scheduler.queue_drop_count = {sched.queue_drop_count}")
    print(f"实测吞吐: {total_ok / elapsed:.1f} fps "
          f"(理论上限 workers/{INFER_COST}s = {WORKERS / INFER_COST:.0f} fps)")

    # ---- 断言 ----
    oks = [r[2] for r in results]
    assert total_busy > 0, "未出现饱和丢弃: 并发压测未触顶, 测试无效"
    assert total_ok + total_busy == total_sent, "帧对账不平: 存在既未成功也未丢弃的请求"
    assert sched.queue_drop_count == total_busy, (
        f"调度器累计丢弃 {sched.queue_drop_count} != 测试侧统计 {total_busy}"
    )
    assert max(oks) / max(min(oks), 1) <= 2.0, (
        f"某路被饿死: 各路成功帧 {sorted(oks)}"
    )
    assert total_ok / elapsed <= WORKERS / INFER_COST * 1.15, (
        "实际吞吐超过 workers/单帧耗时, 说明并发未被限制"
    )
    print("PASS: 饱和丢弃与并发上限均符合预期")


if __name__ == "__main__":
    asyncio.run(main())
