"""ByteTrack 目标跟踪封装.

使用 ultralytics 内置 track (ByteTrack), 持久化 track_id.
"""
from dataclasses import dataclass

from ..common.config import settings
from ..common.logger import logger
from .detector import _category


@dataclass
class Track:
    track_id: int
    cls: int
    category: str  # "person" | "vehicle"
    box: tuple[float, float, float, float]
    center: tuple[float, float]


class Tracker:
    """ByteTrack 跟踪器 (检测+跟踪一体, persist=True 跨帧保持 ID)."""

    def __init__(self, model: str | None = None, conf: float | None = None):
        self.model_name = model or settings.yolo_model
        self.conf = conf or settings.yolo_conf
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from ultralytics import YOLO

            logger.info(f"加载 YOLO 模型 (跟踪): {self.model_name}")
            self._model = YOLO(self.model_name)

    def update(self, frame) -> list[Track]:
        """对一帧做跟踪, 返回带 track_id 的对象列表."""
        self._ensure_loaded()
        results = self._model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            conf=self.conf,
            iou=settings.yolo_iou,
            classes=settings.detect_classes,
            verbose=False,
        )
        tracks: list[Track] = []
        for r in results:
            boxes = r.boxes
            if boxes is None or boxes.id is None:
                continue
            for i in range(len(boxes)):
                xyxy = boxes.xyxy[i].cpu().numpy().tolist()
                tid = int(boxes.id[i].cpu().item())
                c = int(boxes.cls[i].cpu().item())
                cx = (xyxy[0] + xyxy[2]) / 2.0
                cy = (xyxy[1] + xyxy[3]) / 2.0
                tracks.append(
                    Track(
                        track_id=tid,
                        cls=c,
                        category=_category(c),
                        box=(xyxy[0], xyxy[1], xyxy[2], xyxy[3]),
                        center=(cx, cy),
                    )
                )
        return tracks
