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

职责拆分:
  本类现为轻量级协调器, 将具体职责委托给以下组件:
  - GeometryEngine: 几何计算 (坐标转换/法向量/投影/距离)
  - ROIDetector: ROI 多边形管理/点在多边形内判断/线段裁剪
  - CrossingDetector: 跨线检测/夹角过滤/投影范围检查
  - DebounceValidator: 远离线检测/端点误判过滤/滞留确认/滞回防抖
  - IDSwitchDetector: 速度突变检测/方向一致性验证
  - EventGenerator: 事件类型判定/CrossingEvent 创建
  - TrackStateManager: 轨迹跨线状态/已计数去重/TTL清理
"""
import math
from datetime import datetime
from typing import List, Tuple, Optional, Dict

from ..common.logger import logger
from .geometry_engine import GeometryEngine
from .roi_detector import ROIDetector
from .crossing_detector import CrossingDetector
from .debounce_validator import DebounceValidator
from .id_switch_detector import IDSwitchDetector
from .event_generator import EventGenerator, CrossingEvent
from .track_state_manager import TrackStateManager

Point = tuple[float, float]


class LineCrossingCounter:
    """单计数线越线计数器 (内侧锚点方案).

    核心数学:
      line = P1 -> P2,  锚点 anchor 明确位于内侧.
      内侧法向 n_inner: 始终指向 anchor 所在侧, 与线绘制方向无关.
      offset(Q) = n_inner · (Q - P1):  >0 内侧, <0 外侧.
      跨线: offset 变号.
      方向: 起始 offset 与确认 offset 异号 -> 外->内=Enter, 内->外=Exit.

    职责拆分:
      本类为轻量级协调器, 将具体职责委托给各组件.
    """

    def __init__(
        self,
        line: Optional[Tuple[Point, Point]] = None,
        anchor: Optional[Point] = None,
    ):
        # 初始化组件
        self.geometry = GeometryEngine()
        self.roi_detector = ROIDetector(self.geometry)
        self.crossing_detector = CrossingDetector(self.geometry, self.roi_detector)
        self.debounce_validator = DebounceValidator(self.geometry)
        self.id_switch_detector = IDSwitchDetector(self.geometry)
        self.event_generator = EventGenerator()
        self.state_manager = TrackStateManager()

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
        self.hold_frames = 3  # 滞留确认帧数 (跨线后需在新侧连续保持)
        self.min_motion = 2  # 最小位移(像素), 小于此值视为抖动
        self.count_only: Optional[str] = None  # None=双向, "enter"=只计Enter, "exit"=只计Exit

        # 滞回防抖: 侧别反转需超过此距离 (像素), 带内抖动不触发反转
        # 解决单向车流因线附近抖动误产 Exit, 人流误产 Enter 的问题
        self.hysteresis_ratio = 0.04  # 占帧短边比例
        self.hysteresis_threshold = 30  # set_frame_size 重算
        # 反向跨线冷却 (秒): 同一轨迹反向事件需间隔此时长, 防止瞬时进出
        self.reverse_crossing_cooldown = 3.0

        # ID 切换检测参数 (可配置, 适配不同帧率/分辨率)
        self.id_switch_speed_ratio = 3.0  # 速度超过历史平均 N 倍视为 ID 切换
        self.id_switch_min_pixel = 30.0  # 速度差距至少 N 像素
        self.id_switch_min_avg_speed = 5.0  # 历史平均速度低于此值不判 (静止误判)
        self.id_switch_history_window = 5  # 方向一致性验证的历史窗口

        # 已计数去重保留时长 (秒); 超时后淘汰, 避免内存泄漏与流重连 ID 重用漏计
        self.counted_tracks_ttl = 300

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
        self._hysteresis_offset: float = 0.0  # 滞回 offset 阈值 (hysteresis_threshold * 线长)
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
        self.geometry.frame_width = width
        self.geometry.frame_height = height
        self.min_distance_threshold = max(
            20, int(self.min_distance_ratio * min(width, height))
        )
        self.hysteresis_threshold = max(
            20, int(self.hysteresis_ratio * min(width, height))
        )
        self.debounce_validator.min_distance_threshold = self.min_distance_threshold
        self.debounce_validator.hysteresis_threshold = self.hysteresis_threshold
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
        """预计算像素坐标几何量, 更新各组件参数."""
        self._p1 = self._normalize_to_pixel(self.line_points[0])
        self._p2 = self._normalize_to_pixel(self.line_points[1])
        self._anchor_px = self._normalize_to_pixel(self.anchor_points)
        dx = self._p2[0] - self._p1[0]
        dy = self._p2[1] - self._p1[1]
        self._line_vec = [dx, dy]
        self._line_len_sq = dx * dx + dy * dy
        line_len = math.sqrt(self._line_len_sq)

        # 通过 GeometryEngine 计算法向量
        self._n_inner, self._n_unit = self.geometry.compute_inner_normal(
            self._p1, self._p2, self._anchor_px
        )

        # 几何校验: 线段退化 / 锚点落在线上 -> 计数语义失效, 仅告警不中断
        anchor_off = (
            self._n_inner[0] * (self._anchor_px[0] - self._p1[0])
            + self._n_inner[1] * (self._anchor_px[1] - self._p1[1])
        )
        if self._line_len_sq < 1e-6:
            logger.warning("计数线退化为点 (P1==P2), 越线计数将不触发, 请检查 line_coords")
        elif abs(anchor_off) < 1e-6:
            logger.warning("内侧锚点落在计数线上, 法向方向不确定, 请调整 anchor_coords")

        # 滞回 offset 阈值 = 像素阈值 × 线长 (offset 量纲 = 距离 × 线长)
        self._hysteresis_offset = self.hysteresis_threshold * line_len

        # 更新 CrossingDetector 参数
        self.crossing_detector.set_line_params(
            self._p1, self._p2, self._n_inner, self._n_unit,
            self._clip_t0, self._clip_t1,
        )

        # 更新 DebounceValidator 参数
        self.debounce_validator.set_line_params(self._p1, self._p2, self._n_inner)

        # 更新 IDSwitchDetector 参数
        self.id_switch_detector.set_line_params(self._p1, self._n_inner)

        # ROI 像素多边形随帧尺寸/线变化重算
        self.roi_detector._recompute_roi_px()

        # 计数线裁剪到 ROI 内的有效区间
        self._recompute_clip()

    def _recompute_clip(self):
        """将计数线裁剪到 ROI 多边形内, 更新有效区间与裁剪后端点."""
        if self.roi_detector._roi_px is None or self._line_len_sq < 1e-6:
            self._clip_t0 = 0.0
            self._clip_t1 = 1.0
            self._clip_p0 = list(self._p1)
            self._clip_p1 = list(self._p2)
        else:
            self._clip_t0, self._clip_t1, self._clip_p0, self._clip_p1 = (
                self.roi_detector.clip_line_to_roi(self._p1, self._p2)
            )

        # 同步更新 CrossingDetector 的裁剪区间
        self.crossing_detector.clip_t0 = self._clip_t0
        self.crossing_detector.clip_t1 = self._clip_t1

    def set_roi(self, polygon: Optional[List[Point]]):
        """设置 ROI 感兴趣区域多边形 (归一化 [x,y] 顶点列表, >=3 个顶点).

        仅 ROI 内轨迹参与越线计数; 传 None 关闭 ROI (全画面计数).
        """
        self.roi_detector.set_roi(polygon)
        if polygon is None or len(polygon) < 3:
            self.roi_polygon = None
        else:
            self.roi_polygon = [list(p) for p in polygon]
        self._recompute_clip()

    def count_roi_vehicles(self, tracks) -> int:
        """统计 ROI 内的车辆个数 (瞬时在场车辆数, 供拥挤判断).

        - 车辆类别: car/truck/bus
        - ROI 未启用时统计全画面车辆 (向后兼容)
        - 只按当前帧判定, 不依赖跨线事件 (反映真实在场占用)
        """
        count = 0
        for track in tracks:
            if track.class_name not in ("car", "truck", "bus"):
                continue
            if self.roi_detector.point_in_roi(track.center):
                count += 1
        return count

    def count_roi_persons(self, tracks) -> int:
        """统计 ROI 内的人员个数 (瞬时在场人数, 供人流拥挤判断).

        语义与 count_roi_vehicles 一致, 只统计 person 类别; ROI 未启用时统计全画面人数.
        """
        count = 0
        for track in tracks:
            if track.class_name != "person":
                continue
            if self.roi_detector.point_in_roi(track.center):
                count += 1
        return count

    # ---- 内部方法 (薄封装, 保持向后兼容) ----

    def _point_in_roi(self, point) -> bool:
        """点是否在 ROI 多边形内 (射线法); ROI 未启用时恒返回 True."""
        return self.roi_detector.point_in_roi(point)

    def _normalize_to_pixel(self, point: List[float]) -> List[float]:
        return self.geometry.normalize_to_pixel(point)

    def _offset(self, point) -> float:
        """点沿内侧法向的投影: >0 内侧, <0 外侧."""
        return self.geometry.offset(point, self._p1, self._n_inner)

    def _side(self, point) -> int:
        return self.geometry.side(point, self._p1, self._n_inner)

    def _point_to_line_distance(self, point, line_start, line_end) -> float:
        return self.geometry.point_to_line_distance(point, line_start, line_end)

    def _is_clear_of_line(self, current_center) -> bool:
        """轨迹已远离计数线 (防抖距离确认)."""
        return self.debounce_validator.is_clear_of_line(current_center)

    def _in_segment(self, point) -> bool:
        return self.crossing_detector._in_segment(point)

    def _angle_filter(self, prev_point, curr_point) -> bool:
        return self.crossing_detector._angle_filter(prev_point, curr_point)

    def _filter_endpoint_false_positive(self, track) -> bool:
        """端点附近误判过滤: 仅当轨迹连续两帧都停滞在线段端点附近时过滤."""
        prev_center = track.history[-2] if track.history and len(track.history) > 1 else None
        return self.debounce_validator.filter_endpoint_false_positive(
            track.center, prev_center,
            self.line_points, self.frame_width, self.frame_height,
        )

    # ---- 主流程 ----

    def _apply_business_rules(self) -> None:
        """从 business_rules.yaml 热重载计数参数 (修改后无需重启 AI)."""
        from ..common.business_rules import get_rule

        self.min_distance_ratio = float(
            get_rule("counting", "min_distance_ratio", default=self.min_distance_ratio)
        )
        self.endpoint_sensitivity = float(
            get_rule("counting", "endpoint_sensitivity", default=self.endpoint_sensitivity)
        )
        self.hold_frames = int(
            get_rule("counting", "hold_frames", default=self.hold_frames)
        )
        self.min_motion = float(
            get_rule("counting", "min_motion", default=self.min_motion)
        )
        self.hysteresis_ratio = float(
            get_rule("counting", "hysteresis_ratio", default=self.hysteresis_ratio)
        )
        self.reverse_crossing_cooldown = float(
            get_rule("counting", "reverse_crossing_cooldown", default=self.reverse_crossing_cooldown)
        )
        self.id_switch_speed_ratio = float(
            get_rule("counting", "id_switch_speed_ratio", default=self.id_switch_speed_ratio)
        )
        self.id_switch_min_pixel = float(
            get_rule("counting", "id_switch_min_pixel", default=self.id_switch_min_pixel)
        )
        self.id_switch_min_avg_speed = float(
            get_rule("counting", "id_switch_min_avg_speed", default=self.id_switch_min_avg_speed)
        )
        self.id_switch_history_window = int(
            get_rule("counting", "id_switch_history_window", default=self.id_switch_history_window)
        )
        self.counted_tracks_ttl = int(
            get_rule("counting", "counted_tracks_ttl", default=self.counted_tracks_ttl)
        )
        # 阈值随新 ratio 重算 (与 set_frame_size 同逻辑)
        if self.frame_width and self.frame_height:
            self.min_distance_threshold = max(
                20, int(self.min_distance_ratio * min(self.frame_width, self.frame_height))
            )
            self.hysteresis_threshold = max(
                20, int(self.hysteresis_ratio * min(self.frame_width, self.frame_height))
            )
            self.debounce_validator.min_distance_threshold = self.min_distance_threshold
            self.debounce_validator.hysteresis_threshold = self.hysteresis_threshold
        self.debounce_validator.endpoint_sensitivity = self.endpoint_sensitivity
        # 同步 ID 切换检测器参数
        self.id_switch_detector.update_config(
            speed_ratio=self.id_switch_speed_ratio,
            min_pixel=self.id_switch_min_pixel,
            min_avg_speed=self.id_switch_min_avg_speed,
            history_window=self.id_switch_history_window,
        )
        self.crossing_detector.min_motion = self.min_motion

    def process_tracks(self, track_result, camera_id: str = "CAM001", current_time: Optional[float] = None) -> List[CrossingEvent]:
        """处理一批跟踪轨迹, 返回越线事件.

        current_time: 当前时间戳 (秒); 离线处理传视频时间, 实时流默认 datetime.now().
        """
        if current_time is None:
            current_time = datetime.now().timestamp()
        events: List[CrossingEvent] = []

        # 热重载业务规则 (每帧检查文件 mtime, 变化才生效)
        self._apply_business_rules()

        for track in track_result.tracks:
            track_id = track.track_id

            self.state_manager.ensure_track_initialized(track_id)

            if len(track.history) < 2:
                continue

            prev_point = track.history[-2]
            curr_point = track.center

            # 1. ROI 过滤: 中心点不在多边形内的轨迹跳过计数
            if not self.roi_detector.point_in_roi(curr_point):
                continue

            # 2. ID 切换检测: 速度突变过滤
            if self.id_switch_detector.detect_speed_anomaly(track):
                self.state_manager.reset_track(track_id)
                self.debounce_validator.reset_track(track_id)
                continue

            # 3. 跨线检测
            prev_off = self._offset(prev_point)
            curr_off = self._offset(curr_point)
            direction = self.crossing_detector.detect_crossing(prev_point, curr_point)

            if direction is not None:
                self.state_manager.add_crossing_history(
                    track_id, current_time, self.line_name, direction
                )
                # 仅首次跨线记录起始位置 (跨线前), 用于最终方向判定
                if self.state_manager.get_state(track_id) != "CROSSING":
                    self.state_manager.start_crossing(track_id, prev_point, self.geometry.side(curr_point, self._p1, self._n_inner))
                    self.debounce_validator.reset_track(track_id)
                    self.debounce_validator.track_confirm_side[track_id] = self.geometry.side(curr_point, self._p1, self._n_inner)
                self.state_manager.set_state(track_id, "CROSSING")

            # 4. 非 CROSSING 状态: 无待确认事件
            if self.state_manager.get_state(track_id) != "CROSSING":
                continue

            # 5. 防抖: 远离计数线 + 端点过滤
            if self.anti_jitter:
                if not self.debounce_validator.is_clear_of_line(curr_point):
                    continue
                if not self._filter_endpoint_false_positive(track):
                    continue

            # 6. 滞留确认 (含滞回防抖)
            hold_result = self.debounce_validator.confirm_holding(
                track_id, curr_off, self._hysteresis_offset, self.hold_frames,
            )
            if hold_result == "pending":
                continue
            if hold_result == "side_changed":
                self.state_manager.update_crossing_start(track_id, prev_point)
                continue
            # hold_result == "confirmed": 继续

            # 7. 方向由跨线序列起止 offset 判定 (绝对语义, 不受帧间抖动影响)
            start_pos = self.state_manager.get_crossing_start(track_id) or prev_point
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
                self.state_manager.set_state(track_id, "TRACKING")
                continue

            # 8. 方向一致性验证: 跨线方向应与最近运动方向一致 (过滤 ID 切换)
            if not self.id_switch_detector.validate_direction_consistency(
                track, entry_exit, self._p2,
            ):
                self.state_manager.reset_track(track_id)
                self.debounce_validator.reset_track(track_id)
                continue

            # 9. 去重 + 反向冷却 + count_only
            if not self.state_manager.can_count(
                track_id, entry_exit, current_time,
                self.reverse_crossing_cooldown, self.count_only,
            ):
                self.state_manager.set_state(track_id, "TRACKING")
                continue

            # 10. 生成事件
            event = self.event_generator.generate(
                track, camera_id, curr_point, direction_str, entry_exit, self.line_name,
            )
            events.append(event)

            # 双向计数: 记录 track_id+direction, 允许反方向再计一次
            self.state_manager.mark_counted(track_id, entry_exit, current_time)
            self.state_manager.set_state(track_id, "TRACKING")

        # 清理过期轨迹
        self._cleanup_old_tracks(current_time)
        return events

    def _cleanup_old_tracks(self, current_time: Optional[float] = None):
        """清理长时间无跨线的轨迹状态; 按 TTL 淘汰已计数轨迹."""
        if current_time is None:
            current_time = datetime.now().timestamp()
        removed = self.state_manager.cleanup(current_time, self.counted_tracks_ttl)
        for track_id in removed:
            self.debounce_validator.reset_track(track_id)

    def reset(self):
        self.state_manager.reset()
        self.debounce_validator.reset()