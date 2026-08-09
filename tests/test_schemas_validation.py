"""Schema 校验测试 (P1-3): count_only / camera_type 取值约束.

验证三个入口 (后端 DeviceIn/DeviceEnableIn, AI 服务 DeviceRegister) 的 count_only
与 camera_type 字段使用 Literal 类型, 拒绝非法取值 (如大写 "Enter" / "in" / "both"),
接受合法取值 (enter / exit / vehicle / person / None).

可独立运行:
    python -m pytest tests/test_schemas_validation.py -v
或:
    python tests/test_schemas_validation.py
"""
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# stub ultralytics (本地未装; 仅 schema 校验不需要真实 YOLO)
if "ultralytics" not in sys.modules:
    _ul = types.ModuleType("ultralytics")
    _ul.YOLO = type("YOLO", (), {})  # 占位类
    sys.modules["ultralytics"] = _ul

from app.backend.api.devices import DeviceIn, DeviceEnableIn  # noqa: E402
from app.ai.service import DeviceRegister  # noqa: E402


# ===================== DeviceIn =====================

def test_device_in_accepts_valid_count_only():
    """DeviceIn 接受合法 count_only (None/enter/exit)."""
    DeviceIn(id="D1", name="cam", stream_url="rtmp://x")
    DeviceIn(id="D1", name="cam", stream_url="rtmp://x", count_only="enter")
    DeviceIn(id="D1", name="cam", stream_url="rtmp://x", count_only="exit")


def test_device_in_rejects_invalid_count_only():
    """DeviceIn 拒绝非法 count_only (大写/缩写/其他)."""
    for bad in ("Enter", "EXIT", "in", "out", "both", "enter ", ""):
        with pytest.raises(ValidationError):
            DeviceIn(id="D1", name="cam", stream_url="rtmp://x", count_only=bad)


def test_device_in_accepts_valid_camera_type():
    DeviceIn(id="D1", name="cam", stream_url="rtmp://x", camera_type="vehicle")
    DeviceIn(id="D1", name="cam", stream_url="rtmp://x", camera_type="person")


def test_device_in_rejects_invalid_camera_type():
    for bad in ("Vehicle", "car", "pedestrian", "all", "both"):
        with pytest.raises(ValidationError):
            DeviceIn(id="D1", name="cam", stream_url="rtmp://x", camera_type=bad)


# ===================== DeviceEnableIn =====================

def test_enable_in_accepts_valid_count_only():
    DeviceEnableIn(line_coords="0.1,0.4,0.9,0.4", count_only="enter")
    DeviceEnableIn(line_coords="0.1,0.4,0.9,0.4", count_only="exit")
    DeviceEnableIn(line_coords="0.1,0.4,0.9,0.4")  # None


def test_enable_in_rejects_invalid_count_only():
    with pytest.raises(ValidationError):
        DeviceEnableIn(line_coords="0.1,0.4,0.9,0.4", count_only="Enter")


def test_enable_in_rejects_invalid_camera_type():
    with pytest.raises(ValidationError):
        DeviceEnableIn(line_coords="0.1,0.4,0.9,0.4", camera_type="car")


# ===================== DeviceRegister (AI 服务) =====================

def test_register_accepts_valid_count_only():
    DeviceRegister(device_id="D1", stream_url="rtmp://x",
                   line=[[0.1, 0.4], [0.9, 0.4]], count_only="enter")
    DeviceRegister(device_id="D1", stream_url="rtmp://x",
                   line=[[0.1, 0.4], [0.9, 0.4]], count_only="exit")
    DeviceRegister(device_id="D1", stream_url="rtmp://x",
                   line=[[0.1, 0.4], [0.9, 0.4]])  # None


def test_register_rejects_invalid_count_only():
    for bad in ("Enter", "in", "both"):
        with pytest.raises(ValidationError):
            DeviceRegister(device_id="D1", stream_url="rtmp://x",
                           line=[[0.1, 0.4], [0.9, 0.4]], count_only=bad)


def test_register_rejects_invalid_camera_type():
    with pytest.raises(ValidationError):
        DeviceRegister(device_id="D1", stream_url="rtmp://x",
                       line=[[0.1, 0.4], [0.9, 0.4]], camera_type="car")


def _run_all():
    tests = [
        test_device_in_accepts_valid_count_only,
        test_device_in_rejects_invalid_count_only,
        test_device_in_accepts_valid_camera_type,
        test_device_in_rejects_invalid_camera_type,
        test_enable_in_accepts_valid_count_only,
        test_enable_in_rejects_invalid_count_only,
        test_enable_in_rejects_invalid_camera_type,
        test_register_accepts_valid_count_only,
        test_register_rejects_invalid_count_only,
        test_register_rejects_invalid_camera_type,
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
    sys.exit(0 if _run_all() else 1)
