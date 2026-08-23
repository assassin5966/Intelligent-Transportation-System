"""轨迹状态管理器：管理轨迹跨线状态、已计数去重、TTL清理等."""
from datetime import datetime
from typing import Dict, List, Optional, Tuple


class TrackStateManager:
    """轨迹状态管理器.

    负责管理所有与轨迹相关的状态，包括：
    - 跨线状态 (TRACKING / CROSSING)
    - 跨线历史记录
    - 跨线起始位置 (用于方向判定)
    - 已计数轨迹去重 (track_id + direction 维度)
    - TTL 清理 (防内存泄漏)
    """

    def __init__(self):
        # 轨迹跨线历史: track_id -> [(timestamp, line_name, direction), ...]
        self.track_crossing_history: Dict[str, List[Tuple[float, str, str]]] = {}
        # 轨迹状态: track_id -> "TRACKING" | "CROSSING"
        self.track_states: Dict[str, str] = {}
        # 跨线前位置: track_id -> [x, y]
        self.track_crossing_start_pos: Dict[str, List[float]] = {}
        # 双向计数去重: "track_id|direction" -> 计数时间戳
        self.counted_tracks: Dict[str, float] = {}

    def get_state(self, track_id: str) -> str:
        """获取轨迹状态, 默认 TRACKING."""
        return self.track_states.get(track_id, "TRACKING")

    def set_state(self, track_id: str, state: str):
        """设置轨迹状态."""
        self.track_states[track_id] = state

    def ensure_track_initialized(self, track_id: str):
        """确保轨迹的初始状态已创建."""
        if track_id not in self.track_states:
            self.track_states[track_id] = "TRACKING"
        if track_id not in self.track_crossing_history:
            self.track_crossing_history[track_id] = []

    def add_crossing_history(self, track_id: str, timestamp: float, line_name: str, direction: str):
        """记录跨线历史."""
        self.track_crossing_history[track_id].append((timestamp, line_name, direction))

    def start_crossing(self, track_id: str, prev_point: List[float], curr_side: int):
        """开始跨线确认: 记录起始位置和初始侧别.

        Args:
            track_id: 轨迹 ID
            prev_point: 跨线前位置
            curr_side: 跨线后侧别 (+1/-1)
        """
        self.track_crossing_start_pos[track_id] = list(prev_point)

    def update_crossing_start(self, track_id: str, prev_point: List[float]):
        """更新跨线起始位置 (侧别反转时)."""
        self.track_crossing_start_pos[track_id] = list(prev_point)

    def get_crossing_start(self, track_id: str) -> Optional[List[float]]:
        """获取跨线起始位置."""
        return self.track_crossing_start_pos.get(track_id)

    def determine_direction(
        self,
        track_id: str,
        curr_point: List[float],
        default_prev: List[float],
        line_start: List[float],
        normal: List[float],
        offset_fn,
    ) -> Optional[str]:
        """由跨线序列起止 offset 判定方向 (绝对语义, 不受帧间抖动影响).

        Args:
            track_id: 轨迹 ID
            curr_point: 当前帧中心点
            default_prev: 默认起始位置 (无记录时使用)
            line_start: 线段起点
            normal: 法向量
            offset_fn: offset 计算函数

        Returns:
            "enter" | "exit" | None (None 表示起止同侧, 不产出事件)
        """
        start_pos = self.track_crossing_start_pos.get(track_id, default_prev)
        start_off = offset_fn(start_pos, line_start, normal)
        end_off = offset_fn(curr_point, line_start, normal)

        if start_off < 0 and end_off > 0:
            return "enter"
        elif start_off > 0 and end_off < 0:
            return "exit"
        else:
            return None

    def can_count(
        self,
        track_id: str,
        entry_exit: str,
        current_time: float,
        reverse_crossing_cooldown: float,
        count_only: Optional[str] = None,
    ) -> bool:
        """检查是否可以计数 (双向去重 + 反向冷却 + count_only).

        Returns:
            True 表示可以计数, False 表示应跳过
        """
        dedup_key = f"{track_id}|{entry_exit}"

        # 双向去重: track_id+direction 维度, 允许同一轨迹来回各计一次
        if dedup_key in self.counted_tracks:
            return False

        # 反向跨线冷却: 同一轨迹反向事件需间隔冷却时间
        opposite_dir = "exit" if entry_exit == "enter" else "enter"
        opposite_key = f"{track_id}|{opposite_dir}"
        if opposite_key in self.counted_tracks:
            time_since_opposite = current_time - self.counted_tracks[opposite_key]
            if time_since_opposite < reverse_crossing_cooldown:
                return False

        # 单向计数模式: 只产出指定方向事件
        if count_only is not None and entry_exit != count_only:
            return False

        return True

    def mark_counted(self, track_id: str, entry_exit: str, current_time: float):
        """标记轨迹已计数."""
        dedup_key = f"{track_id}|{entry_exit}"
        self.counted_tracks[dedup_key] = current_time

    def reset_track(self, track_id: str):
        """清除轨迹的跨线状态 (ID 切换检测触发)."""
        self.track_crossing_start_pos.pop(track_id, None)

    def cleanup(self, current_time: Optional[float] = None, ttl: float = 300):
        """清理长时间无跨线的轨迹状态; 按 TTL 淘汰已计数轨迹.

        Args:
            current_time: 当前时间戳 (秒); 缺省用 datetime.now()
            ttl: 已计数轨迹保留时长 (秒)
        """
        if current_time is None:
            current_time = datetime.now().timestamp()

        # 清理长时间无跨线的轨迹 (20 秒)
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

        # 已计数轨迹按 TTL 淘汰: 7x24 流长期运行防止内存无限增长;
        # 流重连后跟踪器 ID 从头分配, 淘汰旧 ID 避免新轨迹被误判为已计数而漏计.
        expired = [
            tid for tid, ts in self.counted_tracks.items()
            if current_time - ts > ttl
        ]
        for tid in expired:
            self.counted_tracks.pop(tid, None)

        return to_remove

    def reset(self):
        """重置所有状态."""
        self.track_crossing_history.clear()
        self.track_states.clear()
        self.track_crossing_start_pos.clear()
        self.counted_tracks.clear()