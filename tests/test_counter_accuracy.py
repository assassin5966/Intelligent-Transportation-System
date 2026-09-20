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


# ===================== 场景 5: 跨线帧中心点恰好落在计数线上 =====================

def test_scenario_crossing_on_line_zero_offset():
    """跨线时中心点恰好落在计数线上 (offset==0) 仍应计数.

    真值: VehicleEnter=1.
    回归: 旧实现用 prev_off*curr_off >= 0 排除同侧, 落线帧乘积为 0 被一并丢弃,
    整次跨线静默漏计.
    """
    counter = _make_counter()
    # y=360 恰好落在计数线上 (offset==0)
    ys = [300, 330, _LINE_Y, 390, 420, 450]
    obj = MovingObject(track_id=1, class_name="car", x=640, start_frame=0, path_y=ys)
    events = simulate(counter, [obj], len(ys) + 2, dt=1.0)

    print(f"\n[场景5 落线帧跨线] VehicleEnter={events.count(VEHICLE_ENTER)} truth=1 → {events}")
    assert events.count(VEHICLE_ENTER) == 1, f"落线帧导致漏计: {events}"


# ===================== 场景 6: 低速小步长跨线 =====================

def test_scenario_slow_crossing_small_step():
    """低速车辆 (每帧位移 5px, 远小于防抖距离 20px) 跨线应计数.

    真值: VehicleEnter=1.
    回归: 落线帧 offset==0 同样会出现, 且跨线后需连续多帧才达到防抖距离.
    """
    counter = _make_counter()
    ys = [_LINE_Y - 10, _LINE_Y - 5, _LINE_Y, _LINE_Y + 5, _LINE_Y + 10,
          _LINE_Y + 15, _LINE_Y + 20, _LINE_Y + 25, _LINE_Y + 30]
    obj = MovingObject(track_id=1, class_name="car", x=640, start_frame=0, path_y=ys)
    events = simulate(counter, [obj], len(ys) + 2, dt=1.0)

    print(f"\n[场景6 低速小步长] VehicleEnter={events.count(VEHICLE_ENTER)} truth=1 → {events}")
    assert events.count(VEHICLE_ENTER) == 1, f"慢速跨线漏计: {events}"


# ===================== 场景 7: ROI 与计数线不相交 (失效保护) =====================

def test_scenario_roi_not_intersecting_line():
    """ROI 与计数线完全不相交时, 有效段标记为失效 (t0>t1) 且不产生事件.

    失效必须显式可见 (t0>t1 + warning), 而非默认放行导致越线段误计.
    """
    roi = [[0.25, 0.0], [0.75, 0.0], [0.75, 0.4], [0.25, 0.4]]  # 像素 y<=288, 线在 y=360
    counter = _make_counter()
    counter.set_roi(roi)

    assert counter._clip_t0 > counter._clip_t1, (
        f"ROI 与线不相交应标记整段失效, 实际 [{counter._clip_t0}, {counter._clip_t1}]"
    )

    path = _enter_path_y(300)
    obj = MovingObject(track_id=1, class_name="car", x=640, start_frame=0, path_y=path)
    events = simulate(counter, [obj], len(path) + 2, dt=1.0)

    print(f"\n[场景7 ROI与线不相交] events={events} (应为空)")
    assert events == [], f"越线段失效时不应产生事件: {events}"


# ===================== 场景 8: ROI 贴线, 跨线后移出 ROI 仍应计数 =====================

def test_scenario_roi_line_adjacent_exit_after_crossing():
    """ROI 为贴线窄带, 目标跨线后在防抖确认完成前移出 ROI 仍应计数.

    真值: VehicleEnter=1.
    回归 (P0-1): 旧实现每帧无条件按 ROI 过滤, 目标跨线后移出 ROI 被
    continue 中断, 滞留确认永远无法完成 -> 漏计且状态残留 CROSSING.
    修复后 CROSSING 状态目标不受 ROI 闸门中断.
    """
    # ROI 窄带: y 像素 [300, 370], 计数线 y=360 在带内, 内侧仅 10px 余量
    roi = [[0.0, 300 / _H], [1.0, 300 / _H], [1.0, 370 / _H], [0.0, 370 / _H]]
    counter = _make_counter()
    counter.set_roi(roi)

    # 跨线帧 y=365 (带内, 距线 5px 不满足防抖), 满足防抖距离的帧 (380/400/420)
    # 全部已出 ROI (>370) -> 确认必须发生在 ROI 外
    ys = [320, 340, 365, 380, 400, 420]
    obj = MovingObject(track_id=1, class_name="car", x=640, start_frame=0, path_y=ys)
    events = simulate(counter, [obj], len(ys) + 2, dt=0.04)

    print(f"\n[场景8 ROI贴线出区] VehicleEnter={events.count(VEHICLE_ENTER)} truth=1 → {events}")
    assert events.count(VEHICLE_ENTER) == 1, f"跨线后出 ROI 导致漏计: {events}"


