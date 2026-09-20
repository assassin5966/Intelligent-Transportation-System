"""目标跟踪封装 (基于 ultralytics, 配置 bytetrack.yaml 实际使用 BoT-SORT 算法).

注: 类名保留 ByteTracker 为历史命名; 实际 tracker_type 由 configs/bytetrack.yaml
决定 (当前为 botsort). 支持 car/truck/bus/person 四类细分跟踪, 含轨迹历史与速度.

轨迹缝合 (trajectory stitching, 高速目标补计):
  高速车运动模糊导致漏检数帧 -> 旧轨迹在 BoT-SORT 内死亡, 车以新 ID 重现 ->
  跨线动作发生在换 ID 的缝隙里, 两个 ID 各自的历史都不完整, 跨线漏计.
  本类在检测框层面缝合: 新 ID 出现时扫描"消失中的同类别旧轨迹",
  满足位置外推衔接 (旧轨迹末位 + 末速 × 消失帧数 ≈ 新检测位置)、运动速度合理
  且方向一致的, 把旧历史拼接到新轨迹前. 计数器看到的是一条连续轨迹,
  可正常完成跨线判定. 安全约束:
  - 只接续"本帧无检测"的轨迹 (在场轨迹 ID 是真实的);
  - 同一旧轨迹只被缝合一次 (防止分叉双计);
  - 类别必须一致 (车不接人的轨迹);
  - 外推点与新车 bbox 距离超过 bbox 对角线 × 系数即放弃 (宁漏不误).
"""
from pathlib import Path
from typing import Optional
import math
import numpy as np
from collections import OrderedDict
from ultralytics import YOLO

from ..common.business_rules import get_rule
from ..common.config import settings
from ..common.logger import logger

from . import DETECTION_CLASSES as _DETECTION_CLASSES
from . import CAMERA_TYPE_CLASSES as _CAMERA_TYPE_CLASSES

# 自定义跟踪配置路径 (文件名沿用 bytetrack, 实际算法由其中 tracker_type 决定)
_TRACKER_CFG = str(Path(__file__).resolve().parents[2] / "configs" / "bytetrack.yaml")


class Track:
    def __init__(self, track_id, class_name, bbox, center, confidence, age, velocity, history):
        self.track_id = str(track_id)
        self.class_name = class_name
        self.bbox = bbox
        self.center = center
        self.confidence = confidence
        self.age = age
        self.velocity = velocity
        self.history = history


class TrackResult:
    def __init__(self, frame_id, tracks):
        self.frame_id = frame_id
        self.tracks = tracks


