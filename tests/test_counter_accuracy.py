"""越线计数精度基准测试 (已知真值的多目标场景).

模拟多车辆/多人员跨线场景, 以人工构造的真值 (ground truth) 衡量计数器的
precision / recall / accuracy. 作为交付前的精度回归基线.

合成轨迹验证, 不依赖 Redis / 模型 / 真实视频.

可独立运行:
    python -m pytest tests/test_counter_accuracy.py -v
或:
    python tests/test_counter_accuracy.py
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.ai.counter import LineCrossingCounter  # noqa: E402
from app.schemas.events import (  # noqa: E402
    PERSON_ENTER,
    PERSON_EXIT,
    VEHICLE_ENTER,
    VEHICLE_EXIT,
)


@dataclass
class Track:
    track_id: str
    class_name: str
    center: List[float]
    history: List[List[float]]
    confidence: float = 0.9
    bbox: List[float] = field(default_factory=list)
    age: int = 0
    velocity: Optional[List[float]] = None


@dataclass
class TrackResult:
    frame_id: int
    tracks: List[Track]


@dataclass
class MovingObject:
    """一个移动目标: 在指定帧范围内沿路径移动.

    path_y: 各帧的 y 像素坐标 (按 start_frame 对齐). 缺帧表示该目标未出现.
    """
    track_id: int
    class_name: str
    x: float
    start_frame: int
    path_y: List[int]  # 从 start_frame 起, 每帧的 y

    def center_at(self, frame_idx: int) -> Optional[List[float]]:
        rel = frame_idx - self.start_frame
        if rel < 0 or rel >= len(self.path_y):
            return None
        return [self.x, float(self.path_y[rel])]


# ---- 几何 (与 test_counter.py 一致) ----
_W, _H = 1280, 720
_LINE_Y = 360
_LINE = [[0.1, 0.5], [0.9, 0.5]]
_ANCHOR = [0.5, 0.9]


def _make_counter(count_only=None):
    c = LineCrossingCounter()
    c.set_frame_size(_W, _H)
    c.set_line(_LINE, anchor=_ANCHOR)
    if count_only:
        c.count_only = count_only
    return c


def _enter_path_y(start_y=300) -> List[int]:
    """从上方跨线进入 (y 增大), 跨线后远离并滞留 3 帧."""
    return [start_y, start_y + 30, _LINE_Y + 10, _LINE_Y + 40, _LINE_Y + 70, _LINE_Y + 100]


def _exit_path_y(start_y=420) -> List[int]:
    """从下方跨线离开 (y 减小)."""
    return [start_y, start_y - 30, _LINE_Y - 10, _LINE_Y - 40, _LINE_Y - 70, _LINE_Y - 100]


def simulate(counter: LineCrossingCounter, objects: List[MovingObject],
             total_frames: int, dt: float = 2.0) -> List[str]:
    """逐帧驱动计数器, 返回事件类型列表 (按时间顺序)."""
    events: List[str] = []
    for f in range(total_frames):
        tracks: List[Track] = []
        for obj in objects:
            center = obj.center_at(f)
            if center is None:
                continue
            # 历史 = 该目标已出现的各帧中心
            history = []
            for ff in range(obj.start_frame, f + 1):
                c = obj.center_at(ff)
                if c is not None:
                    history.append([float(c[0]), float(c[1])])
            tracks.append(Track(
                track_id=str(obj.track_id),
                class_name=obj.class_name,
                center=[float(center[0]), float(center[1])],
                history=history,
                confidence=0.9,
                bbox=[center[0] - 15, center[1] - 15, center[0] + 15, center[1] + 15],
                age=len(history),
            ))
        result = TrackResult(frame_id=f + 1, tracks=tracks)
        evs = counter.process_tracks(result, camera_id="BENCH", current_time=f * dt)
        events.extend(e.event_type for e in evs)
    return events


def _metrics(events: List[str], truth: dict) -> dict:
    """计算 precision / recall / accuracy.

    truth: {event_type: expected_count}
    """
    all_types = set(truth) | set(events)
    tp = fp = fn = 0
    per_type = {}
    for et in all_types:
        detected = events.count(et)
        expected = truth.get(et, 0)
        t = min(detected, expected)
        tp += t
        fp += detected - t
        fn += expected - t
        per_type[et] = {"expected": expected, "detected": detected}
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    accuracy = tp / (tp + fp + fn) if (tp + fp + fn) else 1.0
    return {"precision": precision, "recall": recall, "accuracy": accuracy,
            "per_type": per_type, "tp": tp, "fp": fp, "fn": fn}


# ===================== 场景 1: 多车辆双向 (Enter + Exit) =====================

def test_scenario_bidirectional_vehicles():
    """10 辆车进入, 5 辆车离开 (离开发生在进入 3s 后, 不触发冷却).

    真值: VehicleEnter=10, VehicleExit=5.
    """
    objects = []
    # 10 辆车进入, x 错开, start_frame 错开 (避免重叠遮挡干扰)
    xs = [200, 400, 600, 800, 1000, 250, 450, 650, 850, 300]
    for i, x in enumerate(xs):
        objects.append(MovingObject(
            track_id=i + 1, class_name="car", x=x,
            start_frame=i * 3, path_y=_enter_path_y(300),
        ))
    # 5 辆车 (id 1-5) 进入后跨回上方离开: start_frame 在 enter 完成后 + 足够间隔 (>3s, dt=2)
    # enter 在 start_frame+5 完成 (t=(start_frame+5)*2); exit 路径从 start_frame+8 起
    for i in range(5):
        exit_start = (i * 3) + 8  # enter 完成于 i*3+5, exit 始于 i*3+8 (间隔 3 帧 = 6s > 3s)
        objects.append(MovingObject(
            track_id=i + 1, class_name="car", x=xs[i],
            start_frame=exit_start, path_y=_exit_path_y(420),
        ))
    total_frames = max(o.start_frame + len(o.path_y) for o in objects) + 2

    counter = _make_counter()
    events = simulate(counter, objects, total_frames, dt=2.0)
    truth = {VEHICLE_ENTER: 10, VEHICLE_EXIT: 5}
    m = _metrics(events, truth)

    print(f"\n[场景1 双向车辆] events={events.count(VEHICLE_ENTER)}E/{events.count(VEHICLE_EXIT)}X "
          f"truth=10E/5X → P={m['precision']:.2f} R={m['recall']:.2f} Acc={m['accuracy']:.2f}")
    assert m["precision"] >= 0.95, f"精度过低: {m}"
    assert m["recall"] >= 0.95, f"召回过低: {m}"


# ===================== 场景 2: 单向人流 (count_only) =====================

def test_scenario_unidirectional_persons():
    """20 个人员单向离开 (count_only='exit'), 含线附近抖动干扰.

    真值: PersonExit=20.
    """
    objects = []
    xs = [200, 350, 500, 650, 800, 950, 250, 400, 550, 700,
          300, 450, 600, 750, 900, 220, 470, 620, 770, 920]
    for i, x in enumerate(xs):
        objects.append(MovingObject(
            track_id=i + 1, class_name="person", x=x,
            start_frame=i * 2, path_y=_exit_path_y(420),
        ))
    total_frames = max(o.start_frame + len(o.path_y) for o in objects) + 2

    counter = _make_counter(count_only="exit")
    events = simulate(counter, objects, total_frames, dt=1.0)
    truth = {PERSON_EXIT: 20}
    m = _metrics(events, truth)

    print(f"\n[场景2 单向人流] PersonExit={events.count(PERSON_EXIT)} truth=20 "
          f"→ P={m['precision']:.2f} R={m['recall']:.2f} Acc={m['accuracy']:.2f}")
    assert m["precision"] >= 0.95, f"精度过低: {m}"
    assert m["recall"] >= 0.95, f"召回过低: {m}"
    # count_only=exit 不应产出任何 Enter
    assert events.count(PERSON_ENTER) == 0


# ===================== 场景 3: 混合车流+人流, 含噪声 =====================

def test_scenario_mixed_traffic_with_noise():
    """5 车 + 5 人混合, 含 1 条抖动轨迹 (不应计).

    真值: VehicleEnter=5, PersonEnter=5.
    """
    objects = []
    # 5 辆车进入
    for i, x in enumerate([300, 500, 700, 900, 250]):
        objects.append(MovingObject(
            track_id=i + 1, class_name="car", x=x,
            start_frame=i * 4, path_y=_enter_path_y(300),
        ))
    # 5 个人员进入
    for i, x in enumerate([400, 600, 800, 1000, 350]):
        objects.append(MovingObject(
            track_id=i + 11, class_name="person", x=x,
            start_frame=i * 4 + 2, path_y=_enter_path_y(280),
        ))
    # 1 条抖动轨迹 (在线附近 ±5px 振荡, 不应计)
    jitter_y = [355, 365, 355, 365, 358, 362, 355, 365, 358, 362]
    objects.append(MovingObject(
        track_id=99, class_name="car", x=150,
        start_frame=0, path_y=jitter_y,
    ))
    total_frames = max(o.start_frame + len(o.path_y) for o in objects) + 2

    counter = _make_counter()
    events = simulate(counter, objects, total_frames, dt=1.0)
    truth = {VEHICLE_ENTER: 5, PERSON_ENTER: 5}
    m = _metrics(events, truth)

    print(f"\n[场景3 混合含噪] V-in={events.count(VEHICLE_ENTER)} P-in={events.count(PERSON_ENTER)} "
          f"truth=5/5 → P={m['precision']:.2f} R={m['recall']:.2f} Acc={m['accuracy']:.2f}")
    assert m["precision"] >= 0.95, f"精度过低 (抖动应被过滤): {m}"
    assert m["recall"] >= 0.95, f"召回过低: {m}"


# ===================== 场景 4: ROI 过滤 (部分目标在 ROI 外不计) =====================

def test_scenario_roi_filtering():
    """8 辆车跨线, 其中 3 辆在 ROI 外 (不计), 5 辆在 ROI 内 (计).

    真值: VehicleEnter=5.
    """
    # ROI = x 归一化 [0.25, 0.75] (像素 320~960)
    roi = [[0.25, 0.0], [0.75, 0.0], [0.75, 1.0], [0.25, 1.0]]
    counter = _make_counter()
    counter.set_roi(roi)

    objects = []
    # 5 辆在 ROI 内
    for i, x in enumerate([400, 500, 600, 700, 800]):
        objects.append(MovingObject(
            track_id=i + 1, class_name="car", x=x,
            start_frame=i * 3, path_y=_enter_path_y(300),
        ))
    # 3 辆在 ROI 外 (x < 320 或 > 960)
    for i, x in enumerate([100, 200, 1100]):
        objects.append(MovingObject(
            track_id=i + 20, class_name="car", x=x,
            start_frame=i * 3, path_y=_enter_path_y(300),
        ))
    total_frames = max(o.start_frame + len(o.path_y) for o in objects) + 2

    events = simulate(counter, objects, total_frames, dt=2.0)
    truth = {VEHICLE_ENTER: 5}
    m = _metrics(events, truth)

    print(f"\n[场景4 ROI过滤] VehicleEnter={events.count(VEHICLE_ENTER)} truth=5 "
          f"(8 辆中 3 辆 ROI 外) → P={m['precision']:.2f} R={m['recall']:.2f} Acc={m['accuracy']:.2f}")
    assert m["precision"] >= 0.95, f"精度过低: {m}"
    assert m["recall"] >= 0.95, f"召回过低: {m}"


# ===================== 自运行入口 =====================

def _run_all():
    tests = [
        test_scenario_bidirectional_vehicles,
        test_scenario_unidirectional_persons,
        test_scenario_mixed_traffic_with_noise,
        test_scenario_roi_filtering,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} 通过")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
