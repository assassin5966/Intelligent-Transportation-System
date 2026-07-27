"""ByteTrack 目标跟踪封装.

支持 car/truck/bus/person 四类细分跟踪, 包含轨迹历史和速度计算.
使用自定义 bytetrack.yaml 配置 (configs/bytetrack.yaml), 降低 ID 切换.
"""
from pathlib import Path
import numpy as np
from collections import OrderedDict
from ultralytics import YOLO

from ..common.config import settings
from ..common.logger import logger

# 自定义 ByteTrack 配置路径
_TRACKER_CFG = str(Path(__file__).resolve().parents[2] / "configs" / "bytetrack.yaml")

_DETECTION_CLASSES = {
    2: "car",
    7: "truck",
    5: "bus",
    0: "person"
}


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
    def __init__(self):
        self._model = None
        self.track_history = OrderedDict()
        self.max_history_length = settings.track_buffer
        self._miss_grace = 5  # 连续丢失多少帧后才清除轨迹历史
        self._miss_count: dict = {}
        self.frame_id = 0

    def _ensure_loaded(self):
        if self._model is None:
            logger.info(f"加载 YOLO 模型 (跟踪): {settings.yolo_model}")
            self._model = YOLO(settings.yolo_model)

    def track(self, frame: np.ndarray) -> TrackResult:
        self._ensure_loaded()
        self.frame_id += 1
        
        results = self._model.track(
            frame,
            conf=settings.yolo_conf,
            iou=settings.yolo_iou,
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