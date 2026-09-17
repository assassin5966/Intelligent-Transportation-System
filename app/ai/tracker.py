"""目标跟踪封装 (基于 ultralytics, 配置 bytetrack.yaml 实际使用 BoT-SORT 算法).

注: 类名保留 ByteTracker 为历史命名; 实际 tracker_type 由 configs/bytetrack.yaml
决定 (当前为 botsort). 支持 car/truck/bus/person 四类细分跟踪, 含轨迹历史与速度.
"""
from pathlib import Path
from typing import Optional
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
                
                if track_id not in self.track_history:
                    self.track_history[track_id] = []
                
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

    def get_track_history(self, track_id: int):
        return self.track_history.get(track_id, [])