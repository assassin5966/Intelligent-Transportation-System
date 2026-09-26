"""目标跟踪封装 (基于 ultralytics, 配置 bytetrack.yaml 实际使用 BoT-SORT 算法).

注: 类名保留 ByteTracker 为历史命名; 实际 tracker_type 由 configs/bytetrack.yaml
决定 (当前为 botsort). 支持 car/truck/bus/person 四类细分跟踪, 含轨迹历史与速度.

两种运行模式 (由 app/ai/model_pool.py 决定, 见 app/common/config.py 的 infer_mode):
  - legacy: 本类自持一个 YOLO 模型实例 (CPU 部署 / 离线工具 / gpu_batch 降级时使用),
    `track(frame)` 逐帧推理 -> 轨迹状态;
  - gpu_batch: 模型由 GPU 引擎按卡共享, 本类只保留每路轨迹状态,
    `track_from_raw(raw_tracks)` 直接把引擎结果转为 TrackResult.

每路的轨迹历史/速度/轨迹缝合状态已抽离到 app/ai/traj_state.py (TrajectoryState),
使"模型"与"每路状态"解耦; 本类的 track_history / track_class 以属性代理转发,
保持既有脚本与调用方 (scripts/zbny-*.py) 的用法不变.
"""
from pathlib import Path
from typing import Optional
import threading
import numpy as np
from ultralytics import YOLO

from ..common.business_rules import get_rule
from ..common.config import settings
from ..common.logger import logger

from . import DETECTION_CLASSES as _DETECTION_CLASSES
from .traj_state import RawTrack, Track, TrackResult, TrajectoryState

# 自定义跟踪配置路径 (文件名沿用 bytetrack, 实际算法由其中 tracker_type 决定)
TRACKER_CFG = str(Path(__file__).resolve().parents[2] / "configs" / "bytetrack.yaml")

# legacy 模式下各设备 ByteTracker 各自加载 YOLO; 多线程并发首次加载 (含 CUDA 初始化)
# 在 GPU 服务器上曾卡死, 用模块级锁串行化所有实例的首次加载
_MODEL_LOAD_LOCK = threading.Lock()

__all__ = ["ByteTracker", "Track", "TrackResult", "RawTrack", "TRACKER_CFG"]


class ByteTracker:
    def __init__(self, camera_type: Optional[str] = None):
        self._model = None
        self._camera_type = camera_type
        self._state = TrajectoryState(camera_type=camera_type)

    # ---- 兼容属性: 既有脚本直接读写 track_history / track_class ----

    @property
    def track_history(self):
        return self._state.track_history

    @track_history.setter
    def track_history(self, value):
        self._state.track_history = value

    @property
    def track_class(self):
        return self._state.track_class

    @track_class.setter
    def track_class(self, value):
        self._state.track_class = value

    @property
    def frame_id(self):
        return self._state.frame_id

    @property
    def max_history_length(self):
        return self._state.max_history_length

    @max_history_length.setter
    def max_history_length(self, value):
        self._state.max_history_length = value

    def _ensure_loaded(self):
        if self._model is None:
            import time as _time
            # 多路设备同时首帧推理会在多个线程并发初始化 CUDA/YOLO,
            # GPU 服务器上曾出现并发首推卡死; 串行化加载并在完成后打点,
            # 卡死时可从日志区分"卡在 YOLO() 构造"还是"卡在首次 track()"
            with _MODEL_LOAD_LOCK:
                if self._model is None:  # 双重检查: 等锁期间可能已被别路加载
                    import torch
                    logger.info(
                        f"加载 YOLO 模型 (跟踪): {settings.yolo_model} "
                        f"(device={'cuda' if torch.cuda.is_available() else 'cpu'})"
                    )
                    t0 = _time.monotonic()
                    self._model = YOLO(settings.yolo_model)
                    logger.info(f"YOLO 模型构造完成 (耗时 {_time.monotonic() - t0:.1f}s)")

    # ---- 运行参数 (热重载) ----

    def _track_params(self) -> tuple:
        """热重载跟踪参数 (yolo_conf/iou/track_buffer 修改后无需重启)."""
        conf = float(get_rule("tracking", "yolo_conf", default=settings.yolo_conf))
        iou = float(get_rule("tracking", "yolo_iou", default=settings.yolo_iou))
        track_buffer = int(get_rule("tracking", "track_buffer", default=settings.track_buffer))
        return conf, iou, track_buffer

    # ---- legacy 路径: 本地模型逐帧推理 ----

    def track(self, frame: np.ndarray) -> TrackResult:
        """legacy/CPU/离线路径: 本地模型推理 + 跟踪, 返回带历史的 TrackResult."""
        self._ensure_loaded()
        conf, iou, track_buffer = self._track_params()

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
            tracker=TRACKER_CFG
        )

        raw_tracks = []
        for result in results:
            if result.boxes.id is None:
                continue
            for box in result.boxes:
                track_id = int(box.id[0].item())
                class_id = int(box.cls[0].item())
                class_name = _DETECTION_CLASSES.get(class_id)
                if class_name is None:
                    continue
                raw_tracks.append(RawTrack(
                    track_id=track_id,
                    class_id=class_id,
                    class_name=class_name,
                    bbox=box.xyxy[0].cpu().numpy().tolist(),
                    confidence=box.conf[0].item(),
                ))

        return self._state.apply(raw_tracks, max_history_length=track_buffer)

    # ---- gpu_batch 路径: 只做每路状态维护 ----

    def track_from_raw(self, raw_tracks: list, max_history_length: Optional[int] = None) -> TrackResult:
        """批量引擎路径: 直接把引擎返回的原始轨迹转为带历史的 TrackResult."""
        if max_history_length is None:
            max_history_length = int(
                get_rule("tracking", "track_buffer", default=settings.track_buffer)
            )
        return self._state.apply(raw_tracks, max_history_length=max_history_length)

    def get_track_history(self, track_id: int):
        return self._state.get_track_history(track_id)
