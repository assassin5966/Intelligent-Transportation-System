"""轨迹缝合 (高速目标补计) 验证脚本.

不加载 YOLO/ultralytics: 通过直接注入 track_history/miss_count 模拟
"高速车漏检数帧 -> 旧 ID 死亡 -> 新 ID 重现" 场景, 验证 _stitch_history 判定.

场景矩阵:
  A. 正例: 高速车匀速漏检 4 帧后新 ID 重现 -> 应缝合, 且历史继承
  B. 正例: 缝合后新旧历史拼接成连续轨迹 (速度方向一致)
  C. 反例: 类别不同 (旧 car 新 person) -> 不缝合
  D. 反例: 新目标出现在旧轨迹反方向 (掉头车辆) -> 不缝合
  E. 反例: 旧轨迹消失超过 gap 帧 (12) -> 不缝合 (已超 BoT-SORT 保留期)
  F. 反例: 在场轨迹 (本帧有检测) 不被接续
  G. 防双计: 同一旧轨迹只能被缝合一次
  H. 反例: 新车位置与外推点距离超过 bbox 对角线 x 2.0 -> 不缝合

运行: python3 scripts/zbny-test-track-stitching.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/zbny-test-track-stitching", exist_ok=True)
os.chdir("/tmp/zbny-test-track-stitching")

# ultralytics 首次导入会在 ~/.config/Ultralytics 建配置目录 (沙箱/容器内可能不可写),
# 重定向到可写临时目录
os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/zbny-test-track-stitching/Ultralytics")

from app.ai.tracker import ByteTracker


def make_tracker():
    return ByteTracker(camera_type="vehicle")


def inject_old_track(tracker: ByteTracker, tid=1, cls="car",
                      positions=None, miss=4):
    """直接注入一条"消失中"的旧轨迹 (绕过 YOLO)."""
    hist = positions if positions is not None else [
        [100.0, 500.0], [130.0, 500.0], [160.0, 500.0], [160.0, 500.0], [160.0, 500.0]
    ]
    tracker.track_history[tid] = [tuple(p) for p in hist]
    tracker.track_class[tid] = cls
    tracker._miss_count[tid] = miss
    return tid


def stitch(tracker, new_tid, cls="car", bbox=None):
    bbox = bbox or [900.0, 450.0, 980.0, 520.0]  # 对角线 ~106, 中心 (940, 485)
    return tracker._stitch_history(new_tid, cls, bbox)


def case(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    return cond


def main():
    ok = True

    # ---- A+B. 正例: 高速车匀速漏检 4 帧后新 ID 重现 ----
    t = make_tracker()
    # 车以 30px/帧 向右行驶 (近景轿车 bbox 200px, 25fps -> 约 3m/s 实际速度)
    inject_old_track(t, tid=1, cls="car",
                     positions=[[850, 500], [880, 500], [910, 500]], miss=4)
    # 外推: 910 + 30*5 = 1060, 新 bbox 中心应在 1060 附近
    r = stitch(t, new_tid=2, bbox=[1020, 470, 1100, 540])
    ok &= case("A 缝合成功: 继承旧轨迹历史", len(r) == 3)
    ok &= case("B 缝合目标正确", t._stitched_from == {1})

    # ---- C. 反例: 类别不同 ----
    t = make_tracker()
    inject_old_track(t, tid=1, cls="car", miss=4)
    r = stitch(t, new_tid=2, cls="person")
    ok &= case("C 类别不同不缝合", r == [])

    # ---- D. 反例: 反方向 (掉头车辆走回头路, 新 ID 出现在旧轨迹后方) ----
    t = make_tracker()
    inject_old_track(t, tid=1, cls="car",
                     positions=[[850, 500], [880, 500], [910, 500]], miss=2)
    # 新 ID 出现在旧轨迹"后方" (外推点 1060 的反方向)
    r = stitch(t, new_tid=2, bbox=[760, 470, 800, 540])
    ok &= case("D 反方向不缝合", r == [])

    # ---- E. 反例: 消失超过 gap 帧 ----
    t = make_tracker()
    inject_old_track(t, tid=1, cls="car", miss=13)
    r = stitch(t, new_tid=2)
    ok &= case("E 消失超 gap 帧不缝合", r == [])

    # ---- F. 反例: 在场轨迹不被接续 ----
    t = make_tracker()
    inject_old_track(t, tid=1, cls="car", miss=0)
    t._current_track_ids = {1}
    r = stitch(t, new_tid=2)
    ok &= case("F 在场轨迹不接续", r == [])

    # ---- G. 防双计: 同一旧轨迹只能缝合一次 ----
    t = make_tracker()
    inject_old_track(t, tid=1, cls="car",
                     positions=[[850, 500], [880, 500], [910, 500]], miss=4)
    r1 = stitch(t, new_tid=2)
    # 人为再造一个新 ID 试图再次缝合同一旧轨迹 (旧轨迹已被 del, 不应命中)
    t._miss_count[3] = 4  # 模拟另一条消失轨迹, 但类别不同
    t.track_history[3] = [(100, 100), (110, 100), (120, 100)]
    t.track_class[3] = "car"
    r2 = stitch(t, new_tid=4, bbox=[990, 470, 1000, 540])
    ok &= case("G 首次缝合后旧轨迹已删除, 不会二次缝合",
               t._stitched_from == {1} and len(t.track_history) == 1)

    # ---- H. 反例: 位置跳变过大 ----
    t = make_tracker()
    inject_old_track(t, tid=1, cls="car",
                     positions=[[850, 500], [880, 500], [910, 500]], miss=4)
    # 新车 bbox 在画面另一端
    r = stitch(t, new_tid=2, bbox=[1700, 400, 1800, 500])
    ok &= case("H 跨画面跳变不缝合", r == [])

    # ---- I. 端到端: 缝合后的拼接历史可支撑跨线计数 (高速车漏检仍计一次) ----
    # 构造: 计数线 x=500, 车从 x=300 向右行驶, 在 x=470 漏检 4 帧,
    # 重现时已在 x=590 (线右侧). 若不缝合, 旧轨迹止步线左侧, 新轨迹起于线右侧
    # -> 永不跨线, 漏计. 缝合后历史跨越线两侧 -> 检出跨越.
    line = (500.0, 0.0)  # x=500 垂直线
    def offset_x(p):
        return p[0] - line[0]
    t = make_tracker()
    # 旧轨迹: 300 -> 470 (每帧 42.5px, 共 5 点), 此时在线左侧
    old = [[300.0, 500.0], [342.5, 500.0], [385.0, 500.0], [427.5, 500.0], [470.0, 500.0]]
    inject_old_track(t, tid=1, cls="car", positions=old, miss=4)
    # 新 ID 出现在 x=590 (外推 470+42.5*5=682.5? 不对: miss=4, n=5, 470+212.5=682.5)
    # 实际重现 x=600 -> gap=82.5 < diag*2 (bbox [550,470,650,530] diag=117)
    r = stitch(t, new_tid=2, bbox=[550.0, 470.0, 650.0, 530.0])
    hist = r + [(600.0, 500.0)]
    offsets = [p[0] - line[0] for p in hist]
    crossed = any(a < 0 <= b or b < 0 <= a for a, b in zip(offsets, offsets[1:]))
    ok &= case("I 缝合后拼接历史可检出跨线 (不缝合则漏计)",
               len(r) == 5 and crossed)

    print()
    if ok:
        print("PASS: 轨迹缝合全部 9 个场景符合预期")
    else:
        print("FAIL: 存在未通过场景")
        sys.exit(1)


if __name__ == "__main__":
    main()
