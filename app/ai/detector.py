"""YOLO11 目标检测封装.

支持 car/truck/bus/person 四类细分检测.
"""
import cv2
import numpy as np
from datetime import datetime
from ultralytics import YOLO

from ..common.config import settings
from ..common.logger import logger
from . import DETECTION_CLASSES as _DETECTION_CLASSES


class Detection:
    def __init__(self, id, class_name, bbox, confidence, center):
        self.id = id
        self.class_name = class_name
        self.bbox = bbox
        self.confidence = confidence
        self.center = center


class DetectionResult:
    def __init__(self, frame_id, timestamp, detections):
        self.frame_id = frame_id
        self.timestamp = timestamp
        self.detections = detections


class YOLODetector:
    def __init__(self):
        model_name = settings.yolo_model
        self.conf_threshold = settings.yolo_conf
        self.iou_threshold = settings.yolo_iou
        
        self._model = None
        self.frame_id = 0

    def _ensure_loaded(self):
        if self._model is None:
            logger.info(f"加载 YOLO 模型: {settings.yolo_model}")
            self._model = YOLO(settings.yolo_model)

    def detect(self, frame: np.ndarray) -> DetectionResult:
        self._ensure_loaded()
        self.frame_id += 1
        
        results = self._model(
            frame,
            conf=self.conf_threshold,
            iou=self.iou_threshold,
            verbose=False
        )
        
        detections = []
        for result in results:
            for i, box in enumerate(result.boxes):
                class_id = int(box.cls[0].item())
                if class_id not in _DETECTION_CLASSES:
                    continue
                
                class_name = _DETECTION_CLASSES[class_id]
                confidence = box.conf[0].item()
                
                bbox = box.xyxy[0].cpu().numpy().tolist()
                x1, y1, x2, y2 = bbox
                center = [(x1 + x2) / 2, (y1 + y2) / 2]
                
                detection = Detection(
                    id=i,
                    class_name=class_name,
                    bbox=bbox,
                    confidence=confidence,
                    center=center
                )
                detections.append(detection)
        
        timestamp = datetime.now().isoformat()
        return DetectionResult(
            frame_id=self.frame_id,
            timestamp=timestamp,
            detections=detections
        )

    def detect_from_path(self, image_path: str) -> DetectionResult:
        frame = cv2.imread(image_path)
        if frame is None:
            raise ValueError(f"无法读取图像: {image_path}")
        return self.detect(frame)