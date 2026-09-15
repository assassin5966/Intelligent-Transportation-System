"""流地址翻译单元测试 (core/stream_urls.py).

可独立运行:
    python -m pytest tests/test_stream_urls.py -v
"""
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.common.config import settings  # noqa: E402
from app.backend.core.stream_urls import to_browser, to_internal  # noqa: E402

WVP_URL = "http://23.45.1.112:80/rtp/34020000002000000001_34020000001320000001.live.flv"


def test_to_internal_rewrites_host_keeps_path():
    """配置 zlm_internal_base: 只换 host, path 原样 (stream_id 在 path 里)."""
    old = settings.zlm_internal_base
    try:
        settings.zlm_internal_base = "http://zlmediakit"
        assert to_internal(WVP_URL) == "http://zlmediakit/rtp/34020000002000000001_34020000001320000001.live.flv"
    finally:
        settings.zlm_internal_base = old


def test_to_internal_passthrough_when_unset():
    """未配置: 原样返回."""
    old = settings.zlm_internal_base
    try:
        settings.zlm_internal_base = ""
        assert to_internal(WVP_URL) == WVP_URL
        assert to_internal(None) is None
    finally:
        settings.zlm_internal_base = old


def test_to_browser_strips_trailing_slash():
    """配置 zlm_public_base: 重写且容忍末尾斜杠."""
    old = settings.zlm_public_base
    try:
        settings.zlm_public_base = "http://192.168.1.28:80/"
        assert to_browser(WVP_URL) == "http://192.168.1.28:80/rtp/34020000002000000001_34020000001320000001.live.flv"
    finally:
        settings.zlm_public_base = old


def test_to_browser_passthrough_when_unset():
    """未配置: 原样返回 (sdp-ip 局域网可达的部署直接可用)."""
    old = settings.zlm_public_base
    try:
        settings.zlm_public_base = ""
        assert to_browser(WVP_URL) == WVP_URL
        assert to_browser(None) is None
    finally:
        settings.zlm_public_base = old
