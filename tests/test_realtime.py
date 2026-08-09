"""后端实时状态管理 (realtime.py) 单元测试.

覆盖:
  - P0-1: count_only 单向摄像头不更新 current_* (防单调漂移), 仍更新今日累计/区间
  - 双向摄像头正常更新 current_*
  - P1-5: 越线事件历史持久化 (Redis List) + list_events 读取
  - 负值钳位

使用 FakeRedis (内存模拟), 不依赖真实 Redis.

可独立运行:
    python -m pytest tests/test_realtime.py -v
或:
    python tests/test_realtime.py
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.backend.core import realtime  # noqa: E402
from app.schemas.events import VEHICLE_ENTER, VEHICLE_EXIT, PERSON_ENTER, PERSON_EXIT  # noqa: E402


# ===================== FakeRedis (内存模拟, 仅实现 realtime.py 用到的方法) =====================

class FakePipeline:
    def __init__(self, redis):
        self._r = redis
        self._cmds = []

    def hincrby(self, key, field, amount):
        self._cmds.append(("hincrby", key, field, amount))

    def hget(self, key, field):
        self._cmds.append(("hget", key, field))

    def hgetall(self, key):
        self._cmds.append(("hgetall", key))

    def scard(self, key):
        self._cmds.append(("scard", key))

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping is not None:
            self._cmds.append(("hset_map", key, mapping))
        else:
            self._cmds.append(("hset", key, field, value))

    def sadd(self, key, member):
        self._cmds.append(("sadd", key, member))

    def expire(self, key, seconds):
        self._cmds.append(("expire", key, seconds))

    def lpush(self, key, value):
        self._cmds.append(("lpush", key, value))

    def ltrim(self, key, start, stop):
        self._cmds.append(("ltrim", key, start, stop))

    async def execute(self):
        results = []
        for cmd in self._cmds:
            op = cmd[0]
            if op == "hincrby":
                _, key, field, amount = cmd
                results.append(self._r._hincrby(key, field, amount))
            elif op == "hget":
                _, key, field = cmd
                results.append(await self._r.hget(key, field))
            elif op == "hgetall":
                _, key = cmd
                results.append(await self._r.hgetall(key))
            elif op == "scard":
                _, key = cmd
                results.append(await self._r.scard(key))
            elif op == "hset":
                _, key, field, value = cmd
                results.append(self._r._hset(key, field, value))
            elif op == "hset_map":
                _, key, mapping = cmd
                results.append(self._r._hset_map(key, mapping))
            elif op == "sadd":
                _, key, member = cmd
                results.append(self._r._sadd(key, member))
            elif op == "expire":
                results.append(True)
            elif op == "lpush":
                _, key, value = cmd
                results.append(self._r._lpush(key, value))
            elif op == "ltrim":
                _, key, start, stop = cmd
                results.append(self._r._ltrim(key, start, stop))
        return results


class FakeRedis:
    def __init__(self):
        self.data = {}  # key -> dict | set | list | str | int

    def pipeline(self):
        return FakePipeline(self)

    async def hget(self, key, field):
        v = self.data.get(key, {})
        if isinstance(v, dict):
            return v.get(field)
        return None

    async def hgetall(self, key):
        v = self.data.get(key, {})
        return dict(v) if isinstance(v, dict) else {}

    def _hset(self, key, field, value):
        if not isinstance(self.data.get(key), dict):
            self.data[key] = {}
        self.data[key][field] = value
        return 1

    def _hset_map(self, key, mapping):
        if not isinstance(self.data.get(key), dict):
            self.data[key] = {}
        self.data[key].update(mapping)
        return len(mapping)

    async def hset(self, key, field=None, value=None, mapping=None):
        if mapping is not None:
            return self._hset_map(key, mapping)
        return self._hset(key, field, value)

    def _hincrby(self, key, field, amount):
        if not isinstance(self.data.get(key), dict):
            self.data[key] = {}
        cur = int(self.data[key].get(field, 0) or 0)
        new = cur + amount
        self.data[key][field] = str(new)
        return new

    async def hincrby(self, key, field, amount):
        return self._hincrby(key, field, amount)

    def _sadd(self, key, member):
        if not isinstance(self.data.get(key), set):
            self.data[key] = set()
        before = len(self.data[key])
        self.data[key].add(member)
        return len(self.data[key]) - before

    async def sadd(self, key, *members):
        n = 0
        for m in members:
            n += self._sadd(key, m)
        return n

    async def scard(self, key):
        v = self.data.get(key, set())
        return len(v) if isinstance(v, set) else 0

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def _lpush(self, key, value):
        if not isinstance(self.data.get(key), list):
            self.data[key] = []
        self.data[key].insert(0, value)
        return len(self.data[key])

    def _ltrim(self, key, start, stop):
        v = self.data.get(key, [])
        if isinstance(v, list):
            if stop >= 0:
                self.data[key] = v[start:stop + 1]
            else:
                self.data[key] = v[start:] if stop == -1 else v[start:stop]
        return True

    async def lrange(self, key, start, stop):
        v = self.data.get(key, [])
        if not isinstance(v, list):
            return []
        if stop >= 0:
            return v[start:stop + 1]
        return v[start:]


def _patch_redis(fake: FakeRedis):
    """patch realtime.get_redis 返回 fake 实例."""
    return patch.object(realtime, "get_redis", return_value=fake)


# ===================== current_* 更新语义 =====================

def test_count_only_vehicle_updates_current():
    """车流单向车道 (count_only='enter'): VehicleEnter 仍更新 current_vehicles (累计进入数).

    单向车道无 Exit 事件, current_vehicles 即累计进入数, 为该场景预期语义.
    """
    fake = FakeRedis()
    # 设备配置: count_only='enter' (车流单向车道)
    fake._hset(f"sc:device:CAM-V", "count_only", "enter")
    occurred = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    with _patch_redis(fake):
        asyncio.run(realtime.apply_event(VEHICLE_ENTER, "CAM-V", occurred))
        asyncio.run(realtime.apply_event(VEHICLE_ENTER, "CAM-V", occurred))

    cur = fake.data.get(realtime._CUR_KEY, {})
    # current_vehicles 应累加 (单向车道累计进入数)
    assert cur["current_vehicles"] == "2", f"车流单向车道 current_vehicles 应累加: {cur}"
    # today_vehicle_in 也应累加
    today_key = f"sc:realtime:daily:20260809"
    assert fake.data[today_key]["today_vehicle_in"] == "2"
    # 区间计数也应累加
    interval_keys = [k for k in fake.data if ":realtime:interval:" in k]
    assert interval_keys, "应有区间计数"


def test_bidirectional_device_updates_current():
    """双向摄像头 (车流双向): VehicleEnter +1, VehicleExit -1, 自然对冲."""
    fake = FakeRedis()
    fake._hset(f"sc:device:CAM-B", "count_only", "")
    occurred = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    with _patch_redis(fake):
        asyncio.run(realtime.apply_event(VEHICLE_ENTER, "CAM-B", occurred))
        asyncio.run(realtime.apply_event(VEHICLE_ENTER, "CAM-B", occurred))
        asyncio.run(realtime.apply_event(VEHICLE_EXIT, "CAM-B", occurred))

    cur = fake.data.get(realtime._CUR_KEY, {})
    # 2 进 1 出 → current_vehicles = 1
    assert cur["current_vehicles"] == "1", f"双向应正常对冲 current: {cur}"


def test_bidirectional_person_updates_current():
    """人流恒为双向计数: PersonEnter +1, PersonExit -1, 自然对冲."""
    fake = FakeRedis()
    # 人流设备无 count_only (恒双向)
    fake._hset(f"sc:device:CAM-P", "count_only", "")
    occurred = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    with _patch_redis(fake):
        asyncio.run(realtime.apply_event(PERSON_ENTER, "CAM-P", occurred))
        asyncio.run(realtime.apply_event(PERSON_ENTER, "CAM-P", occurred))
        asyncio.run(realtime.apply_event(PERSON_EXIT, "CAM-P", occurred))

    cur = fake.data.get(realtime._CUR_KEY, {})
    # 2 进 1 出 → current_persons = 1
    assert cur["current_persons"] == "1", f"人流双向应正常对冲 current: {cur}"
    today_key = f"sc:realtime:daily:20260809"
    assert fake.data[today_key]["today_person_in"] == "2"
    assert fake.data[today_key]["today_person_out"] == "1"


# ===================== P1-5: 事件历史持久化 =====================

def test_event_log_persisted():
    """apply_event 后, 事件记录写入 Redis List (lpush)."""
    fake = FakeRedis()
    fake._hset(f"sc:device:CAM-B", "count_only", "")
    occurred = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    with _patch_redis(fake):
        asyncio.run(realtime.apply_event(VEHICLE_ENTER, "CAM-B", occurred))
        asyncio.run(realtime.apply_event(VEHICLE_EXIT, "CAM-B", occurred))

    events_list = fake.data.get(realtime._EVENTS_KEY, [])
    assert len(events_list) == 2, f"应有 2 条事件历史, 实际 {len(events_list)}"
    # lpush 头插, 最近事件在前
    first = json.loads(events_list[0])
    assert first["event_type"] == VEHICLE_EXIT
    assert first["device_id"] == "CAM-B"
    second = json.loads(events_list[1])
    assert second["event_type"] == VEHICLE_ENTER


def test_list_events_reads_history():
    """list_events 从 Redis List 读取并反序列化."""
    fake = FakeRedis()
    fake._lpush(realtime._EVENTS_KEY, json.dumps({"device_id": "D1", "event_type": VEHICLE_ENTER}))
    fake._lpush(realtime._EVENTS_KEY, json.dumps({"device_id": "D2", "event_type": PERSON_EXIT}))
    with _patch_redis(fake):
        result = asyncio.run(realtime.list_events(10))
    assert len(result) == 2
    assert result[0]["device_id"] == "D2"  # lpush 头插, 最近在前
    assert result[1]["device_id"] == "D1"


def test_list_events_empty():
    """无事件历史时返回空列表."""
    fake = FakeRedis()
    with _patch_redis(fake):
        result = asyncio.run(realtime.list_events(10))
    assert result == []


def test_list_events_limit():
    """limit 限制返回条数."""
    fake = FakeRedis()
    for i in range(5):
        fake._lpush(realtime._EVENTS_KEY, json.dumps({"device_id": f"D{i}", "event_type": VEHICLE_ENTER}))
    with _patch_redis(fake):
        result = asyncio.run(realtime.list_events(3))
    assert len(result) == 3


# ===================== 负值钳位 =====================

def test_negative_current_clamped():
    """current_vehicles 不会为负 (异常/重启漂移修正)."""
    fake = FakeRedis()
    fake._hset(f"sc:device:CAM-B", "count_only", "")
    occurred = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    with _patch_redis(fake):
        # 仅 Exit 无 Enter → current_vehicles 应被钳位为 0
        asyncio.run(realtime.apply_event(VEHICLE_EXIT, "CAM-B", occurred))
    cur = fake.data.get(realtime._CUR_KEY, {})
    assert int(cur.get("current_vehicles", 0)) >= 0


# ===================== 自运行入口 =====================

def _run_all():
    tests = [
        test_count_only_vehicle_updates_current,
        test_bidirectional_device_updates_current,
        test_bidirectional_person_updates_current,
        test_event_log_persisted,
        test_list_events_reads_history,
        test_list_events_empty,
        test_list_events_limit,
        test_negative_current_clamped,
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
