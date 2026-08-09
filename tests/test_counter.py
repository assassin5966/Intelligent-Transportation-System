"""越线计数器 (LineCrossingCounter) 单元测试.

覆盖防抖链各环节: 跨线检测 / 夹角过滤 / 投影落段 / 远离线 / 端点过滤 /
滞留确认 / 方向判定 / ID 切换检测 / 双向去重 / 反向冷却 / count_only / ROI.

合成轨迹验证, 不依赖 Redis / 模型 / 真实视频.

可独立运行:
    python -m pytest tests/test_counter.py -v
或:
    python tests/test_counter.py
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


# ---- 轻量级 Track / TrackResult (避免引入 ultralytics 依赖) ----
# counter.py 对 track 是鸭子类型, 只需 .track_id/.class_name/.center/.history/.confidence
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

# ---- 测试几何 ----
# 1280x720 画面, 水平计数线 y=0.5 (像素 y=360), 锚点 (0.5, 0.9) 在线下方 (内侧).
# Enter = 外→内 = 从上往下 (y 增大), Exit = 内→外 = 从下往上 (y 减小).
_W, _H = 1280, 720
_LINE_Y = 360  # 像素 y
_LINE = [[0.1, 0.5], [0.9, 0.5]]
_ANCHOR = [0.5, 0.9]


def _make_counter(count_only=None, roi=None, hold_frames=3):
    """构造已 set_frame_size 的计数器."""
    c = LineCrossingCounter()
    c.set_frame_size(_W, _H)
    c.set_line(_LINE, anchor=_ANCHOR)
    if count_only is not None:
        c.count_only = count_only
    if roi is not None:
        c.set_roi(roi)
    c.hold_frames = hold_frames
    return c


def _track(track_id, class_name, history):
    """构造 Track (history 为像素坐标点列表)."""
    cx, cy = history[-1]
    return Track(
        track_id=str(track_id),
        class_name=class_name,
        center=[float(cx), float(cy)],
        history=[[float(p[0]), float(p[1])] for p in history],
        confidence=0.9,
        bbox=[cx - 15, cy - 15, cx + 15, cy + 15],
        age=len(history),
        velocity=[0.0, 0.0],
    )


def run_path(counter, track_id, class_name, points, times=None, dt=1.0):
    """沿像素点序列逐帧驱动计数器, 返回累计事件.

    points: 每帧的像素中心点 [(x,y), ...]; 每帧该轨迹历史 = points[:i+1].
    times: 每帧的 current_time (秒); 缺省按 dt 线性递增 (重要: 冷却测试用小 dt).
    """
    events = []
    for i, pt in enumerate(points):
        track = _track(track_id, class_name, points[: i + 1])
        result = TrackResult(frame_id=i + 1, tracks=[track])
        t = times[i] if times is not None else i * dt
        evs = counter.process_tracks(result, camera_id="TEST", current_time=t)
        events.extend(evs)
    return events


def _enter_path(start_y=300, step=30, frames=6, x=640):
    """构造一条从上往下跨线的轨迹 (Enter). 跨线后远离 >=20px 并滞留 3 帧."""
    # F0,F1 在线上方; F2 刚跨线 (距线<20, 不 clear); F3+ 远离并滞留
    ys = [start_y, start_y + step, _LINE_Y + 10, _LINE_Y + 40, _LINE_Y + 70, _LINE_Y + 100]
    return [(x, y) for y in ys[:frames]]


def _exit_path(start_y=420, step=30, frames=6, x=640):
    """构造一条从下往上跨线的轨迹 (Exit)."""
    ys = [start_y, start_y - step, _LINE_Y - 10, _LINE_Y - 40, _LINE_Y - 70, _LINE_Y - 100]
    return [(x, y) for y in ys[:frames]]


# ===================== 基本跨线计数 =====================

def test_basic_vehicle_enter():
    """车辆从外→内跨线 → 1 个 VehicleEnter."""
    c = _make_counter()
    events = run_path(c, 1, "car", _enter_path())
    assert len(events) == 1
    assert events[0].event_type == VEHICLE_ENTER
    assert events[0].direction == "outer_to_inner"


def test_basic_vehicle_exit():
    """车辆从内→外跨线 → 1 个 VehicleExit."""
    c = _make_counter()
    events = run_path(c, 1, "car", _exit_path())
    assert len(events) == 1
    assert events[0].event_type == VEHICLE_EXIT
    assert events[0].direction == "inner_to_outer"


def test_person_enter_exit():
    """person 类别 → PersonEnter/PersonExit (人流分类)."""
    c = _make_counter()
    e1 = run_path(c, 1, "person", _enter_path())
    assert len(e1) == 1 and e1[0].event_type == PERSON_ENTER

    c2 = _make_counter()
    e2 = run_path(c2, 1, "person", _exit_path())
    assert len(e2) == 1 and e2[0].event_type == PERSON_EXIT


def test_truck_and_bus_are_vehicle():
    """truck/bus 归车流; bicycle/motorcycle 归人流."""
    for cls in ("truck", "bus"):
        c = _make_counter()
        ev = run_path(c, 1, cls, _enter_path())
        assert len(ev) == 1 and ev[0].event_type == VEHICLE_ENTER, cls
    for cls in ("bicycle", "motorcycle"):
        c = _make_counter()
        ev = run_path(c, 1, cls, _enter_path())
        assert len(ev) == 1 and ev[0].event_type == PERSON_ENTER, cls


# ===================== 边界 / 防抖 =====================

def test_no_crossing_same_side():
    """轨迹始终在线一侧 → 0 事件."""
    c = _make_counter()
    # 始终在线上方 (y<360) 移动
    points = [(640, 300), (640, 320), (640, 310), (640, 330), (640, 300), (640, 320)]
    events = run_path(c, 1, "car", points)
    assert events == []


def test_jitter_near_line_no_false_event():
    """线附近 ±5px 抖动 (滞回带内) → 0 事件 (不触发方向反转/误计)."""
    c = _make_counter()
    # 在线两侧小幅抖动, 始终 < min_distance_threshold(20px) 且 < hysteresis(28px)
    points = [(640, 355), (640, 365), (640, 355), (640, 365),
              (640, 358), (640, 362), (640, 355), (640, 365)]
    events = run_path(c, 1, "car", points)
    assert events == []


def test_hold_frames_required():
    """跨线后仅滞留 2 帧 (< hold_frames=3) → 不产出事件."""
    c = _make_counter(hold_frames=3)
    # F0 上方, F1 上方, F2 刚跨线(不clear), F3 clear(hold1), F4 hold2 → 结束 (无第3帧)
    points = [(640, 300), (640, 330), (640, 370), (640, 400), (640, 430)]
    events = run_path(c, 1, "car", points)
    assert events == []


def test_dedup_same_direction():
    """同一轨迹同方向跨线两次 (TTL 内) → 仅计 1 次 (去重)."""
    c = _make_counter()
    # 第一次 Enter
    points = list(_enter_path())
    # 跨回上方 (不计, 因 enter 已 dedup; 这里测同方向再跨需先回到外侧再进入)
    # 完整: enter → 回到上方 → 再 enter (同方向第二次)
    points += [(640, 430), (640, 400), (640, 370),  # 回到线上方
               (640, 330), (640, 300),  # 上方滞留
               (640, 370), (640, 400), (640, 430), (640, 460), (640, 490)]  # 再次 enter
    events = run_path(c, 1, "car", points, dt=1.0)
    enter_count = sum(1 for e in events if e.event_type == VEHICLE_ENTER)
    assert enter_count == 1, f"同方向应去重, 实际 {enter_count}"


# ===================== 双向 / 冷却 =====================

def test_bidirectional_enter_then_exit_after_cooldown():
    """同一轨迹 Enter 后 (>3s) Exit → 2 事件 (双向各计一次)."""
    c = _make_counter()
    # Enter (dt=2.0, enter 在 t=10), 之后回到上方 Exit (t=22, 间隔 12s > 3s)
    points = list(_enter_path())  # F0-F5
    # 回到上方跨线 Exit
    points += [(640, 430), (640, 400), (640, 350),  # 跨回上方
               (640, 320), (640, 290), (640, 260)]  # 滞留确认
    events = run_path(c, 1, "car", points, dt=2.0)
    types = [e.event_type for e in events]
    assert VEHICLE_ENTER in types
    assert VEHICLE_EXIT in types
    assert len(types) == 2, f"双向应各计一次, 实际 {types}"


def test_reverse_cooldown_blocks_rapid_exit():
    """Enter 后 3s 内 Exit → Exit 被反向冷却拦截 → 仅 1 事件."""
    c = _make_counter()
    # dt=0.4: enter 在 t=2.0, exit 在 t=4.4 (间隔 2.4s < 3.0s)
    points = list(_enter_path())
    points += [(640, 430), (640, 400), (640, 350),
               (640, 320), (640, 290), (640, 260)]
    events = run_path(c, 1, "car", points, dt=0.4)
    types = [e.event_type for e in events]
    assert types == [VEHICLE_ENTER], f"冷却内 Exit 应被拦截, 实际 {types}"


# ===================== count_only 单向计数 =====================

def test_count_only_enter_filters_exit():
    """count_only='enter': Exit 跨线被忽略, Enter 正常计."""
    c = _make_counter(count_only="enter")
    # Exit 轨迹 → 0 事件
    e_exit = run_path(c, 1, "car", _exit_path())
    assert e_exit == []
    # Enter 轨迹 (新轨迹) → 1 事件
    e_enter = run_path(c, 2, "car", _enter_path())
    assert len(e_enter) == 1 and e_enter[0].event_type == VEHICLE_ENTER


def test_count_only_exit_filters_enter():
    """count_only='exit': Enter 跨线被忽略, Exit 正常计."""
    c = _make_counter(count_only="exit")
    e_enter = run_path(c, 1, "car", _enter_path())
    assert e_enter == []
    e_exit = run_path(c, 2, "car", _exit_path())
    assert len(e_exit) == 1 and e_exit[0].event_type == VEHICLE_EXIT


# ===================== ROI =====================

def test_roi_filters_track_outside():
    """轨迹中心在 ROI 外 → 不计数 (即使跨线)."""
    # ROI = 左侧 40% 区域 (x 归一化 0~0.4); 轨迹 x=640 (0.5) 在 ROI 外
    roi = [[0.0, 0.0], [0.4, 0.0], [0.4, 1.0], [0.0, 1.0]]
    c = _make_counter(roi=roi)
    events = run_path(c, 1, "car", _enter_path(x=640))
    assert events == []


def test_roi_inside_counts():
    """轨迹中心在 ROI 内 → 正常计数."""
    roi = [[0.3, 0.0], [0.8, 0.0], [0.8, 1.0], [0.3, 1.0]]
    c = _make_counter(roi=roi)
    events = run_path(c, 1, "car", _enter_path(x=640))
    assert len(events) == 1 and events[0].event_type == VEHICLE_ENTER


# ===================== ID 切换检测 =====================

def test_id_switch_speed_spike_no_event():
    """轨迹速度突变 (ID 切换特征) 跨线 → 不计 (避免跨对象误计)."""
    c = _make_counter()
    # 缓慢上移 (avg_speed=6 > 5 阈值), 然后大幅跳到线下方 (速度 80, 触发 ID 切换)
    points = [(640, 340), (640, 334), (640, 328), (640, 322),  # 缓慢上移
              (640, 420)]  # 突跳到下方 (跨线)
    events = run_path(c, 1, "car", points)
    assert events == [], f"ID 切换不应产出事件, 实际 {len(events)}"


# ===================== 多轨迹同帧 =====================

def test_multiple_tracks_same_frame():
    """同帧多条轨迹独立计数."""
    c = _make_counter()
    # 两条车一上一下, 同时 Enter
    path_a = _enter_path(start_y=300, x=400)
    path_b = _enter_path(start_y=280, x=800)
    events = []
    for i in range(max(len(path_a), len(path_b))):
        tracks = []
        if i < len(path_a):
            tracks.append(_track(1, "car", path_a[: i + 1]))
        if i < len(path_b):
            tracks.append(_track(2, "car", path_b[: i + 1]))
        result = TrackResult(frame_id=i + 1, tracks=tracks)
        events.extend(c.process_tracks(result, camera_id="TEST", current_time=float(i)))
    assert len(events) == 2
    assert all(e.event_type == VEHICLE_ENTER for e in events)


# ===================== 自运行入口 =====================

def _run_all():
    tests = [
        test_basic_vehicle_enter,
        test_basic_vehicle_exit,
        test_person_enter_exit,
        test_truck_and_bus_are_vehicle,
        test_no_crossing_same_side,
        test_jitter_near_line_no_false_event,
        test_hold_frames_required,
        test_dedup_same_direction,
        test_bidirectional_enter_then_exit_after_cooldown,
        test_reverse_cooldown_blocks_rapid_exit,
        test_count_only_enter_filters_exit,
        test_count_only_exit_filters_enter,
        test_roi_filters_track_outside,
        test_roi_inside_counts,
        test_id_switch_speed_spike_no_event,
        test_multiple_tracks_same_frame,
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
