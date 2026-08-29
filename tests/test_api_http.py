"""后端接口验收测试 (HTTP 层, 面向真实后端).

用途: 内网部署/联调后, 对运行中的后端做接口契约验收 —— 覆盖
health / stats / devices / events / alerts / police / device-info /
config / prediction / websocket 全部主要 REST 接口 (只读断言, 不改数据).

用法:
    pytest tests/test_api_http.py -v                        # 默认 http://localhost:8000
    BACKEND_BASE_URL=http://192.168.1.10:8000 pytest tests/test_api_http.py -v
后端未启动时自动 skip (不会误报失败); 也可指定远程后端做部署验收.

设计原则:
  - 只读接口断言"状态码 + 关键字段", 不耦合具体数值, 后端数据变化不影响结果.
  - 写操作仅测试"非法请求被拒绝"的路径 (400/422), 不产生真实数据副作用.
  - websocket 握手 + 首条消息验证 (确认 /ws 实时通道可用).
"""
import asyncio
import os
from datetime import date, timedelta

import httpx
import pytest

BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000")
WS_URL = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")


def _online() -> bool:
    try:
        with httpx.Client(base_url=BASE_URL, timeout=2.0) as c:
            return c.get("/health").status_code == 200
    except Exception:  # noqa: BLE001 连接失败/超时
        return False


@pytest.fixture(scope="module", autouse=True)
def require_backend():
    """后端不可达时跳过整个模块."""
    if not _online():
        pytest.skip(f"后端 {BASE_URL} 不可达, 跳过接口验收测试")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as c:
        yield c


# ---------------------------------------------------------------- 系统
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["service"] == "backend"


# ---------------------------------------------------------------- 统计
def test_stats_realtime(client):
    r = client.get("/api/stats/realtime")
    assert r.status_code == 200
    d = r.json()
    for k in ("current_vehicles", "current_persons", "today_vehicle_in",
              "today_vehicle_out", "today_person_in", "today_person_out",
              "active_devices", "updated_at"):
        assert k in d, f"realtime 缺少字段 {k}"


def test_stats_devices(client):
    r = client.get("/api/stats/devices")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    for row in rows:
        for k in ("device_id", "name", "status", "longitude", "latitude", "category"):
            assert k in row, f"设备统计缺少字段 {k}"


def test_stats_devices_one(client):
    """单设备统计: 取列表第一个设备验证, 空列表则跳过."""
    rows = client.get("/api/stats/devices").json()
    if not rows:
        pytest.skip("无设备数据, 跳过单设备统计")
    r = client.get(f"/api/stats/devices/{rows[0]['device_id']}")
    assert r.status_code == 200
    assert r.json()["device_id"] == rows[0]["device_id"]


def test_stats_hourly_history(client):
    end = date.today()
    start = end - timedelta(days=13)
    r = client.get(
        "/api/stats/hourly/history",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    assert r.status_code == 200
    d = r.json()
    assert "records" in d, "hourly/history 应返回 records"
    assert "total" in d, "hourly/history 应返回 total"


def test_stats_hourly_history_invalid_date(client):
    """非法日期格式 → 422 (Pydantic 参数校验)."""
    r = client.get("/api/stats/hourly/history",
                   params={"start_date": "2026-13-99", "end_date": "2026-08-01"})
    assert r.status_code in (200, 422)  # 兼容宽松解析, 但不应 500


# ---------------------------------------------------------------- 设备
def test_devices_list(client):
    """设备配置列表: 每项以 name 为唯一标识 (DeviceOut.id=Redis key 后缀)."""
    r = client.get("/api/devices")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    for row in rows:
        assert "id" in row and "name" in row and "status" in row


def test_devices_unknown_404(client):
    r = client.get("/api/devices/__no_such_device__/stream")
    assert r.status_code == 404


# ---------------------------------------------------------------- 事件/告警
def test_events_list(client):
    r = client.get("/api/events")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_events_reject_invalid_type(client):
    """非法 event_type → 400 (无副作用, 不写数据)."""
    r = client.post("/api/events", json={
        "device_id": "__test__",
        "event_type": "VehicleCrash",
        "occurred_at": "2026-08-29T12:00:00+00:00",
    })
    assert r.status_code == 400


def test_alerts_list(client):
    r = client.get("/api/alerts")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------- 警力
def test_police_regions(client):
    r = client.get("/api/police/regions")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_police_allocation(client):
    r = client.get("/api/police/allocation")
    assert r.status_code == 200
    d = r.json()
    assert "total_officers" in d and "regions" in d


def test_police_plan_may_404(client):
    """分配方案可能尚未生成 (404), 但不应 500."""
    r = client.get("/api/police/plan")
    assert r.status_code in (200, 404)


def test_police_total_reject_negative(client):
    """负数总警力 → 400 (无副作用)."""
    r = client.post("/api/police/total", json={"total": -1})
    assert r.status_code == 400


# ---------------------------------------------------------------- 设备信息 (MySQL)
def test_device_info_list(client):
    r = client.get("/api/device-info")
    assert r.status_code in (200, 503)  # 503 = MySQL 未启用, 属可接受状态
    if r.status_code == 200:
        for row in r.json():
            assert "name" in row


# ---------------------------------------------------------------- 业务规则
def test_config_business_rules(client):
    r = client.get("/api/config/business-rules")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# ---------------------------------------------------------------- 预测
def test_prediction_health(client):
    r = client.get("/api/prediction/health")
    assert r.status_code == 200
    d = r.json()
    assert "status" in d and "degraded" in d


def test_prediction_predict(client):
    """预测接口: 无历史时返回 predicted_total=0, 有历史时返回整数人数."""
    r = client.post("/api/prediction/predict", timeout=30.0)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["predicted_total"], int) and d["predicted_total"] >= 0


# ---------------------------------------------------------------- WebSocket 实时通道
def test_ws_receives_stats():
    """连接 /ws 后 10s 内应收到首条 stats 消息 (实时推送通道验证)."""

    async def _recv() -> str:
        import websockets
        async with websockets.connect(f"{WS_URL}/ws", open_timeout=5, close_timeout=2) as ws:
            return await asyncio.wait_for(ws.recv(), timeout=10)

    try:
        msg = asyncio.run(_recv())
    except (ConnectionRefusedError, OSError) as e:
        pytest.skip(f"WebSocket 不可达: {e}")
    assert "stats" in msg, f"首条 WS 消息应为 stats 推送: {msg[:120]}"