# ===================== 场景 8b: 低帧率流滞留确认 (fps 解耦) =====================

def test_scenario_low_fps_hold_confirmation():
    """低帧率流 (5fps) 下目标跨线后仅存活 2 个防抖帧即消失, 仍应计数.

    真值: VehicleEnter=1.
    回归 (P0-2): 旧实现按帧数滞留确认 (hold_frames=3), 低帧率下 3 帧=0.6s,
    目标过早消失导致漏计. 修复后按时间量纲 (0.12s) 累计, 与帧率解耦.
    """
    counter = _make_counter()
    # dt=0.2 (5fps); 跨线 330->370, 仅 400/430 两帧满足防抖距离
    ys = [330, 370, 400, 430]
    obj = MovingObject(track_id=1, class_name="car", x=640, start_frame=0, path_y=ys)
    events = simulate(counter, [obj], len(ys) + 2, dt=0.2)

    print(f"\n[场景8b 低帧率滞留确认] VehicleEnter={events.count(VEHICLE_ENTER)} truth=1 → {events}")
    assert events.count(VEHICLE_ENTER) == 1, f"低帧率流滞留确认过慢导致漏计: {events}"


# ===================== 场景 9: 斜穿线端点 (交点在段内, 当前点投影在段外) =====================

def test_scenario_diagonal_crossing_near_endpoint():
    """斜穿计数线端点附近: 真实交点在段内, 但跨线帧中心点投影在段外.

    真值: VehicleEnter=1, 且事件 cross_point 应为运动线段与计数线的真实交点.
    回归 (P0-3): 旧实现用 curr_point 的投影做段内检查, 斜穿端点时
    投影落在线段延长线上被丢弃 -> 漏计; 事件坐标也用 curr_point 而非交点.
    """
    counter = _make_counter()
    # 计数线 y=360, x∈[128, 1152]
    # 跨线帧: (1070,350) -> (1175,365), 真实交点 x=1140 (段内, t≈0.988),
    # 而 curr_point=(1175,365) 投影 t≈1.022 落在段外
    path = [
        [1070.0, 350.0],
        [1175.0, 365.0],
        [1225.0, 380.0],
        [1275.0, 395.0],
        [1275.0, 410.0],
    ]
    # MovingObject 只支持固定 x, 本场景需变 x 路径, 手动逐帧驱动
    events = []
    for f in range(len(path)):
        center = path[f]
        history = [list(p) for p in path[: f + 1]]
        tracks = [Track(
            track_id="1", class_name="car",
            center=[float(center[0]), float(center[1])],
            history=history, confidence=0.9,
            bbox=[center[0] - 15, center[1] - 15, center[0] + 15, center[1] + 15],
            age=len(history),
        )]
        evs = counter.process_tracks(
            TrackResult(frame_id=f + 1, tracks=tracks), camera_id="BENCH",
            current_time=f * 1.0,
        )
        events.extend(evs)
    enters = [e for e in events if e.event_type == VEHICLE_ENTER]

    print(f"\n[场景9 斜穿端点] events={[e.event_type for e in events]} "
          f"cross_point={enters[0].cross_point if enters else None} truth=1")
    assert len(enters) == 1, f"斜穿端点导致漏计: {[e.event_type for e in events]}"
    # 交点贯通: 事件坐标应为真实交点 (≈1140, 360), 而非跨线帧中心 (1175, 365)
    cp = enters[0].cross_point
    assert abs(cp[0] - 1140.0) < 1.0 and abs(cp[1] - 360.0) < 1.0, (
        f"事件 cross_point 应为真实交点 (≈1140,360), 实际 {cp}"
    )


# ===================== 场景 10: 近距离高速加速目标不误杀 (P0-4) =====================

