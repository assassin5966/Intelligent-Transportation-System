"""持久上报、响应丢失与后端原子去重的回归；Redis 用独立测试实例。"""
import asyncio
import os
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from app.ai.event_outbox import EventOutbox
from app.backend.core import realtime
from app.backend.core import archive_scheduler as archive


@pytest_asyncio.fixture
async def redis_client(monkeypatch):
    url = os.environ.get('TEST_REDIS_URL')
    if not url:
        pytest.skip('需要独立 TEST_REDIS_URL，不使用生产 Redis')
    client = redis_async.from_url(url, decode_responses=True)
    prefix = f'test_event_delivery_{uuid4().hex}'
    monkeypatch.setattr(realtime.settings, 'redis_prefix', prefix)
    monkeypatch.setattr(realtime, '_CUR_KEY', f'{prefix}:realtime:current')
    monkeypatch.setattr(realtime, '_DEVICES_KEY', f'{prefix}:devices:active')
    monkeypatch.setattr(realtime, '_EVENTS_KEY', f'{prefix}:events')
    monkeypatch.setattr(archive, 'get_redis', lambda: client)
    monkeypatch.setattr(archive, '_HOURLY_PREFIX', f'{prefix}:hourly:')
    monkeypatch.setattr(archive, '_MARKER_KEY', f'{prefix}:archive:last_hour')
    monkeypatch.setattr(archive, '_PRUNE_MARKER_KEY', f'{prefix}:archive:last_prune')
    monkeypatch.setattr(archive, '_DIRTY_KEY', f'{prefix}:archive:dirty')
    monkeypatch.setattr(archive, '_QUARANTINE_KEY', f'{prefix}:archive:quarantine')
    monkeypatch.setattr(archive, '_DIRTY_DEVICES_PREFIX', f'{prefix}:archive:dirty_devices:')
    monkeypatch.setattr(realtime, 'get_redis', lambda: client)
    yield client
    keys = [key async for key in client.scan_iter(f'{prefix}:*')]
    if keys:
        await client.delete(*keys)
    await client.aclose()


async def wait_for(predicate, timeout=5):
    async def run():
        while not predicate():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(run(), timeout)


@pytest.mark.asyncio
async def test_atomic_duplicate_conflict_and_invalid_storage(redis_client):
    when = datetime(2026, 10, 28, 12, 0)
    results = await asyncio.gather(*(realtime.apply_event('VehicleEnter', 'cam1', when, 'evt1') for _ in range(40)))
    assert sum(results) == 1
    assert await redis_client.hget(realtime._CUR_KEY, 'current_vehicles') == '1'
    assert await redis_client.hget(realtime._daily_key(when.date()), 'today_vehicle_in') == '1'
    assert await redis_client.llen(realtime._EVENTS_KEY) == 1
    with pytest.raises(realtime.EventIdConflict):
        await realtime.apply_event('VehicleExit', 'cam1', when, 'evt1')
    await redis_client.set(realtime._device_key('bad'), 'wrong-type')
    with pytest.raises(redis_async.ResponseError):
        await realtime.apply_event('VehicleEnter', 'bad', when, 'evt2')
    assert await redis_client.hget(realtime._CUR_KEY, 'current_vehicles') == '1'
    assert await redis_client.hget(f'{realtime.settings.redis_prefix}:event:dedup', 'evt2') is None
    # 旧调用者无 event_id，维持逐请求累计。
    assert await realtime.apply_event('PersonEnter', 'cam1', when)
    assert await realtime.apply_event('PersonEnter', 'cam1', when)
    assert await redis_client.hget(realtime._CUR_KEY, 'current_persons') == '2'


@pytest.mark.asyncio
async def test_lost_response_restart_and_dedup(redis_client, tmp_path):
    seen = []
    async def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'idempotency': 'permanent-v1'})
        import json
        payload = json.loads(request.content)
        applied = await realtime.apply_event(payload['event_type'], payload['device_id'],
                                              datetime.fromisoformat(payload['occurred_at']), payload['event_id'])
        seen.append(applied)
        if len(seen) == 1:
            raise httpx.ReadTimeout('response lost after commit', request=request)
        return httpx.Response(201, json={'event_id': payload['event_id'], 'applied': applied})
    db = str(tmp_path / 'outbox.db')
    box = EventOutbox(db, 'http://backend', httpx.MockTransport(handle))
    await box.start()
    await box.enqueue({'event_id': 'retry-1', 'device_id': 'cam', 'event_type': 'VehicleEnter',
                       'occurred_at': '2026-10-28T12:00:00'})
    await wait_for(lambda: len(seen) == 1)
    await box.stop()
    resumed = EventOutbox(db, 'http://backend', httpx.MockTransport(handle))
    await resumed.start()
    try:
        await wait_for(lambda: len(seen) == 2)
        await wait_for(lambda: resumed._pending == 0)
        assert seen == [True, False]
        assert await redis_client.hget(realtime._CUR_KEY, 'current_vehicles') == '1'
        assert (await resumed.snapshot())['pending'] == 0
    finally:
        await resumed.stop()


