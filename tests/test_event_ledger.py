"""MySQL 账本和 Redis 物化集成测试，只连接显式指定的隔离测试服务。"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from app.ai.event_outbox import EventOutbox
from app.backend.api import events as events_api
from app.backend.core import realtime
from app.backend.core.event_ledger import EventLedger, LedgerConflict
from app.common import mysql_client


async def eventually(check, timeout=8):
    async def loop():
        while not await check():
            await asyncio.sleep(.05)
    await asyncio.wait_for(loop(), timeout)


@pytest_asyncio.fixture
async def services(monkeypatch):
    port, redis_url = os.environ.get('TEST_MYSQL_PORT'), os.environ.get('TEST_REDIS_URL')
    if not port or not redis_url:
        pytest.skip('需要隔离的 TEST_MYSQL_PORT 和 TEST_REDIS_URL')
    settings = mysql_client.settings
    monkeypatch.setattr(settings, 'mysql_host', '127.0.0.1')
    monkeypatch.setattr(settings, 'mysql_port', int(port))
    monkeypatch.setattr(settings, 'mysql_user', 'root')
    monkeypatch.setattr(settings, 'mysql_password', 'test-only-pw')
    monkeypatch.setattr(settings, 'mysql_database', 'dt_plan_test')
    monkeypatch.setattr(settings, 'mysql_enabled', True)
    monkeypatch.setattr(settings, 'event_ledger_enabled', True)
    prefix = 'ledger_' + uuid4().hex
    monkeypatch.setattr(settings, 'redis_prefix', prefix)
    monkeypatch.setattr(realtime, '_CUR_KEY', f'{prefix}:realtime:current')
    monkeypatch.setattr(realtime, '_DEVICES_KEY', f'{prefix}:devices:active')
    monkeypatch.setattr(realtime, '_EVENTS_KEY', f'{prefix}:events')
    redis = redis_async.from_url(redis_url, decode_responses=True)
    monkeypatch.setattr(realtime, 'get_redis', lambda: redis)
    ledger = EventLedger()
    monkeypatch.setattr(events_api, 'event_ledger', ledger)
    await ledger.ensure_ready()
    yield ledger, redis, prefix
    await ledger.stop()
    pool = await mysql_client.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute('DELETE FROM traffic_event_ledger WHERE device_id=%s', (prefix,))
    await mysql_client.close_mysql()
    keys = [key async for key in redis.scan_iter(f'{prefix}:*')]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


@pytest.mark.asyncio
async def test_persist_conflict_and_crash_after_redis_application(services):
    ledger, redis, device = services
    event = {'event_id': uuid4().hex, 'device_id': device, 'event_type': 'VehicleEnter',
             'occurred_at': datetime.now(timezone.utc).isoformat()}
    evidence = {'track_id': '1', 'stream_epoch': 'epoch'}
    outcomes = await asyncio.gather(*(ledger.persist(event, evidence) for _ in range(12)))
    assert sum(outcomes) == 1
    with pytest.raises(LedgerConflict):
        await ledger.persist(event, {'track_id': 'other'})
    # 模拟 Redis 已应用而 MySQL 的 materialized_at 尚未提交时进程退出。
    assert await realtime.apply_event(event['event_type'], device,
                                      datetime.fromisoformat(event['occurred_at']).astimezone(ZoneInfo('Asia/Shanghai')), event['event_id'])
    ledger.start()
    async def done():
        return (await ledger.snapshot())['pending'] == 0
    await eventually(done)
    assert await redis.hget(realtime._CUR_KEY, 'current_vehicles') == '1'


@pytest.mark.asyncio
async def test_redis_outage_keeps_order_and_retries_materialization(services):
    ledger, redis, device = services
    enter = {'event_id': uuid4().hex, 'device_id': device, 'event_type': 'VehicleEnter',
             'occurred_at': datetime.now(timezone.utc).isoformat()}
    leave = {**enter, 'event_id': uuid4().hex, 'event_type': 'VehicleExit'}
    await ledger.persist(enter, {})
    await ledger.persist(leave, {})
    actual_apply = realtime.apply_event
    calls = []
    async def fail_once(*args):
        calls.append(args[3])
        if len(calls) == 1:
            raise OSError('injected Redis outage')
        return await actual_apply(*args)
    with patch.object(realtime, 'apply_event', side_effect=fail_once):
        ledger.start()
        async def done():
            return len(calls) >= 3 and (await ledger.snapshot())['pending'] == 0
        await eventually(done)
    assert calls == [enter['event_id'], enter['event_id'], leave['event_id']]
    assert await redis.hget(realtime._CUR_KEY, 'current_vehicles') == '0'


@pytest.mark.asyncio
async def test_mysql_unavailable_is_not_acknowledged(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(mysql_client.settings, 'mysql_enabled', True)
    monkeypatch.setattr(mysql_client.settings, 'event_ledger_enabled', True)
    event = events_api.EventV2In(event_id='db-failure', device_id='cam',
                                 event_type='VehicleEnter', occurred_at=datetime.now(timezone.utc))
    with patch.object(events_api.event_ledger, 'persist', AsyncMock(side_effect=OSError('MySQL unavailable'))):
        with pytest.raises(HTTPException) as error:
            await events_api.receive_event_v2(event)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_v2_utc_time_is_bucketed_in_business_timezone(services):
    from datetime import date
    ledger, redis, device = services
    event = {'event_id': uuid4().hex, 'device_id': device, 'event_type': 'VehicleEnter',
             'occurred_at': '2026-01-01T16:30:00+00:00'}
    await ledger.persist(event, {})
    ledger.start()
    async def done():
        return (await ledger.snapshot())['pending'] == 0
    await eventually(done)
    assert await redis.hget(realtime._daily_key(date(2026, 1, 2)), 'today_vehicle_in') == '1'
    assert await redis.hget(realtime._daily_key(date(2026, 1, 1)), 'today_vehicle_in') is None


@pytest.mark.asyncio
async def test_v2_rejects_naive_timestamp(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(mysql_client.settings, 'mysql_enabled', True)
    monkeypatch.setattr(mysql_client.settings, 'event_ledger_enabled', True)
    event = events_api.EventV2In(event_id='naive', device_id='cam',
                                 event_type='VehicleEnter', occurred_at=datetime(2026, 1, 1))
    with pytest.raises(HTTPException) as error:
        await events_api.receive_event_v2(event)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_v2_lost_ack_keeps_one_durable_event_and_retries(services, tmp_path, monkeypatch):
    ledger, redis, device = services
    monkeypatch.setattr(mysql_client.settings, 'event_delivery_protocol', 'journal_v2')
    posts = []
    async def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, json=await events_api.event_capabilities())
        model = events_api.EventV2In(**json.loads(request.content))
        ack = await events_api.receive_event_v2(model)
        posts.append(ack)
        if len(posts) == 1:
            raise httpx.ReadTimeout('lost after mysql commit', request=request)
        return httpx.Response(201, json=ack)
    box = EventOutbox(str(tmp_path / 'events.sqlite'), 'http://backend', httpx.MockTransport(handle))
    await box.start()
    event = {'event_id': uuid4().hex, 'device_id': device, 'event_type': 'PersonEnter',
             'occurred_at': datetime.now(timezone.utc).isoformat()}
    try:
        await box.enqueue_many([event], evidence=[{'track_id': '33'}])
        async def delivered():
            return len(posts) >= 2 and (await box.snapshot())['pending'] == 0
        await eventually(delivered)
        assert [item['created'] for item in posts] == [True, False]
        assert all(item['persisted'] for item in posts)
        ledger.start()
        async def materialized():
            return (await ledger.snapshot())['pending'] == 0
        await eventually(materialized)
        assert await redis.hget(realtime._CUR_KEY, 'current_persons') == '1'
        pool = await mysql_client.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('SELECT evidence,materialized_at FROM traffic_event_ledger WHERE event_id=%s', (event['event_id'],))
                row = await cur.fetchone()
        assert json.loads(row[0]) == {'track_id': '33'} and row[1] is not None
        other = {**event, 'event_id': uuid4().hex}
        await ledger.persist(other, {})  # deliberately absent from this producer's SQLite
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'reconcile_event_journal.py'
        env = {**os.environ, 'MYSQL_HOST': '127.0.0.1', 'MYSQL_PORT': os.environ['TEST_MYSQL_PORT'],
               'MYSQL_USER': 'root', 'MYSQL_PASSWORD': 'test-only-pw', 'MYSQL_DATABASE': 'dt_plan_test',
               'REDIS_URL': os.environ['TEST_REDIS_URL']}
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), '--sqlite', str(box.path),
                                                       '--redis-prefix', device, '--device', device, env=env,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        report = json.loads(stdout)
        assert report['counts']['ledger_match'] == 1 and report['counts']['redis_match'] == 1
        assert report['scope'] == 'both_directions_for_device'
        assert report['counts']['backend_only_candidate'] == 1
    finally:
        await box.stop()
