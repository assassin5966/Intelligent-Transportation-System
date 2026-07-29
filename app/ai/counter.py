"""越线 (Line Crossing) 计数.

单计数线 + 内侧锚点方案:
  - 通过内侧锚点 (anchor) 归一化法向, 使 Enter/Exit 成为绝对语义:
    外侧 -> 内侧 = Enter, 内侧 -> 外侧 = Exit.
    与线的绘制方向无关, 适用于水平/垂直任意方向计数线.
  - 方向由跨线序列起止位置的 offset(沿内侧法向投影) 判定, 不受帧间抖动影响.
  - 夹角过滤: 根据线段水平/垂直方向, 要求跨线方向位移 >= min_motion (兼容斜向车流).
  - 投影范围检查: 跨线点需落在线段内 (t∈[0,1]).
  - 防抖: 事件落定需轨迹远离计数线 (min_distance_threshold) 并滞留 hold_frames 帧.
  - 单向流动: 每条轨迹只计一次 (counted_tracks 去重).
"""
import math
from datetime import datetime
from typing import List, Tuple, Optional, Dict

from ..common.logger import logger
from ..schemas.events import (
    PERSON_ENTER,
    PERSON_EXIT,
    VEHICLE_ENTER,
    VEHICLE_EXIT,
)

Point = tuple[float, float]


class CrossingEvent:
    def __init__(self, event_type, track_id, class_name, timestamp, camera_id, cross_point, cross_line, direction, confidence):
        self.event_type = event_type
        self.track_id = track_id
        self.class_name = class_name
        self.timestamp = timestamp
        self.camera_id = camera_id
        self.cross_point = cross_point
        self.cross_line = cross_line
        self.direction = direction
        self.confidence = confidence


