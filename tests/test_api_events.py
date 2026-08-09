"""事件 API 功能测试 (集成层).

用 FastAPI TestClient 验证 /api/events 的 POST (事件入库) + GET (历史读取) 全链路:
  HTTP 请求 -> 路由 -> 处理函数 -> realtime (Redis) -> 响应.

使用 FakeRedis (内存模拟), 不依赖真实 Redis / 模型 / WVP.

可独立运行:
    python -m pytest tests/test_api_events.py -v
或:
    python tests/test_api_events.py
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.backend.api import events as events_api  # noqa: E402
from app.backend.core import realtime  # noqa: E402
from tests.test_realtime import FakeRedis  # noqa: E402

from app.schemas.events import VEHICLE_ENTER, VEHICLE_EXIT, PERSON_ENTER, PERSON_EXIT  # noqa: E402


def _make_app():
    """构建仅含 events 路由的最小 FastAPI 应用 (避免完整 lifespan 启动后台任务)."""
    app = FastAPI()
    app.include_router(events_api.router)
    return app


async def _noop_evaluate():
    """告警评估占位 (避免触发真实 Redis/规则加载)."""
    return None


def _setup_device(fake: FakeRedis, device_id: str, count_only: str = ""):
    """在 FakeRedis 中写入设备 count_only 配置."""
    fake._hset(f"sc:device:{device_id}", "count_only", count_only)


def test_post_event_returns_ok():
    """POST /api/events 接收 VehicleEnter → 返回 201 + status ok."""
    fake = FakeRedis()
    _setup_device(fake, "CAM-B", "")
    app = _make_app()
    with patch.object(realtime, "get_redis", return_value=fake), \
         patch.object(events_api, "evaluate", _noop_evaluate):  # noqa: PLC3002 屏蔽告警任务
        client = TestClient(app)
        resp = client.post("/api/events", json={
            "device_id": "CAM-B",
            "event_type": VEHICLE_ENTER,
            "occurred_at": "2026-08-09T12:00:00+00:00",
        })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "ok"
    assert body["event_type"] == VEHICLE_ENTER


def test_post_invalid_event_type_rejected():
    """POST 非法 event_type → 400."""
    fake = FakeRedis()
    app = _make_app()
    with patch.object(realtime, "get_redis", return_value=fake):
        client = TestClient(app)
        resp = client.post("/api/events", json={
            "device_id": "CAM-B",
            "event_type": "VehicleCrash",
            "occurred_at": "2026-08-09T12:00:00+00:00",
        })
    assert resp.status_code == 400


def test_get_events_returns_history():
    """POST 多个事件后, GET /api/events 返回历史 (倒序, 最近在前)."""
    fake = FakeRedis()
    _setup_device(fake, "CAM-B", "")
    app = _make_app()
    with patch.object(realtime, "get_redis", return_value=fake), \
         patch.object(events_api, "evaluate", _noop_evaluate):
        client = TestClient(app)
        client.post("/api/events", json={"device_id": "CAM-B", "event_type": VEHICLE_ENTER, "occurred_at": "2026-08-09T12:00:00+00:00"})
        client.post("/api/events", json={"device_id": "CAM-B", "event_type": VEHICLE_EXIT, "occurred_at": "2026-08-09T12:00:01+00:00"})
        client.post("/api/events", json={"device_id": "CAM-B", "event_type": PERSON_ENTER, "occurred_at": "2026-08-09T12:00:02+00:00"})
        resp = client.get("/api/events?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    # lpush 头插, 最近 (PersonEnter) 在前
    assert data[0]["event_type"] == PERSON_ENTER
    assert data[1]["event_type"] == VEHICLE_EXIT
    assert data[2]["event_type"] == VEHICLE_ENTER
    assert all("device_id" in e and "occurred_at" in e for e in data)


def test_get_events_limit_param():
    """GET /api/events?limit=2 限制返回 2 条."""
    fake = FakeRedis()
    _setup_device(fake, "CAM-B", "")
    app = _make_app()
    with patch.object(realtime, "get_redis", return_value=fake), \
         patch.object(events_api, "evaluate", _noop_evaluate):
        client = TestClient(app)
        for et in [VEHICLE_ENTER, VEHICLE_EXIT, PERSON_ENTER, PERSON_EXIT]:
            client.post("/api/events", json={"device_id": "CAM-B", "event_type": et, "occurred_at": "2026-08-09T12:00:00+00:00"})
        resp = client.get("/api/events?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_events_empty_when_none():
    """无事件时 GET /api/events 返回空列表."""
    fake = FakeRedis()
    app = _make_app()
    with patch.object(realtime, "get_redis", return_value=fake):
        client = TestClient(app)
        resp = client.get("/api/events")
    assert resp.status_code == 200
    assert resp.json() == []


def test_count_only_vehicle_post_updates_current():
    """车流单向车道 (count_only='enter') POST VehicleEnter → current_vehicles 累加 (累计进入数)."""
    fake = FakeRedis()
    _setup_device(fake, "CAM-V", "enter")
    app = _make_app()
    with patch.object(realtime, "get_redis", return_value=fake), \
         patch.object(events_api, "evaluate", _noop_evaluate):
        client = TestClient(app)
        client.post("/api/events", json={"device_id": "CAM-V", "event_type": VEHICLE_ENTER, "occurred_at": "2026-08-09T12:00:00+00:00"})
        client.post("/api/events", json={"device_id": "CAM-V", "event_type": VEHICLE_ENTER, "occurred_at": "2026-08-09T12:00:01+00:00"})
        client.post("/api/events", json={"device_id": "CAM-V", "event_type": VEHICLE_ENTER, "occurred_at": "2026-08-09T12:00:02+00:00"})
    cur = fake.data.get(realtime._CUR_KEY, {})
    # 单向车道: current_vehicles 累加 (累计进入数, 该场景预期语义)
    assert cur.get("current_vehicles") == "3", f"单向车道 current_vehicles 应累加: {cur}"
    # 事件历史应记录 3 条
    assert len(fake.data.get(realtime._EVENTS_KEY, [])) == 3


def test_get_events_limit_validation():
    """GET /api/events?limit=0 → 422 (ge=1 约束)."""
    fake = FakeRedis()
    app = _make_app()
    with patch.object(realtime, "get_redis", return_value=fake):
        client = TestClient(app)
        resp = client.get("/api/events?limit=0")
    assert resp.status_code == 422


def _run_all():
    tests = [
        test_post_event_returns_ok,
        test_post_invalid_event_type_rejected,
        test_get_events_returns_history,
        test_get_events_limit_param,
        test_get_events_empty_when_none,
        test_count_only_vehicle_post_updates_current,
        test_get_events_limit_validation,
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
