"""视频异常识别单元测试 (黑屏 + 花屏).

可独立运行:
    python -m pytest tests/test_anomaly.py -v
或:
    python tests/test_anomaly.py

合成帧验证, 不依赖 Redis / 模型 / 真实视频.
"""
import sys
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.ai.anomaly import (  # noqa: E402
    AnomalyConfig,
    AnomalyDetector,
    AnomalyMonitor,
    BLACK_SCREEN,
    FLOWER_SCREEN,
)


def _cfg(**overrides) -> AnomalyConfig:
    """构造测试配置 (默认宽松阈值, 便于合成帧稳定触发)."""
    base = dict(
        analysis_width=160,
        black_screen_brightness=20,
        black_screen_ratio=0.95,
        black_pixel_value=20,
        flower_block_grid=8,
        flower_noise_std=30.0,
        flower_uniformity=0.5,
        flower_channel_corr=0.5,
        flower_temporal_diff=20.0,
        check_interval=1,  # 测试: 每帧都检测
        confirm_frames=2,
    )
    base.update(overrides)
    return AnomalyConfig(**base)


def _normal_frame(h=120, w=160) -> np.ndarray:
    """合成正常帧: 彩色渐变 + 几何图形 (通道高度相关, 有结构纹理)."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    # 灰度渐变 (R=G=B 高相关)
    grad = np.tile(np.linspace(0, 255, w, dtype=np.uint8), (h, 1))
    img[:, :, 0] = grad
    img[:, :, 1] = grad
    img[:, :, 2] = grad
    # 加几块稳定色块 (结构纹理, 非随机)
    img[20:40, 20:60] = (60, 120, 180)
    img[60:90, 80:130] = (200, 100, 50)
    return img


def _black_frame(h=120, w=160) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _noise_frame(h=120, w=160, seed: int = 0) -> np.ndarray:
    """合成花屏帧: 每通道独立随机 (通道去相关, 高空间噪声, 高时域差分)."""
    rng = np.random.RandomState(seed)
    return rng.randint(0, 256, size=(h, w, 3), dtype=np.uint8)


# ---------------- AnomalyDetector ----------------

def test_black_screen_detected():
    det = AnomalyDetector(_cfg())
    res = det.detect(_black_frame())
    assert res.anomaly_type == BLACK_SCREEN
    assert res.scores["brightness"] < 1


def test_flower_screen_detected():
    det = AnomalyDetector(_cfg())
    # 第一帧噪声 (无前帧, 靠去相关)
    res = det.detect(_noise_frame(seed=1))
    assert res.anomaly_type == FLOWER_SCREEN, res.scores
    # 第二帧噪声 (有前帧, 时域差分也高)
    res2 = det.detect(_noise_frame(seed=2))
    assert res2.anomaly_type == FLOWER_SCREEN, res2.scores


def test_normal_frame_not_flagged():
    det = AnomalyDetector(_cfg())
    res = det.detect(_normal_frame())
    assert res.anomaly_type is None, res.scores
    # 通道相关性应高
    assert res.scores["channel_corr"] > 0.8


def test_dark_but_not_black_not_flagged():
    """低光但非纯黑 (有少量亮度变化) 不应判黑屏."""
    det = AnomalyDetector(_cfg())
    frame = np.full((120, 160, 3), 25, dtype=np.uint8)  # 均值 25 > brightness 阈值 20
    res = det.detect(frame)
    assert res.anomaly_type is None


def test_temporal_none_on_first_frame():
    det = AnomalyDetector(_cfg())
    res = det.detect(_noise_frame(seed=0))
    # 第一帧无前帧 -> temporal_diff 为 None
    assert res.scores["temporal_diff"] is None


# ---------------- AnomalyMonitor ----------------

def test_monitor_debounce_and_onset():
    cfg = _cfg(check_interval=1, confirm_frames=2)
    mon = AnomalyMonitor(cfg)
    # 第 1 帧噪声: consecutive=1 < 2, 无事件
    ev1 = mon.check(_noise_frame(seed=1))
    assert ev1 is None
    assert mon.state is None
    # 第 2 帧噪声: consecutive=2, onset
    ev2 = mon.check(_noise_frame(seed=2))
    assert ev2 is not None
    assert ev2.anomaly_type == FLOWER_SCREEN
    assert ev2.phase == "onset"
    assert mon.state == FLOWER_SCREEN
    # 第 3 帧噪声: 同状态持续, 无新事件
    ev3 = mon.check(_noise_frame(seed=3))
    assert ev3 is None


def test_monitor_recovery():
    cfg = _cfg(check_interval=1, confirm_frames=1)
    mon = AnomalyMonitor(cfg)
    mon.check(_noise_frame(seed=1))  # onset (confirm=1)
    assert mon.state == FLOWER_SCREEN
    # 恢复正常 -> recovery 事件
    ev = mon.check(_normal_frame())
    assert ev is not None
    assert ev.phase == "recovery"
    assert ev.anomaly_type == FLOWER_SCREEN
    assert mon.state is None


def test_monitor_sampling_interval():
    cfg = _cfg(check_interval=3, confirm_frames=1)
    mon = AnomalyMonitor(cfg)
    # 前两帧不采样
    assert mon.check(_noise_frame(seed=1)) is None  # counter=1, 1%3!=0
    assert mon.check(_noise_frame(seed=2)) is None  # counter=2
    # 第 3 帧采样 -> onset
    ev = mon.check(_noise_frame(seed=3))  # counter=3
    assert ev is not None
    assert ev.phase == "onset"


def test_monitor_normal_segment_no_events():
    cfg = _cfg(check_interval=1, confirm_frames=1)
    mon = AnomalyMonitor(cfg)
    for i in range(5):
        assert mon.check(_normal_frame()) is None
    assert mon.state is None


# ---------------- 自运行入口 ----------------

def _run_all():
    tests = [
        test_black_screen_detected,
        test_flower_screen_detected,
        test_normal_frame_not_flagged,
        test_dark_but_not_black_not_flagged,
        test_temporal_none_on_first_frame,
        test_monitor_debounce_and_onset,
        test_monitor_recovery,
        test_monitor_sampling_interval,
        test_monitor_normal_segment_no_events,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} 通过")
    return passed == len(tests)


if __name__ == "__main__":
    import sys
    sys.exit(0 if _run_all() else 1)
