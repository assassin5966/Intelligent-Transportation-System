"""WVP-GB28181 客户端单元测试.

可独立运行:
    python -m pytest tests/test_wvp_client.py -v
或:
    python tests/test_wvp_client.py

mock httpx, 不依赖真实 WVP / Redis / 网络.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.backend.core import wvp_sync  # noqa: E402
from app.backend.core.wvp_client import WVPClient, WVPError, get_wvp_client  # noqa: E402


def _resp(status_code=200, json_data=None):
    """构造 mock httpx.Response."""
    r = Mock()
    r.status_code = status_code
    r.json = Mock(return_value=json_data if json_data is not None else {})
    r.raise_for_status = Mock()
    return r


def _make_client(enabled=True):
    """构造 WVPClient, 注入 mock httpx client (跳过真实网络)."""
    c = WVPClient()
    c.enabled = enabled
    c._client = Mock()
    c._client.request = AsyncMock()
    c._client.post = AsyncMock()
    c._client.aclose = AsyncMock()
    c._token = "tok" if enabled else None  # enabled 时跳过登录
    return c


def test_disabled_returns_empty():
    """wvp_enabled=False: 所有方法返回空值."""
    c = _make_client(enabled=False)
    assert asyncio.run(c.list_devices()) == []
    assert asyncio.run(c.start_play("d", "ch")) is None
    assert asyncio.run(c.stop_play("d", "ch")) is False
    assert c.select_stream_url(None) is None


def test_login_caches_token():
    c = _make_client()
    c._token = None
    c._client.post.return_value = _resp(200, {"data": {"accessToken": "tok-abc"}})
    asyncio.run(c._login())
    assert c._token == "tok-abc"
    # 兼容 access-token 字段名
    c._token = None
    c._client.post.return_value = _resp(200, {"data": {"access-token": "tok-xyz"}})
    asyncio.run(c._login())
    assert c._token == "tok-xyz"


def test_list_devices_pagination():
    """分页: total=150, 每页 100, 应拉 2 页共 150 条."""
    c = _make_client()
    page1 = {
        "code": 0,
        "data": {"total": 150, "list": [{"deviceId": f"d{i}"} for i in range(100)]},
    }
    page2 = {
        "code": 0,
        "data": {"total": 150, "list": [{"deviceId": f"d{i}"} for i in range(100, 150)]},
    }
    c._client.request.side_effect = [_resp(200, page1), _resp(200, page2)]
    devices = asyncio.run(c.list_devices())
    assert len(devices) == 150
    assert devices[0]["deviceId"] == "d0"
    assert devices[-1]["deviceId"] == "d149"


def test_start_play_new_format():
    """新版 wvp-pro: data.flv 直接含地址."""
    c = _make_client()
    body = {
        "code": 0,
        "data": {
            "flv": "http://zlm/live/x.flv",
            "rtsp": "rtsp://zlm/live/x",
            "streamId": "x",
        },
    }
    c._client.request.return_value = _resp(200, body)
    res = asyncio.run(c.start_play("d", "ch"))
    assert res is not None
    assert res["flv"] == "http://zlm/live/x.flv"
    assert res["stream_id"] == "x"
    assert c.select_stream_url(res) == "http://zlm/live/x.flv"


def test_start_play_old_format():
    """旧版: data.stream.flv 嵌套结构."""
    c = _make_client()
    body = {
        "code": 0,
        "data": {"stream": {"flv": "http://zlm/live/y.flv", "rtsp": "rtsp://zlm/live/y"}},
    }
    c._client.request.return_value = _resp(200, body)
    res = asyncio.run(c.start_play("d", "ch"))
    assert res is not None
    assert res["flv"] == "http://zlm/live/y.flv"
    assert res["stream_id"] == "d_ch"  # 无显式 streamId, 回退拼接


def test_start_play_failure_returns_none():
    """WVP 点播报错时返回 None (不抛)."""
    c = _make_client()
    c._client.request.side_effect = WVPError("boom")
    assert asyncio.run(c.start_play("d", "ch")) is None


def test_401_triggers_relogin():
    """token 失效(401): 自动重登一次后重试成功."""
    c = _make_client()
    c._token = "stale"
    ok_body = {"code": 0, "data": {"flv": "http://zlm/live/z.flv"}}
    c._client.request.side_effect = [_resp(401), _resp(200, ok_body)]
    c._client.post.return_value = _resp(200, {"data": {"accessToken": "fresh"}})
    res = asyncio.run(c.start_play("d", "ch"))
    assert res["flv"] == "http://zlm/live/z.flv"
    assert c._token == "fresh"  # token 已更新


def test_select_stream_url_protocol():
    """按 wvp_play_protocol 选择地址, 并在缺失时回退."""
    from app.common.config import settings

    c = _make_client()
    play = {"flv": "http://x.flv", "rtsp": "rtsp://x"}
    orig = settings.wvp_play_protocol
    try:
        settings.wvp_play_protocol = "rtsp"
        assert c.select_stream_url(play) == "rtsp://x"
        settings.wvp_play_protocol = "flv"
        assert c.select_stream_url(play) == "http://x.flv"
        # 缺失回退: 只有 flv 时 rtsp 协议应回退到 flv
        settings.wvp_play_protocol = "rtsp"
        assert c.select_stream_url({"flv": "http://only.flv"}) == "http://only.flv"
    finally:
        settings.wvp_play_protocol = orig


def test_sync_once_disabled():
    """wvp_enabled=False: sync_once 直接返回 skipped, 不触碰 Redis."""
    get_wvp_client().enabled = False
    res = asyncio.run(wvp_sync.sync_once())
    assert res["skipped"] == "wvp_disabled"
    assert res["added"] == 0


def test_infer_camera_type():
    assert wvp_sync._infer_camera_type("南门车辆摄像头") == "vehicle"
    assert wvp_sync._infer_camera_type("人行出入口") == "person"
    assert wvp_sync._infer_camera_type("Vehicle Cam 01") == "vehicle"
    assert wvp_sync._infer_camera_type("普通通道") is None
    assert wvp_sync._infer_camera_type("") is None


if __name__ == "__main__":
    # 直接运行: 逐个执行测试函数
    import inspect

    funcs = [
        (n, f)
        for n, f in sorted(globals().items())
        if n.startswith("test_") and inspect.isfunction(f)
    ]
    passed = 0
    for name, fn in funcs:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            import traceback

            print(f"[FAIL] {name}: {e}")
            traceback.print_exc()
    print(f"\n{passed}/{len(funcs)} passed")
    sys.exit(0 if passed == len(funcs) else 1)