class LineCrossingCounter:
    """单计数线越线计数器 (内侧锚点方案).

    核心数学:
      line = P1 -> P2,  锚点 anchor 明确位于内侧.
      内侧法向 n_inner: 始终指向 anchor 所在侧, 与线绘制方向无关.
      offset(Q) = n_inner · (Q - P1):  >0 内侧, <0 外侧.
      跨线: offset 变号.
      方向: 起始 offset 与确认 offset 异号 -> 外->内=Enter, 内->外=Exit.
    """

    def __init__(
        self,
        line: Optional[Tuple[Point, Point]] = None,
        anchor: Optional[Point] = None,
    ):
        if line is not None:
            self.line_points: List[List[float]] = [list(line[0]), list(line[1])]
        else:
            self.line_points = [[0.5, 0.1], [0.5, 0.9]]  # 默认垂直线
        # 内侧锚点 (归一化), 默认 (0.9, 0.5) -> 右侧为内侧
        self.anchor_points: List[float] = list(anchor) if anchor is not None else [0.9, 0.5]

        self.line_name = "line_a"

        self.anti_jitter = True
        self.min_distance_ratio = 0.02  # 距线最小距离占帧短边比例
        self.min_distance_threshold = 50  # set_frame_size 重算
        self.endpoint_sensitivity = 0.05
        self.hold_frames = 2  # 滞留确认帧数 (跨线后需在新侧连续保持)
        self.min_motion = 2  # 最小位移(像素), 小于此值视为抖动
        self.count_only: Optional[str] = None  # None=双向, "enter"=只计Enter, "exit"=只计Exit

        # ID 切换检测参数 (可配置, 适配不同帧率/分辨率)
        self.id_switch_speed_ratio = 3.0  # 速度超过历史平均 N 倍视为 ID 切换
        self.id_switch_min_pixel = 30.0  # 速度差距至少 N 像素
        self.id_switch_min_avg_speed = 5.0  # 历史平均速度低于此值不判 (静止误判)
        self.id_switch_history_window = 5  # 方向一致性验证的历史窗口

        # 已计数轨迹的去重保留时长 (秒); 超时后淘汰, 避免内存泄漏与流重连 ID 重用漏计
        self.counted_tracks_ttl = 3600

        # 轨迹状态
        self.track_crossing_history: Dict[str, List[Tuple[float, str, str]]] = {}
        self.track_states: Dict[str, str] = {}
        self.track_crossing_start_pos: Dict[str, List[float]] = {}  # 跨线前位置
        self.track_confirm_side: Dict[str, int] = {}  # 滞留确认侧 (+1/-1)
        self.track_hold_count: Dict[str, int] = {}  # 滞留已确认帧数
        self.counted_tracks: Dict[str, float] = {}  # track_id -> 计数时间戳 (TTL 淘汰)

        # ROI 感兴趣区域多边形 (归一化坐标 [x1,y1,x2,y2,...], >=6 个值即 >=3 个顶点).
        # 仅 ROI 内 (中心点在多边形内) 的轨迹参与越线计数;
        # None 表示不启用 ROI, 全画面计数 (向后兼容).
        self.roi_polygon: Optional[List[Point]] = None

        self.frame_width = 1920
        self.frame_height = 1080

        # 预计算几何量 (像素坐标)
        self._p1: List[float] = [0.0, 0.0]
        self._p2: List[float] = [0.0, 0.0]
        self._anchor_px: List[float] = [0.0, 0.0]
        self._line_vec: List[float] = [0.0, 0.0]
        self._line_len_sq: float = 1.0
        self._n_inner: List[float] = [0.0, 0.0]
        self._n_unit: List[float] = [0.0, 0.0]  # 归一化法向 (朝向锚点)
        self._roi_px: Optional[List[List[float]]] = None  # ROI 像素多边形顶点
        # 计数线裁剪到 ROI 内的有效区间 [t0, t1] (线段参数 t∈[0,1]); ROI 关闭时 [0,1]
        self._clip_t0: float = 0.0
        self._clip_t1: float = 1.0
        # 裁剪后红线端点像素坐标 (供可视化区分有效段); ROI 关闭时等于 _p1/_p2
        self._clip_p0: List[float] = [0.0, 0.0]
        self._clip_p1: List[float] = [0.0, 0.0]
        self._precompute()

    def set_frame_size(self, width: int, height: int):
        self.frame_width = width
        self.frame_height = height
        self.min_distance_threshold = max(
            20, int(self.min_distance_ratio * min(width, height))
        )
        self._precompute()

    def set_line(self, line: List[Point], anchor: Optional[Point] = None):
        """设置计数线 (归一化 [[x1,y1],[x2,y2]]) 与可选内侧锚点."""
        self.line_points = [list(line[0]), list(line[1])]
        if anchor is not None:
            self.anchor_points = list(anchor)
        self._precompute()

    def set_anchor(self, anchor: Point):
        """设置内侧锚点 (归一化 [x,y])."""
        self.anchor_points = list(anchor)
        self._precompute()

    def set_lines(self, outer_line: List[Point], inner_line: Optional[List[Point]] = None):
        """向后兼容: 仅使用 outer_line, 忽略 inner_line."""
        self.set_line(outer_line)

    def _precompute(self):
        """预计算像素坐标几何量与内侧法向 (适用于任意角度计数线)."""
        self._p1 = self._normalize_to_pixel(self.line_points[0])
        self._p2 = self._normalize_to_pixel(self.line_points[1])
        self._anchor_px = self._normalize_to_pixel(self.anchor_points)
        dx = self._p2[0] - self._p1[0]
        dy = self._p2[1] - self._p1[1]
        self._line_vec = [dx, dy]
        self._line_len_sq = dx * dx + dy * dy
        line_len = math.sqrt(self._line_len_sq)
        # 法向 (左旋90°): n=(-dy, dx), 长度=线段长度
        nx, ny = -dy, dx
        # 用锚点归一化: 使法向指向锚点所在侧 (内侧)
        anchor_off = nx * (self._anchor_px[0] - self._p1[0]) + ny * (self._anchor_px[1] - self._p1[1])
        if anchor_off < 0:
            nx, ny = -nx, -ny
        self._n_inner = [nx, ny]
        # 归一化法向 (用于夹角过滤等需要真实距离的计算)
        if line_len > 0:
            self._n_unit = [nx / line_len, ny / line_len]
        else:
            self._n_unit = [0.0, 0.0]
        # 几何校验: 线段退化 / 锚点落在线上 -> 计数语义失效, 仅告警不中断
        if self._line_len_sq < 1e-6:
            logger.warning("计数线退化为点 (P1==P2), 越线计数将不触发, 请检查 line_coords")
        elif abs(anchor_off) < 1e-6:
            logger.warning("内侧锚点落在计数线上, 法向方向不确定, 请调整 anchor_coords")
        # ROI 像素多边形随帧尺寸/线变化重算 (归一化 -> 像素)
        self._recompute_roi_px()
        # 计数线裁剪到 ROI 内的有效区间 (ROI 关闭时整段有效)
        self._recompute_clip()

    def _recompute_roi_px(self):
        """将归一化 ROI 多边形转像素坐标 (None 时禁用)."""
        if self.roi_polygon is None or len(self.roi_polygon) < 3:
            self._roi_px = None
            return
        self._roi_px = [self._normalize_to_pixel(list(p)) for p in self.roi_polygon]

    def _recompute_clip(self):
        """将计数线 P1->P2 裁剪到 ROI 多边形内, 得到有效参数区间 [t0,t1] 与端点像素.

        线段参数 t: P(t) = P1 + t*(P2-P1), t∈[0,1] 为原线段.
        算法: 收集线段与 ROI 所有边的交点 t + 端点 t=0/1, 排序后扫描相邻 t 的中点,
        中点在 ROI 内 -> 该子段有效; 合并所有有效子段取最长者作为 [_clip_t0,_clip_t1].
        ROI 关闭时整段有效 [0,1]; 线完全在 ROI 外时 t0>t1 (整段失效).
        """
        # 默认整段有效; 端点像素用于可视化
        self._clip_t0 = 0.0
        self._clip_t1 = 1.0
        self._clip_p0 = list(self._p1)
        self._clip_p1 = list(self._p2)
        if self._roi_px is None or self._line_len_sq < 1e-6:
            return
        p1x, p1y = self._p1
        dx, dy = self._line_vec
        # 候选参数点: 端点 + 线段与 ROI 每条边的交点
        ts = [0.0, 1.0]
        n = len(self._roi_px)
        for i in range(n):
            ax, ay = self._roi_px[i]
            bx, by = self._roi_px[(i + 1) % n]
            ex, ey = bx - ax, by - ay
            # 求解 P1 + t*(dx,dy) = (ax,ay) + s*(ex,ey), t∈[0,1], s∈[0,1]
            denom = dx * (-ey) + dy * ex
            if abs(denom) < 1e-9:
                continue  # 线段与边平行/重合, 跳过
            t = ((ax - p1x) * (-ey) + (ay - p1y) * ex) / denom
            s = (dx * (ay - p1y) - dy * (ax - p1x)) / denom
            if 0.0 <= t <= 1.0 and 0.0 <= s <= 1.0:
                ts.append(t)
        ts = sorted(set(ts))
        # 扫描相邻 t 区间, 中点在 ROI 内则为有效子段; 取最长有效段
        best_t0, best_t1, best_len = 1.0, 0.0, -1.0
        for i in range(len(ts) - 1):
            t0, t1 = ts[i], ts[i + 1]
            if t1 - t0 < 1e-9:
                continue
            mid_t = (t0 + t1) / 2
            mx = p1x + mid_t * dx
            my = p1y + mid_t * dy
            if self._point_in_roi([mx, my]):
                if (t1 - t0) > best_len:
                    best_t0, best_t1, best_len = t0, t1, t1 - t0
        if best_len < 0:
            # 线段完全在 ROI 外, 标记整段失效 (t0>t1)
            self._clip_t0 = 1.0
            self._clip_t1 = 0.0
            return
        self._clip_t0 = best_t0
        self._clip_t1 = best_t1
        self._clip_p0 = [p1x + best_t0 * dx, p1y + best_t0 * dy]
        self._clip_p1 = [p1x + best_t1 * dx, p1y + best_t1 * dy]

    def set_roi(self, polygon: Optional[List[Point]]):
        """设置 ROI 感兴趣区域多边形 (归一化 [x,y] 顶点列表, >=3 个顶点).

        仅 ROI 内轨迹参与越线计数; 传 None 关闭 ROI (全画面计数).
        """
        if polygon is None or len(polygon) < 3:
            self.roi_polygon = None
            self._roi_px = None
            self._recompute_clip()
            logger.info("ROI 已关闭, 全画面计数")
            return
        self.roi_polygon = [list(p) for p in polygon]
        self._recompute_roi_px()
        self._recompute_clip()
        logger.info(f"已设置 ROI 多边形 ({len(self.roi_polygon)} 顶点)")

    def _point_in_roi(self, point) -> bool:
        """点是否在 ROI 多边形内 (射线法); ROI 未启用时恒返回 True."""
        if self._roi_px is None:
            return True
        n = len(self._roi_px)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = self._roi_px[i]
            xj, yj = self._roi_px[j]
            # 射线法: point 与多边形边的交点奇偶性判断内外
            if (yi > point[1]) != (yj > point[1]):
                x_int = (xj - xi) * (point[1] - yi) / (yj - yi) + xi
                if point[0] < x_int:
                    inside = not inside
            j = i
        return inside

    def _normalize_to_pixel(self, point: List[float]) -> List[float]:
        return [point[0] * self.frame_width, point[1] * self.frame_height]

    def _offset(self, point) -> float:
        """点沿内侧法向的投影: >0 内侧, <0 外侧."""
        return (
            self._n_inner[0] * (point[0] - self._p1[0])
            + self._n_inner[1] * (point[1] - self._p1[1])
        )

    def _side(self, point) -> int:
        off = self._offset(point)
        if off > 0:
            return 1
        if off < 0:
            return -1
        return 0

    def _point_to_line_distance(self, point, line_start, line_end) -> float:
        A = point[0] - line_start[0]
        B = point[1] - line_start[1]
        C = line_end[0] - line_start[0]
        D = line_end[1] - line_start[1]
        dot = A * C + B * D
        len_sq = C * C + D * D
        if len_sq == 0:
            return math.sqrt((point[0] - line_start[0]) ** 2 + (point[1] - line_start[1]) ** 2)
        param = dot / len_sq
        if param < 0:
            xx, yy = line_start[0], line_start[1]
        elif param > 1:
            xx, yy = line_end[0], line_end[1]
        else:
            xx = line_start[0] + param * C
            yy = line_start[1] + param * D
        dx = point[0] - xx
        dy = point[1] - yy
        return math.sqrt(dx * dx + dy * dy)

    def _is_clear_of_line(self, current_center) -> bool:
        """轨迹已远离计数线 (防抖距离确认)."""
        dist = self._point_to_line_distance(current_center, self._p1, self._p2)
        return dist >= self.min_distance_threshold

    def _in_segment(self, point) -> bool:
        """跨线点投影落在计数线有效段内 (裁剪到 ROI 后的 t∈[_clip_t0,_clip_t1]).

        ROI 关闭时有效段为整条线 [0,1]; ROI 启用时仅 ROI 内部分有效,
        越过 ROI 外线段不触发计数 (避免失效区误计).
        """
        if self._line_len_sq == 0:
            return False
        t = (
            (point[0] - self._p1[0]) * self._line_vec[0]
            + (point[1] - self._p1[1]) * self._line_vec[1]
        ) / self._line_len_sq
        return self._clip_t0 <= t <= self._clip_t1

    def _angle_filter(self, prev_point, curr_point) -> bool:
        """夹角过滤: 运动向量在法向方向的投影 >= min_motion.

        适用于任意角度计数线: 通过朝向锚点的归一化法向量计算跨线分量.
        运动向量 v 在法向 n_unit 上的投影 = v·n_unit, 即跨线方向位移.
        返回 True 表示通过 (有效跨线运动).
        """
        vx = curr_point[0] - prev_point[0]
        vy = curr_point[1] - prev_point[1]
        cross_component = vx * self._n_unit[0] + vy * self._n_unit[1]
        return abs(cross_component) >= self.min_motion

    def _filter_endpoint_false_positive(self, track) -> bool:
        """端点附近误判过滤: 仅当轨迹连续两帧都停滞在线段端点附近时过滤 (表示在线端徘徊).

        单帧靠近端点 (仅经过) 不过滤, 避免端点附近正常越线被误删.
        """
        current_center = track.center
        sensitivity_pixel = self.endpoint_sensitivity * max(self.frame_width, self.frame_height)
        if not (track.history and len(track.history) > 1):
            return True
        prev_center = track.history[-2]
        for endpoint in self.line_points:
            endpoint_pixel = self._normalize_to_pixel(endpoint)
            cur_dist = math.hypot(
                current_center[0] - endpoint_pixel[0],
                current_center[1] - endpoint_pixel[1],
            )
            if cur_dist >= sensitivity_pixel:
                continue
            # 当前帧靠近端点: 仅当前一帧也靠近同一端点时才过滤
            prev_dist = math.hypot(
                prev_center[0] - endpoint_pixel[0],
                prev_center[1] - endpoint_pixel[1],
            )
            if prev_dist < sensitivity_pixel:
                return False
        return True

    def process_tracks(self, track_result, camera_id: str = "CAM001") -> List[CrossingEvent]:
        events: List[CrossingEvent] = []

        for track in track_result.tracks:
            track_id = track.track_id

            if track_id not in self.track_states:
                self.track_states[track_id] = "TRACKING"
            if track_id not in self.track_crossing_history:
                self.track_crossing_history[track_id] = []

            if len(track.history) < 2:
                continue

            prev_point = track.history[-2]
            curr_point = track.center

            # ROI 过滤: 中心点不在多边形内的轨迹跳过计数 (减算力 / 过滤画面边缘干扰)
            if not self._point_in_roi(curr_point):
                continue

            # 轨迹连续性验证: 速度突变检测 (过滤 ByteTrack ID 切换)
            # ID 切换时 prev_point 与 curr_point 来自不同车辆, 位移远超历史速度
            if len(track.history) >= 4:
                h = track.history
                hist_speeds = [
                    math.hypot(h[i][0] - h[i-1][0], h[i][1] - h[i-1][1])
                    for i in range(1, len(h) - 1)
                ]
                if hist_speeds:
                    avg_speed = sum(hist_speeds) / len(hist_speeds)
                    curr_speed = math.hypot(
                        curr_point[0] - prev_point[0],
                        curr_point[1] - prev_point[1],
                    )
                    # 速度突变 (超过历史平均 N 倍且差距足够) -> ID 切换, 清除跨线状态
                    if (
                        avg_speed > self.id_switch_min_avg_speed
                        and curr_speed > avg_speed * self.id_switch_speed_ratio
                        and (curr_speed - avg_speed) > self.id_switch_min_pixel
                    ):
                        self.track_states[track_id] = "TRACKING"
                        self.track_crossing_start_pos.pop(track_id, None)
                        self.track_confirm_side.pop(track_id, None)
                        self.track_hold_count[track_id] = 0
                        continue

            # 1. 跨线检测: offset 异号
            prev_off = self._offset(prev_point)
            curr_off = self._offset(curr_point)
            crossed = prev_off * curr_off < 0

            if crossed:
                # 夹角过滤 + 投影范围: 排除沿线滑动与延长线误判
                if not self._angle_filter(prev_point, curr_point):
                    continue
                if not self._in_segment(curr_point):
                    continue

                current_time = datetime.now().timestamp()
                # 方向标签 (基于 offset, 绝对语义)
                if prev_off < 0 and curr_off > 0:
                    direction = "outer_to_inner"
                elif prev_off > 0 and curr_off < 0:
                    direction = "inner_to_outer"
                else:
                    direction = "same_side"
                self.track_crossing_history[track_id].append((current_time, self.line_name, direction))

                # 仅首次跨线记录起始位置 (跨线前), 用于最终方向判定
                if self.track_states[track_id] != "CROSSING":
                    self.track_crossing_start_pos[track_id] = list(prev_point)
                    self.track_hold_count[track_id] = 0
                    self.track_confirm_side[track_id] = self._side(curr_point)
                self.track_states[track_id] = "CROSSING"

            # 2. 非 CROSSING 状态: 无待确认事件
            if self.track_states[track_id] != "CROSSING":
                continue

            # 3. 防抖: 远离计数线 + 端点过滤
            if self.anti_jitter:
                if not self._is_clear_of_line(curr_point):
                    continue
                if not self._filter_endpoint_false_positive(track):
                    continue

            # 4. 滞留确认: 连续 hold_frames 帧保持在跨线后侧
            curr_side_now = self._side(curr_point)
            if curr_side_now != self.track_confirm_side.get(track_id, 0):
                # 侧别反转 (抖动跨回), 重新等待
                self.track_hold_count[track_id] = 0
                self.track_confirm_side[track_id] = curr_side_now
                continue
            self.track_hold_count[track_id] = self.track_hold_count.get(track_id, 0) + 1
            if self.track_hold_count[track_id] < self.hold_frames:
                continue

            # 5. 方向由跨线序列起止 offset 判定 (绝对语义, 不受帧间抖动影响)
            start_pos = self.track_crossing_start_pos.get(track_id, prev_point)
            start_off = self._offset(start_pos)
            end_off = self._offset(curr_point)
            if start_off < 0 and end_off > 0:
                entry_exit = "enter"
                direction_str = "outer_to_inner"
            elif start_off > 0 and end_off < 0:
                entry_exit = "exit"
                direction_str = "inner_to_outer"
            else:
                # 起止同侧 (抖动跨回), 不产出事件
                self.track_states[track_id] = "TRACKING"
                continue

            # 方向一致性验证: 跨线方向应与最近运动方向一致 (过滤 ID 切换)
            # ID 切换时 start_pos 来自前一辆车, 与当前车辆运动方向矛盾
            # 用 offset 变化判断, 适用于任意角度计数线
            h = track.history
            win = self.id_switch_history_window
            if len(h) >= win:
                recent_start_off = self._offset(h[-win])
                recent_end_off = self._offset(h[-1])
                recent_cross_inner = recent_end_off - recent_start_off  # >0 向内, <0 向外
                if entry_exit == "exit" and recent_cross_inner > 0:
                    # 判定 Exit(内->外) 但最近向内移动 -> start_pos 可能有误
                    self.track_states[track_id] = "TRACKING"
                    continue
                if entry_exit == "enter" and recent_cross_inner < 0:
                    # 判定 Enter(外->内) 但最近向外移动 -> start_pos 可能有误
                    self.track_states[track_id] = "TRACKING"
                    continue

            # 6. 单向流动: 每条轨迹只计一次
            if track_id in self.counted_tracks:
                self.track_states[track_id] = "TRACKING"
                continue

            # 单向计数模式: 只产出指定方向事件, 反向跨线忽略 (不标记 counted, 允许后续正向再计)
            if self.count_only is not None and entry_exit != self.count_only:
                self.track_states[track_id] = "TRACKING"
                continue

            # 7. 生成事件 (person/bicycle/motorcycle 归为人流, car/truck/bus 归为车流)
            if track.class_name in ("person", "bicycle", "motorcycle"):
                event_type = PERSON_ENTER if entry_exit == "enter" else PERSON_EXIT
            else:
                event_type = VEHICLE_ENTER if entry_exit == "enter" else VEHICLE_EXIT

            event = CrossingEvent(
                event_type=event_type,
                track_id=track_id,
                class_name=track.class_name,
                timestamp=datetime.now().isoformat(),
                camera_id=camera_id,
                cross_point=curr_point,
                cross_line=self.line_name,
                direction=direction_str,
                confidence=track.confidence,
            )
            events.append(event)
            self.counted_tracks[track_id] = datetime.now().timestamp()
            self.track_states[track_id] = "TRACKING"

        self._cleanup_old_tracks()
        return events

    def _cleanup_old_tracks(self):
        """清理长时间无跨线的轨迹状态; 按 TTL 淘汰已计数轨迹 (防内存泄漏与流重连 ID 重用漏计)."""
        current_time = datetime.now().timestamp()
        to_remove = []
        for track_id, history in self.track_crossing_history.items():
            if not history:
                to_remove.append(track_id)
                continue
            last_time = history[-1][0]
            if current_time - last_time > 20:
                to_remove.append(track_id)

        for track_id in to_remove:
            self.track_crossing_history.pop(track_id, None)
            self.track_states.pop(track_id, None)
            self.track_crossing_start_pos.pop(track_id, None)
            self.track_confirm_side.pop(track_id, None)
            self.track_hold_count.pop(track_id, None)

        # 已计数轨迹按 TTL 淘汰: 7x24 流长期运行防止内存无限增长;
        # 流重连后跟踪器 ID 从头分配, 淘汰旧 ID 避免新轨迹被误判为已计数而漏计.
        expired = [
            tid for tid, ts in self.counted_tracks.items()
            if current_time - ts > self.counted_tracks_ttl
        ]
        for tid in expired:
            self.counted_tracks.pop(tid, None)

    def reset(self):
        self.track_crossing_history.clear()
        self.track_states.clear()
        self.track_crossing_start_pos.clear()
        self.track_confirm_side.clear()
        self.track_hold_count.clear()
        self.counted_tracks.clear()