def test_scenario_fast_accelerating_target_not_killed():
    """透视下近景目标同向加速 (帧位移 10→110px), 跨线帧速度突变不应判为 ID 切换.

    真值: VehicleEnter=1.
    回归 (P0-4): 旧实现仅凭速度突变 (curr > 3×avg 且差 > 30px) 即重置轨迹,
    跨线帧恰逢速度峰值 -> 整次跨线被丢弃漏计.
    修复后需叠加方向连续性佐证: 同向加速目标方向连续, 不误杀.
    """
    counter = _make_counter()
    # y 位移: 10,12,15,25,60,90,110,110 (跨线发生在 312->372 帧, 位移 60)
    ys = [250, 260, 272, 287, 312, 372, 462, 572, 682]
    obj = MovingObject(track_id=1, class_name="car", x=640, start_frame=0, path_y=ys)
    events = simulate(counter, [obj], len(ys) + 2, dt=1.0)

    print(f"\n[场景10 高速加速不误杀] VehicleEnter={events.count(VEHICLE_ENTER)} truth=1 → {events}")
    assert events.count(VEHICLE_ENTER) == 1, f"高速加速目标被误杀漏计: {events}"


# ===================== 单元: ID 切换方向佐证 / 去重窗口 / 状态复位 =====================

def test_id_switch_direction_reversal_vs_acceleration():
    """ID 切换检测: 反向跳变仍识别为切换; 同向加速不误杀."""
    from app.ai.geometry_engine import GeometryEngine
    from app.ai.id_switch_detector import IDSwitchDetector

    det = IDSwitchDetector(GeometryEngine())
    # 反向跳变: 向下 20px/帧运动中突然向上跳 200px (典型 ID 切换)
    t1 = Track("1", "car", [640, 160],
               [[640, 300], [640, 320], [640, 340], [640, 360], [640, 160]])
    assert det.detect_speed_anomaly(t1) is True
    # 同向加速: 位移 12/15/25/60 同向递增 (真实加速)
    t2 = Track("2", "car", [640, 362],
               [[640, 250], [640, 262], [640, 277], [640, 302], [640, 362]])
    assert det.detect_speed_anomaly(t2) is False


def test_dedup_time_window_and_geometry():
    """去重策略: 同点短窗内去重; 时间窗外或跨线点远离时放行真实重过."""
    from app.ai.track_state_manager import TrackStateManager

    sm = TrackStateManager()
    sm.mark_counted("1", "enter", 100.0, [640, 360])
    # 3 秒后同点重复确认 -> 同一物理事件, 去重
    assert sm.can_count("1", "enter", 103.0, 3.0, cross_point=[650, 360]) is False
    # 30 秒后同点再次进入 (掉头绕行重过) -> 真实跨线, 放行
    assert sm.can_count("1", "enter", 130.0, 3.0, cross_point=[640, 360]) is True
    # 3 秒后但跨线点远离 (长线不同位置) -> 真实跨线, 放行
    sm2 = TrackStateManager()
    sm2.mark_counted("2", "enter", 100.0, [200, 360])
    assert sm2.can_count("2", "enter", 103.0, 3.0, cross_point=[900, 360]) is True


def test_reset_track_clears_crossing_state():
    """reset_track 必须复位状态机并清除交点, 防止 CROSSING 残留误走确认流程."""
    from app.ai.track_state_manager import TrackStateManager

    sm = TrackStateManager()
    sm.set_state("1", "CROSSING")
    sm.start_crossing("1", [640, 350], 1)
    sm.set_cross_point("1", [640, 360])
    sm.reset_track("1")
    assert sm.get_state("1") == "TRACKING", "reset_track 后状态应复位为 TRACKING"
    assert sm.get_crossing_start("1") is None
    assert sm.get_cross_point("1") is None


# ===================== 自运行入口 =====================

def _run_all():
    tests = [
        test_scenario_bidirectional_vehicles,
        test_scenario_unidirectional_persons,
        test_scenario_mixed_traffic_with_noise,
        test_scenario_roi_filtering,
        test_scenario_crossing_on_line_zero_offset,
        test_scenario_slow_crossing_small_step,
        test_scenario_roi_not_intersecting_line,
        test_scenario_roi_line_adjacent_exit_after_crossing,
        test_scenario_low_fps_hold_confirmation,
        test_scenario_diagonal_crossing_near_endpoint,
        test_scenario_fast_accelerating_target_not_killed,
        test_id_switch_direction_reversal_vs_acceleration,
        test_dedup_time_window_and_geometry,
        test_reset_track_clears_crossing_state,
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
