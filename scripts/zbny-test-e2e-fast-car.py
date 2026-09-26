"""端到端验证: 高速车漏检数帧, 经轨迹缝合后仍计一次 (不双计).

走真实计数链路 LineCrossingCounter.process_tracks, 构造与 ByteTracker.track()
输出同构的 Track (history[-1] == center 为当前点, history[-2] 为前一点):
  帧 1-6:  车 ID=1 匀速 40px/帧 驶向计数线 (像素 x=500), 至 x=470 (线外侧)
  帧 7-10: 漏检 (运动模糊), 缝隙中车从 x=470 行至 x=630 (已跨到内侧)
  帧 11:   车重现, BoT-SORT 分配新 ID=2 -> _stitch_history 缝合旧历史
  帧 12-14: 滞留确认 (继续向内侧行驶远离线)
对照: 不缝合则 ID=1 止于线外、ID=2 起于线内, 永不检出跨线 -> 漏计
预期: 恰好 1 次 enter, 0 次 exit (滞留确认后落定, 后续帧不重复计数)

运行: YOLO_CONFIG_DIR=/tmp/Ultralytics /tmp/zbny-venv/bin/python scripts/zbny-test-e2e-fast-car.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/zbny-test-e2e", exist_ok=True)
os.chdir("/tmp/zbny-test-e2e")
os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/zbny-test-e2e/Ultralytics")

from app.ai.counter import LineCrossingCounter
from app.ai.tracker import ByteTracker, Track, TrackResult

FRAME_W, FRAME_H = 1920, 1080
LINE_X = 500.0  # 像素 x=500 的垂直计数线 (归一化 500/1920)
SPEED = 40.0    # px/帧 (高速: 近景轿车 25fps 下约 9 m/s)


def make_track(tid, center, history, cls="car"):
    x, y = center
    return Track(
        track_id=str(tid), class_name=cls,
        bbox=[x - 60, y - 30, x + 60, y + 30],
        center=[x, y], confidence=0.7,
        age=len(history), velocity=[SPEED, 0.0],
        history=list(history),  # history[-1] == center
    )


def feed(counter, tracks, frame_id, t):
    result = TrackResult(frame_id=frame_id, tracks=tracks)
    return counter.process_tracks(result, camera_id="zbny-test-cam", current_time=t)


def main():
    counter = LineCrossingCounter(
        line=[[LINE_X / FRAME_W, 0.0], [LINE_X / FRAME_W, 1.0]],
        anchor=[0.4, 0.5],  # 归一化, x=768 > 500, 右侧为内侧
    )
    counter.set_frame_size(FRAME_W, FRAME_H)
    tracker = ByteTracker(camera_type="vehicle")
    # 同步 tracker 侧旧轨迹状态 (等价于 track() 处理过帧 1-5)
    tracker.track_class[1] = "car"
    t = 1000.0
    all_events = []

    # 帧 1-5: ID=1 匀速驶近, x: 310 -> 470 (始终在线外侧)
    hist = []
    x = 310.0
    for fid in range(1, 6):
        hist.append([x, 540.0])
        tracker.track_history[1] = [tuple(p) for p in hist]
        tr = make_track(1, [x, 540.0], hist)
        ev = feed(counter, [tr], fid, t)
        all_events += ev
        x += SPEED
        t += 0.04
    assert hist[-1][0] == 470.0  # 末帧中心 x=470, 线外侧 (offset<0)

    # 帧 6-9: 漏检 4 帧, 无 Track 输出; tracker 侧 miss 累计
    for fid in range(6, 10):
        for tid in list(tracker.track_history):
            tracker._state._miss_count[tid] = tracker._state._miss_count.get(tid, 0) + 1
        t += 0.04
    # 缝隙中车从 x=470 行至 x=630 (第 8-9 帧之间跨过线, 真实跨越发生在此)

    # 帧 11: 新 ID=2 重现于 x=630 (线内侧) -> 缝合
    stitched = tracker._state._stitch_history(2, "car", [570.0, 510.0, 690.0, 570.0])
    assert stitched, "缝合失败: 旧轨迹未被接续"
    tracker.track_class[2] = "car"
    tracker._state._stitched_from.add(1)
    tracker.track_history.pop(1, None)
    tracker._state._miss_count.pop(1, None)
    # 拼接历史: 旧轨迹 5 点 (末点 x=470) + 新位置 -> ID=2 当前历史
    full_hist = stitched + [[630.0, 540.0]]
    tracker.track_history[2] = [list(p) for p in full_hist]
    tr2 = make_track(2, [630.0, 540.0], full_hist)
    ev = feed(counter, [tr2], 11, t)
    all_events += ev
    print(f"帧11 (缝合后首帧): 事件 {[(e.event_type, e.track_id) for e in ev] or '无(待滞留确认)'}")
    t += 0.04

    # 帧 12-14: 滞留确认, 继续向内侧行驶 (远离线)
    h2 = [list(p) for p in full_hist]
    x = 670.0
    for fid in range(12, 15):
        h2.append([x, 540.0])
        tr = make_track(2, [x, 540.0], h2)
        ev = feed(counter, [tr], fid, t)
        all_events += ev
        x += SPEED
        t += 0.04

    enters = [e for e in all_events if "Enter" in e.event_type or e.event_type == "enter"]
    exits = [e for e in all_events if "Exit" in e.event_type or e.event_type == "exit"]
    print(f"\n端到端结果: Enter={len(enters)}, Exit={len(exits)}")
    print(f"拒因统计: {counter.pop_reject_stats()}")

    assert len(enters) == 1, f"预期恰好 1 次 Enter, 实得 {len(enters)}"
    assert len(exits) == 0, f"预期 0 次 Exit, 实得 {len(exits)}"
    print("PASS: 高速车漏检 4 帧, 轨迹缝合后仍计一次 Enter, 无双计")


if __name__ == "__main__":
    main()