class ByteTracker:
    def __init__(self, camera_type: Optional[str] = None):
        self._model = None
        self.track_history = OrderedDict()
        self.max_history_length = settings.track_buffer
        # 短暂遮挡 (货车间歇遮挡/黄昏漏检) 后同一 track_id 重现时保留轨迹历史,
        # 使跨线计数能感知遮挡期间完成的跨越. 取 40 处理帧, 需 >= bytetrack.yaml
        # track_buffer(30): BoT-SORT 对丢失轨迹的保留上限, 同 ID 重现必为同一目标
        # (ultralytics 会话内 ID 单调递增不复用); 超过 buffer 后 ID 已更换, 旧历史
        # 由本表清空, 不会把新目标误接到旧轨迹上.
        self._miss_grace = 40
        self._miss_count: dict = {}
        self.frame_id = 0
        self._camera_type = camera_type
        self._allowed_classes = None
        if camera_type and camera_type in _CAMERA_TYPE_CLASSES:
            self._allowed_classes = _CAMERA_TYPE_CLASSES[camera_type]
        # ---- 轨迹缝合 (高速目标补计) ----
        # 高速车运动模糊导致漏检数帧: 旧轨迹在 miss 宽限期内保留 (本表), 以新 ID
        # 重现时若位置可外推衔接则缝合历史. 判据从严 (宁漏不误):
        #   类别一致 + 消失 <= gap 帧 + 匀速外推落点在新 bbox 邻域 + 速度量级合理
        self._stitch_gap_frames = 12   # 允许接续的最大消失帧数
        self._stitch_max_jump = 2.0    # 外推点距新 bbox 中心 <= 对角线 × 系数
        self._stitch_cos = 0.5         # 缝隙位移方向与旧轨迹末速方向余弦下限
        self._stitched_from: set = set()  # 已被缝合的旧 ID (防重复缝合)
        self.track_class: dict = {}    # track_id -> class_name (缝合类别校验)
        self._current_track_ids: set = set()  # 本帧在场的 track_id

    # ---- 轨迹缝合 ----

    def _stitch_history(self, track_id: str, class_name: str, bbox) -> list:
        """新轨迹尝试接续一条消失中的同类别旧轨迹, 返回拼接后历史 (可为空).

        衔接判据 (宁漏不误):
          1. 旧轨迹"消失中"(本帧无检测) 且消失 <= _stitch_gap_frames 帧;
          2. 类别一致;
          3. 旧轨迹末端以最后速度匀速外推 (消失帧数 + 1) 帧后的位置,
             落在新检测 bbox 中心 <= bbox 对角线 × _stitch_max_jump 邻域内;
          4. 旧轨迹末位 -> 新位置的缝隙位移方向与旧轨迹末速方向一致
             (余弦 >= _stitch_cos), 排除反向/横向误接.
        同帧多候选时取外推距离最小者; 同一旧轨迹只缝合一次.
        """
        candidates = []
        for tid, hist in self.track_history.items():
            if tid == track_id or len(hist) < 3:
                continue
            # 只接续"消失中"的轨迹 (本帧无检测): 在场轨迹的 ID 是真实分配的
            if tid in self._current_track_ids:
                continue
            miss = self._miss_count.get(tid, 0)
            if miss == 0 or miss > self._stitch_gap_frames:
                continue
            # 类别一致 (车不接人的轨迹)
            if self.track_class.get(tid) != class_name:
                continue
            v = self._calculate_velocity(hist)
            speed = math.hypot(v[0], v[1])
            if speed < 1e-6:
                continue
            # 匀速外推: 旧轨迹最后位置 + 末速度 × (消失帧数 + 1)
            n = miss + 1
            ex = hist[-1][0] + v[0] * n
            ey = hist[-1][1] + v[1] * n
            x1, y1, x2, y2 = bbox
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            diag = math.hypot(x2 - x1, y2 - y1)
            if diag <= 0:
                continue
            gap = math.hypot(ex - cx, ey - cy)
            if gap > diag * self._stitch_max_jump:
                continue
            # 方向一致性: 缝隙位移 (旧末位 -> 新位置) 应与旧轨迹末速同向
            gx, gy = cx - hist[-1][0], cy - hist[-1][1]
            gl = math.hypot(gx, gy)
            if gl < 1e-6:
                continue
            cos_sim = (gx * v[0] + gy * v[1]) / (gl * speed)
            if cos_sim < self._stitch_cos:
                continue
            candidates.append((gap, tid, hist))
        if not candidates:
            return []
        # 取外推衔接误差最小的候选
        _, best_tid, old_hist = min(candidates, key=lambda c: c[0])
        old_hist = list(old_hist)
        del self.track_history[best_tid]
        self._stitched_from.add(best_tid)
        self._miss_count.pop(best_tid, None)
        logger.info(
            f"轨迹缝合: 旧轨迹 {best_tid} 接续到新轨迹 {track_id} "
            f"(继承 {len(old_hist)} 点历史, 类别 {class_name})"
        )
        return old_hist

    def _ensure_loaded(self):
        if self._model is None:
            logger.info(f"加载 YOLO 模型 (跟踪): {settings.yolo_model}")
            self._model = YOLO(settings.yolo_model)

    def track(self, frame: np.ndarray) -> TrackResult:
        self._ensure_loaded()
        self.frame_id += 1
        # 热重载跟踪参数 (yolo_conf/iou/track_buffer 修改后无需重启)
        conf = float(get_rule("tracking", "yolo_conf", default=settings.yolo_conf))
        iou = float(get_rule("tracking", "yolo_iou", default=settings.yolo_iou))
        track_buffer = int(get_rule("tracking", "track_buffer", default=settings.track_buffer))
        self.max_history_length = track_buffer

        # persist=True 必须保留: ultralytics 在 persist=False 时每次 predict 都会
        # 重建 tracker (并重置全局 ID 计数器), 逐帧调用 model.track() 会让 ID
        # 每帧从 1 重新分配 -> 同一 track_id 混入不同目标, 轨迹历史错乱,
        # 跨线状态反复被 ID 切换逻辑重置, 表现为跨线计数统计不上/恒为 0.
        results = self._model.track(
            frame,
            persist=True,
            conf=conf,
            iou=iou,
            verbose=False,
            tracker=_TRACKER_CFG
        )

        tracks = []
        current_track_ids = set()

        for result in results:
            if result.boxes.id is None:
                continue

            for i, box in enumerate(result.boxes):
                track_id = int(box.id[0].item())
                class_id = int(box.cls[0].item())

                if class_id not in _DETECTION_CLASSES:
                    continue

                class_name = _DETECTION_CLASSES[class_id]

                if self._allowed_classes and class_name not in self._allowed_classes:
                    continue

                confidence = box.conf[0].item()

                bbox = box.xyxy[0].cpu().numpy().tolist()
                x1, y1, x2, y2 = bbox
                center = [(x1 + x2) / 2, (y1 + y2) / 2]

                is_new = track_id not in self.track_history
                if is_new:
                    # 轨迹缝合: 尝试接续一条消失中的同类别旧轨迹 (高速车漏检补计)
                    self.track_history[track_id] = self._stitch_history(
                        track_id, class_name, bbox
                    )
                self.track_class[track_id] = class_name

                self.track_history[track_id].append(center)
                if len(self.track_history[track_id]) > self.max_history_length:
                    self.track_history[track_id].pop(0)

                history = self.track_history[track_id]
                velocity = self._calculate_velocity(history)

                track = Track(
                    track_id=track_id,
                    class_name=class_name,
                    bbox=bbox,
                    center=center,
                    confidence=confidence,
                    age=len(history),
                    velocity=velocity,
                    history=history
                )
                tracks.append(track)
                current_track_ids.add(track_id)

        self._cleanup_history(current_track_ids)
        self._current_track_ids = current_track_ids

        return TrackResult(frame_id=self.frame_id, tracks=tracks)

    def _calculate_velocity(self, history):
        if len(history) < 2:
            return [0.0, 0.0]

        recent = history[-3:] if len(history) >= 3 else history[-2:]
        velocities = []

        for i in range(1, len(recent)):
            dx = recent[i][0] - recent[i-1][0]
            dy = recent[i][1] - recent[i-1][1]
            velocities.append([dx, dy])

        avg_vx = sum(v[0] for v in velocities) / len(velocities)
        avg_vy = sum(v[1] for v in velocities) / len(velocities)

        return [avg_vx, avg_vy]

    def _cleanup_history(self, current_track_ids):
        to_remove = []
        for tid in self.track_history:
            if tid in current_track_ids:
                self._miss_count[tid] = 0
                continue
            self._miss_count[tid] = self._miss_count.get(tid, 0) + 1
            if self._miss_count[tid] > self._miss_grace:
                to_remove.append(tid)
        for tid in to_remove:
            del self.track_history[tid]
            self._miss_count.pop(tid, None)
            self.track_class.pop(tid, None)

    def get_track_history(self, track_id: int):
        return self.track_history.get(track_id, [])
