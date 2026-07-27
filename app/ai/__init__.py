"""AI 分析层 (Python): YOLO11 检测 + ByteTrack 跟踪 + 越线计数 + 事件推送."""

# COCO class_id -> 业务类别 (tracker 使用, 单一数据源避免不一致)
DETECTION_CLASSES = {
    2: "car",
    7: "truck",
    5: "bus",
    0: "person",
}
