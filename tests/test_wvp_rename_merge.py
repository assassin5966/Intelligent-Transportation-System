"""WVP 改名与未知状态保护的合并回归，使用隔离 Redis。"""
import asyncio
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from app.backend.api import devices
from app.backend.core import wvp_sync


class FakeWvp:
    enabled = True

    def __init__(self, play=None):
        self.play = play or AsyncMock(return_value={'flv': 'http://example/stream'})

    async def list_devices(self):
        return [{'deviceId': 'gb', 'online': True}]

    async def list_channels(self, device_id):
        return [{'channelId': 'ch', 'status': True, 'name': 'new-name'}]

    async def start_play(self, device_id, channel_id):
        return await self.play(device_id, channel_id)

    def select_stream_url(self, play):
        return play['flv']


@pytest_asyncio.fixture
async def context(monkeypatch):
    url = os.environ.get('TEST_REDIS_URL')
    if not url:
        pytest.skip('需要独立 TEST_REDIS_URL')
    redis = redis_async.from_url(url, decode_responses=True)
    prefix = 'test_wvp_rename_' + uuid4().hex + ':'
    key = prefix + 'old-name'
    await redis.hset(key, mapping={'id': 'old-name', 'name': 'old-name',
        'gb_device_id': 'gb', 'gb_channel_id': 'ch',
        'line_coords': '.5,.1,.5,.9', 'status': 'online'})
    monkeypatch.setattr(wvp_sync, '_DEVICE_KEY_PREFIX', prefix)
    monkeypatch.setattr(wvp_sync, '_sync_lock', asyncio.Lock())
    monkeypatch.setattr(wvp_sync, '_play_failures', {})
    monkeypatch.setattr(wvp_sync, 'get_redis', lambda: redis)
    monkeypatch.setattr(wvp_sync, 'to_internal', lambda value: value)
    monkeypatch.setattr(wvp_sync.device_info, 'cache_size', lambda: 0)
    monkeypatch.setattr(wvp_sync, '_get_ai_running_devices', AsyncMock(return_value={'old-name'}))
    stop, start = AsyncMock(return_value=True), AsyncMock(return_value=True)
    monkeypatch.setattr(wvp_sync, '_stop_ai_pipeline', stop)
    monkeypatch.setattr(wvp_sync, '_start_ai_pipeline', start)
    yield redis, prefix, stop, start
    keys = [key async for key in redis.scan_iter(prefix + '*')]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


@pytest.mark.asyncio
async def test_authoritative_rename_preserves_config_and_starts_once(context, monkeypatch):
    redis, prefix, stop, start = context
    monkeypatch.setattr(wvp_sync, 'get_wvp_client', lambda: FakeWvp())
    result = await wvp_sync.sync_once()
    assert result['renamed'] == 1
    assert not await redis.exists(prefix + 'old-name')
    data = await redis.hgetall(prefix + 'new-name')
    assert (data['id'], data['name'], data['line_coords'], data['ai_status']) == (
        'new-name', 'new-name', '.5,.1,.5,.9', 'waiting_first_frame')
    stop.assert_awaited_once_with('old-name')
    start.assert_awaited_once()
    assert start.await_args.args[0]['id'] == 'new-name'


@pytest.mark.asyncio
async def test_unknown_ai_state_does_not_rename_or_stop(context, monkeypatch):
    redis, prefix, stop, start = context
    monkeypatch.setattr(wvp_sync, 'get_wvp_client', lambda: FakeWvp())
    monkeypatch.setattr(wvp_sync, '_get_ai_running_devices', AsyncMock(return_value=None))
    result = await wvp_sync.sync_once()
    assert result['ai_devices_unknown'] and result['renamed'] == 0
    assert (await redis.hgetall(prefix + 'old-name'))['wvp_sync_status'] == 'ai_list_unknown'
    assert not await redis.exists(prefix + 'new-name')
    stop.assert_not_awaited()
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_play_failure_keeps_original_pipeline(context, monkeypatch):
    redis, prefix, stop, start = context
    monkeypatch.setattr(wvp_sync, 'get_wvp_client', lambda: FakeWvp(AsyncMock(return_value=None)))
    result = await wvp_sync.sync_once()
    assert result['renamed'] == 0
    assert await redis.exists(prefix + 'old-name')
    assert not await redis.exists(prefix + 'new-name')
    stop.assert_not_awaited()
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_occupied_new_key_is_never_overwritten(context, monkeypatch):
    redis, prefix, stop, start = context
    await redis.hset(prefix + 'new-name', mapping={'id': 'new-name', 'name': 'other'})
    monkeypatch.setattr(wvp_sync, 'get_wvp_client', lambda: FakeWvp())
    result = await wvp_sync.sync_once()
    assert result['renamed'] == 0
    assert (await redis.hgetall(prefix + 'new-name'))['name'] == 'other'
    assert await redis.exists(prefix + 'old-name')
    stop.assert_not_awaited()
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_config_change_aborts_migration_and_restores_old(context, monkeypatch):
    redis, prefix, stop, start = context
    async def play(*args):
        await redis.hset(prefix + 'old-name', 'line_coords', '.4,.1,.4,.9')
        return {'flv': 'http://example/stream'}
    monkeypatch.setattr(wvp_sync, 'get_wvp_client', lambda: FakeWvp(AsyncMock(side_effect=play)))
    result = await wvp_sync.sync_once()
    assert result['renamed'] == 0
    assert (await redis.hgetall(prefix + 'old-name'))['line_coords'] == '.4,.1,.4,.9'
    assert not await redis.exists(prefix + 'new-name')
    stop.assert_awaited_once_with('old-name')
    start.assert_awaited_once()
    assert start.await_args.args[0]['line_coords'] == '.4,.1,.4,.9'


@pytest.mark.asyncio
async def test_unconfirmed_stop_prevents_rename(context, monkeypatch):
    redis, prefix, stop, start = context
    stop.return_value = False
    monkeypatch.setattr(wvp_sync, 'get_wvp_client', lambda: FakeWvp())
    result = await wvp_sync.sync_once()
    assert result['renamed'] == 0
    assert await redis.exists(prefix + 'old-name')
    assert not await redis.exists(prefix + 'new-name')
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_alias_uses_longest_unambiguous_match(context, monkeypatch):
    redis, prefix, _stop, _start = context
    monkeypatch.setattr(devices, '_DEVICE_KEY_PREFIX', prefix)
    await redis.hset(prefix + 'road', mapping={'id': 'road'})
    await redis.hset(prefix + 'road cross', mapping={'id': 'road cross'})
    assert await devices._resolve_device_key(redis, 'roadcross123') == prefix + 'road cross'
    await redis.hset(prefix + 'roadcross', mapping={'id': 'roadcross'})
    # Two equal-length normalized matches must not select an arbitrary camera.
    assert await devices._resolve_device_key(redis, 'roadcross123') == prefix + 'roadcross123'