@pytest.mark.asyncio
async def test_old_backend_never_receives_post(tmp_path):
    posts = []
    def handle(request):
        if request.method == 'POST':
            posts.append(request)
        return httpx.Response(404)
    box = EventOutbox(str(tmp_path / 'outbox.db'), 'http://backend', httpx.MockTransport(handle))
    await box.start()
    try:
        await box.enqueue({'event_id': 'x', 'device_id': 'cam', 'event_type': 'VehicleEnter',
                           'occurred_at': '2026-10-28T12:00:00'})
        await wait_for(lambda: box._last_error is not None)
        assert not posts
        assert (await box.snapshot())['pending'] == 1
    finally:
        await box.stop()


@pytest.mark.asyncio
async def test_storage_failure_holds_event_and_reports_error(tmp_path, monkeypatch):
    box = EventOutbox(str(tmp_path / 'outbox.db'), 'http://backend')
    await asyncio.to_thread(box._initialize)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(box, '_insert_many', fail)
    task = asyncio.create_task(box.enqueue({'event_id': 'x', 'device_id': 'cam'}))
    await wait_for(lambda: box._storage_error is not None)
    assert not task.done()
    assert 'disk full' in (await box.snapshot())['storage_error']
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_batch_transaction_and_device_order(tmp_path):
    box = EventOutbox(str(tmp_path / 'outbox.db'), 'http://backend')
    await asyncio.to_thread(box._initialize)
    def payload(eid, device='cam', kind='VehicleEnter'):
        return {'event_id': eid, 'device_id': device, 'event_type': kind,
                'occurred_at': '2026-10-28T12:00:00'}
    await box.enqueue_many([payload('a'), payload('b', kind='VehicleExit')])
    with pytest.raises(ValueError):
        await box.enqueue_many([payload('c'), payload('a', kind='PersonEnter')])
    assert await asyncio.to_thread(box._count) == 2
    await asyncio.to_thread(box._retry, 'a', 2)
    assert await asyncio.to_thread(box._next) is None
    await box.enqueue(payload('d', device='other'))
    assert (await asyncio.to_thread(box._next))[0] == 'd'
    await asyncio.to_thread(box._done, 'd')
    await asyncio.to_thread(box._done, 'a')
    assert (await asyncio.to_thread(box._next))[0] == 'b'


@pytest.mark.asyncio
async def test_delivered_journal_is_retained_and_conflicts_are_rejected(tmp_path):
    import sqlite3
    box = EventOutbox(str(tmp_path / 'outbox.db'), 'http://backend')
    await asyncio.to_thread(box._initialize)
    payload = {'event_id': 'saved', 'device_id': 'cam', 'event_type': 'VehicleEnter',
               'occurred_at': '2026-10-28T12:00:00'}
    await box.enqueue_many([payload], evidence=[{'track_id': '42'}])
    await asyncio.to_thread(box._done, 'saved', True)
    await box.enqueue_many([payload], evidence=[{'track_id': '42'}])
    assert await asyncio.to_thread(box._count) == 0
    with pytest.raises(ValueError):
        await box.enqueue_many([payload], evidence=[{'track_id': 'different'}])
    with sqlite3.connect(box.path) as conn:
        row = conn.execute('SELECT evidence,delivered,backend_applied FROM event_journal WHERE event_id=?', ('saved',)).fetchone()
    assert '42' in row[0] and row[1] is not None and row[2] == 1


@pytest.mark.asyncio
async def test_coverage_gap_survives_restart_and_closes_on_recovery(tmp_path):
    box = EventOutbox(str(tmp_path / 'outbox.db'), 'http://backend')
    await asyncio.to_thread(box._initialize)
    await box.record_coverage('cam', 'epoch1', 'processing', 100)
    # 无机会记录停止：下一次不同 epoch 的首个检查应保守补出缺口。
    reopened = EventOutbox(str(box.path), 'http://backend')
    await reopened.record_coverage('cam', 'epoch2', 'waiting_first_frame', 130)
    gaps = await reopened.coverage_gaps('cam')
    assert gaps[0]['start_unix'] == 100 and gaps[0]['end_unix'] is None
    await reopened.record_coverage('cam', 'epoch2', 'processing', 135)
    gaps = await reopened.coverage_gaps('cam')
    assert gaps[0]['end_unix'] == 135 and gaps[0]['reason'] == 'waiting_first_frame'


