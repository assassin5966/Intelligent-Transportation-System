"""YOLO11 目标检测封装.

将 COCO 类别映射为业务类别: person / vehicle.
"""
from dataclasses import dataclass

from ..common.config import settings
from ..common.logger import logger

# COCO -> 业务类别
_PERSON_CLS = {0}                       # person
_VEHICLE_CLS = {1, 2, 3, 5, 7}          # bicycle, car, motorcycle, bus, truck


@dataclass
class Detection:
    box: tuple[float, float, float, float]  # x1, y1, x2, y2
    conf: float
    cls: int
    category: str  # "person" | "vehicle"


def _category(cls: int) -> str:
    return "person" if cls in _PERSON_CLS else "vehicle"


class Detector:
    """YOLO11 检测器 (懒加载)."""

    def __init__(self, model: str | None = None, conf: float | None = None):
        self.model_name = model or settings.yolo_model
        self.conf = conf or settings.yolo_conf
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from ultralytics import YOLO

            logger.info(f"加载 YOLO 模型: {self.model_name}")
            self._model = YOLO(self.model_name)

    def detect(self, frame) -> list[Detection]:
        self._ensure_loaded()
        results = self._model.predict(
            frame,
            conf=self.conf,
            iou=settings.yolo_iou,
            classes=settings.detect_classes,
            verbose=False,
        )
        dets: list[Detection] = []
        for r in results:
            boxes = r.boxes
            if boxes is None:
                continue
            for i in range(len(boxes)):
                xyxy = boxes.xyxy[i].cpu().numpy().tolist()
                c = int(boxes.cls[i].cpu().item())
                cf = float(boxes.conf[i].cpu().item())
                dets.append(
                    Detection(
                        box=(xyxy[0], xyxy[1], xyxy[2], xyxy[3]),
                        conf=cf,
                        cls=c,
                        category=_category(c),
                    )
                )
        return dets
