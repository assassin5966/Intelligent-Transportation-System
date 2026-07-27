"""AI 分析层 (Python): YOLO11 检测 + ByteTrack 跟踪 + 越线计数 + 事件推送."""

# COCO class_id -> 业务类别 (tracker 使用, 单一数据源避免不一致)
DETECTION_CLASSES = {
    2: "car",
    7: "truck",
    5: "bus",
    0: "person",
    1: "bicycle",
    3: "motorcycle",
}

# 摄像头类型 -> 检测类别映射
# vehicle: 机动车 (car/truck/bus)
# person: 人流 (person/bicycle/motorcycle, 含非机动车)
CAMERA_TYPE_CLASSES = {
    "vehicle": {"car", "truck", "bus"},
    "person": {"person", "bicycle", "motorcycle"},
}