@pytest.mark.asyncio
async def test_late_archive_version_race_and_marker(redis_client, monkeypatch):
    from app.common import mysql_client
    hour = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    hour_str = hour.strftime('%Y%m%d%H')
    device_key = realtime._hourly_key('cam', hour)
    global_key = realtime._hourly_all_key(hour)
    for key in (device_key, global_key):
        await redis_client.hset(key, 'vehicle_in', 1)
        await redis_client.expire(key, 86400)
    assert await realtime.apply_event('VehicleEnter', 'cam', hour, 'late1')
    assert await redis_client.hget(archive._DIRTY_KEY, hour_str) == '1'
    writes = []
    async def upsert(rows):
        writes.append(rows)
        if len(writes) == 1:
            assert await realtime.apply_event('VehicleEnter', 'cam', hour, 'late2')
    async def noop(*args):
        return None
    monkeypatch.setattr(mysql_client, 'upsert_hourly', upsert)
    monkeypatch.setattr(mysql_client, 'ensure_table', noop)
    monkeypatch.setattr(mysql_client, 'prune_old', noop)
    # Marker has already moved beyond this hour, so only dirty repair can reach it.
    await redis_client.set(archive._MARKER_KEY, datetime.now().strftime('%Y%m%d%H'))
    await redis_client.set(archive._PRUNE_MARKER_KEY, datetime.now().date().isoformat())
    await archive._archive_once()
    assert writes[0][0][3] == 2
    assert await redis_client.hget(archive._DIRTY_KEY, hour_str) == '2'
    await archive._archive_once()
    assert writes[1][0][3] == 3
    assert await redis_client.hget(archive._DIRTY_KEY, hour_str) is None
    assert await redis_client.exists(f'{archive._DIRTY_DEVICES_PREFIX}{hour_str}') == 0


@pytest.mark.asyncio
async def test_late_missing_or_expired_bucket_is_quarantined(redis_client, monkeypatch):
    from app.common import mysql_client
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    hour = now - timedelta(hours=2)
    hour_str = hour.strftime('%Y%m%d%H')
    # Missing per-device bucket means prior history is unknowable, including a new device.
    await redis_client.hset(realtime._hourly_all_key(hour), 'vehicle_in', 10)
    await redis_client.expire(realtime._hourly_all_key(hour), 3600)
    assert await realtime.apply_event('VehicleEnter', 'cam', hour, 'missing')
    assert await redis_client.hexists(archive._QUARANTINE_KEY, hour_str)
    assert await redis_client.hget(f'{realtime.settings.redis_prefix}:archive:quarantine_events', 'missing')
    assert not await redis_client.exists(realtime._hourly_key('cam', hour))
    assert await redis_client.hget(realtime._hourly_all_key(hour), 'vehicle_in') == '10'
    writes = []
    async def upsert(rows):
        writes.append(rows)
    monkeypatch.setattr(mysql_client, 'upsert_hourly', upsert)
    await archive._archive_hour(redis_client, hour)
    assert not writes
    expired = now - timedelta(days=realtime.settings.hourly_retention_days + 1)
    await redis_client.hset(archive._DIRTY_KEY, expired.strftime('%Y%m%d%H'), 1)
    await archive._archive_dirty(redis_client, now)
    assert await redis_client.hexists(archive._QUARANTINE_KEY, expired.strftime('%Y%m%d%H'))
    assert not writes


@pytest.mark.asyncio
async def test_archive_failure_retains_dirty_and_retries(redis_client, monkeypatch):
    from app.common import mysql_client
    hour = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    for key in (realtime._hourly_key('cam', hour), realtime._hourly_all_key(hour)):
        await redis_client.hset(key, 'vehicle_in', 1)
        await redis_client.expire(key, 3600)
    await realtime.apply_event('VehicleEnter', 'cam', hour, 'retry-archive')
    async def fail(rows):
        raise RuntimeError('MySQL unavailable')
    monkeypatch.setattr(mysql_client, 'upsert_hourly', fail)
    with pytest.raises(RuntimeError):
        await archive._archive_dirty(redis_client, datetime.now())
    assert await redis_client.hget(archive._DIRTY_KEY, hour.strftime('%Y%m%d%H')) == '1'
    writes = []
    async def upsert(rows):
        writes.append(rows)
    monkeypatch.setattr(mysql_client, 'upsert_hourly', upsert)
    await archive._archive_dirty(redis_client, datetime.now())
    assert writes[0][0][3] == 2
    assert await redis_client.hget(archive._DIRTY_KEY, hour.strftime('%Y%m%d%H')) is None
