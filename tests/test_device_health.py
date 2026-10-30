"""设备业务状态、失败关闭及已配置设备的自动恢复。"""
import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from app.ai.model_pool import ModelPool
from app.backend.api.devices import DeviceHeartbeat, heartbeat
from app.backend.core import wvp_sync
from app.backend.core.wvp_client import WVPClient, WVPError


class MemoryRedis:
    def __init__(self, values):
        self.values = values

    async def scan_iter(self, pattern):
        for key in list(self.values):
            yield key

    async def hgetall(self, key):
        return dict(self.values.get(key, {}))

    async def exists(self, key):
        return key in self.values

    async def hset(self, key, name=None, value=None, mapping=None):
        self.values.setdefault(key, {}).update(mapping or {name: value})

    async def delete(self, key):
        self.values.pop(key, None)


class FakeWvp:
    enabled = True

    async def list_devices(self):
        return [{"deviceId": "gb", "online": True}]

    async def list_channels(self, device_id):
        return [{"channelId": "ch", "status": True, "name": "cam"}]

    async def start_play(self, device_id, channel_id):
        return {"flv": "http://example/stream"}

    def select_stream_url(self, result):
        return result["flv"]


class DeviceHealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_sync_calls_are_serialized(self):
        running = 0
        peak = 0
        async def work():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(.02)
            running -= 1
            return {'done': True}
        with patch.object(wvp_sync, '_sync_lock', asyncio.Lock()), \
             patch.object(wvp_sync, '_sync_once', side_effect=work):
            await asyncio.gather(wvp_sync.sync_once(), wvp_sync.sync_once())
        self.assertEqual(peak, 1)
    async def test_partial_wvp_failure_does_not_stop_affected_device(self):
        class PartialWvp(FakeWvp):
            async def list_devices(self):
                return [{"deviceId": "bad", "online": True}, {"deviceId": "gb", "online": True}]

            async def list_channels(self, device_id):
                if device_id == "bad":
                    raise TimeoutError("unavailable")
                return []  # authoritative removal on the successful device

        prefix = wvp_sync._DEVICE_KEY_PREFIX
        redis = MemoryRedis({
            prefix + "badcam": {"id": "badcam", "name": "badcam", "gb_device_id": "bad", "gb_channel_id": "ch", "status": "online"},
            prefix + "goodcam": {"id": "goodcam", "name": "goodcam", "gb_device_id": "gb", "gb_channel_id": "ch", "status": "online"},
        })
        with patch.object(wvp_sync, "get_redis", return_value=redis), \
             patch.object(wvp_sync, "get_wvp_client", return_value=PartialWvp()), \
             patch.object(wvp_sync.device_info, "cache_size", return_value=0), \
             patch.object(wvp_sync, "_get_ai_running_devices", AsyncMock(return_value={"badcam", "goodcam"})), \
             patch.object(wvp_sync, "_stop_ai_pipeline", AsyncMock()) as stop:
            result = await wvp_sync.sync_once()
        stop.assert_awaited_once_with("goodcam")
        self.assertEqual(redis.values[prefix + "badcam"]["status"], "online")
        self.assertEqual(redis.values[prefix + "goodcam"]["status"], "offline")
        self.assertEqual(result["failed_gb_devices"], ["bad"])

    async def test_unknown_ai_list_does_not_restart_or_remove_pipelines(self):
        key = wvp_sync._DEVICE_KEY_PREFIX + "cam"
        original = {"id": "cam", "name": "cam", "gb_device_id": "gb", "gb_channel_id": "ch", "line_coords": "0,0,1,1", "status": "online"}
        redis = MemoryRedis({key: dict(original)})
        with patch.object(wvp_sync, "get_redis", return_value=redis), \
             patch.object(wvp_sync, "get_wvp_client", return_value=FakeWvp()), \
             patch.object(wvp_sync.device_info, "cache_size", return_value=0), \
             patch.object(wvp_sync, "_get_ai_running_devices", AsyncMock(return_value=None)), \
             patch.object(wvp_sync, "_stop_ai_pipeline", AsyncMock()) as stop, \
             patch.object(wvp_sync, "_start_ai_pipeline", AsyncMock()) as start:
            result = await wvp_sync.sync_once()
        stop.assert_not_awaited()
        start.assert_not_awaited()
        self.assertEqual({k: v for k, v in redis.values[key].items() if k != 'wvp_sync_status'}, original)
        self.assertEqual(redis.values[key]['wvp_sync_status'], 'ai_list_unknown')
        self.assertTrue(result["ai_devices_unknown"])

    async def test_existing_name_mismatch_preserves_gb_identity(self):
        key = wvp_sync._DEVICE_KEY_PREFIX + 'cam'
        redis = MemoryRedis({key: {'id': 'cam', 'name': 'old-name', 'gb_device_id': 'gb',
                                   'gb_channel_id': 'ch', 'line_coords': '.5,.1,.5,.9', 'status': 'online'}})
        with patch.object(wvp_sync, 'get_redis', return_value=redis), \
             patch.object(wvp_sync, 'get_wvp_client', return_value=FakeWvp()), \
             patch.object(wvp_sync.device_info, 'cache_size', return_value=1), \
             patch.object(wvp_sync.device_info, 'is_registered', return_value=False), \
             patch.object(wvp_sync, '_get_ai_running_devices', AsyncMock(return_value={'cam'})), \
             patch.object(wvp_sync, '_stop_ai_pipeline', AsyncMock()) as stop:
            await wvp_sync.sync_once()
        stop.assert_not_awaited()
        self.assertIn(key, redis.values)
        self.assertEqual(redis.values[key]['wvp_sync_status'], 'registration_unconfirmed')

    async def test_ai_list_failure_is_unknown_and_empty_success_is_authoritative(self):
        response = unittest.mock.Mock(status_code=503)
        client = AsyncMock()
        client.__aenter__.return_value.get.return_value = response
        with patch.object(wvp_sync.httpx, "AsyncClient", return_value=client):
            self.assertIsNone(await wvp_sync._get_ai_running_devices())
            response.status_code = 200
            response.json.return_value = []
            self.assertEqual(await wvp_sync._get_ai_running_devices(), set())
            response.json.return_value = {"bad": "shape"}
            self.assertIsNone(await wvp_sync._get_ai_running_devices())

    async def test_incomplete_or_malformed_wvp_pages_are_not_authoritative(self):
        client = WVPClient()
        client.enabled = True
        first = {'data': {'total': 2, 'list': [{'channelId': 'one'}]}}
        second = {'data': {'total': 2, 'list': []}}
        with patch.object(client, '_request', AsyncMock(side_effect=[first, second])):
            with self.assertRaises(WVPError):
                await client.list_channels('gb')
        with patch.object(client, '_request', AsyncMock(return_value={'data': {}})):
            with self.assertRaises(WVPError):
                await client.list_channels('gb')
        with patch.object(client, '_request', AsyncMock(return_value={'data': {'total': 0, 'list': []}})):
            self.assertEqual(await client.list_channels('gb'), [])

    async def test_string_zero_is_offline_and_unknown_status_is_not_removal(self):
        self.assertIs(wvp_sync._online_flag('0'), False)
        self.assertIsNone(wvp_sync._online_flag(None))
        class UnknownStatus(FakeWvp):
            async def list_channels(self, device_id):
                return [{'channelId': 'ch', 'name': 'cam'}]
        key = wvp_sync._DEVICE_KEY_PREFIX + 'cam'
        redis = MemoryRedis({key: {'id': 'cam', 'name': 'cam', 'gb_device_id': 'gb',
                                   'gb_channel_id': 'ch', 'status': 'online'}})
        with patch.object(wvp_sync, 'get_redis', return_value=redis), \
             patch.object(wvp_sync, 'get_wvp_client', return_value=UnknownStatus()), \
             patch.object(wvp_sync.device_info, 'cache_size', return_value=0), \
             patch.object(wvp_sync, '_get_ai_running_devices', AsyncMock(return_value={'cam'})), \
             patch.object(wvp_sync, '_stop_ai_pipeline', AsyncMock()) as stop:
            await wvp_sync.sync_once()
        stop.assert_not_awaited()
        self.assertEqual(redis.values[key]['status'], 'online')

    async def test_play_failure_is_bounded_and_success_clears_backoff(self):
        wvp = FakeWvp()
        play = AsyncMock(side_effect=[None, {'flv': 'http://example/stream'}])
        with patch.object(wvp, 'start_play', play), \
             patch.object(wvp_sync, '_play_failures', {}), \
             patch.object(wvp_sync, 'to_internal', side_effect=lambda value: value), \
             patch.object(wvp_sync.time, 'monotonic', return_value=100):
            self.assertEqual(await wvp_sync._play_with_backoff(wvp, 'gb', 'ch'), (None, True))
            self.assertEqual(await wvp_sync._play_with_backoff(wvp, 'gb', 'ch'), (None, False))
            self.assertEqual(play.await_count, 1)
            with patch.object(wvp_sync.time, 'monotonic', return_value=1000):
                url, attempted = await wvp_sync._play_with_backoff(wvp, 'gb', 'ch')
            self.assertTrue(attempted)
            self.assertEqual(url, 'http://example/stream')
            self.assertEqual(wvp_sync._play_failures, {})

    async def test_gpu_dual_engine_is_opt_in_and_fixed_slots_stay_per_card(self):
        pool = ModelPool()
        with patch('app.ai.model_pool.settings.infer_mode', 'gpu_batch'), \
             patch('app.ai.model_pool.settings.infer_engines_per_gpu', 2), \
             patch('app.ai.model_pool.settings.infer_slots_per_gpu', 8), \
             patch('app.ai.model_pool.settings.infer_gpu_devices', '0,0,1'), \
             patch.object(pool, '_probe_cuda', return_value=(True, {'count': 2})), \
             patch.object(pool, '_start_engines', AsyncMock()) as start:
            await pool.initialize()
        specs, mode, slots, _ = start.await_args.args
        self.assertEqual([item[0] for item in specs], ['gpu0-0', 'gpu0-1', 'gpu1-0', 'gpu1-1'])
        self.assertEqual([item[1] for item in specs], ['cuda:0', 'cuda:0', 'cuda:1', 'cuda:1'])
        self.assertEqual((mode, slots), ('gpu_batch', 4))
    async def test_heartbeat_has_separate_processing_state_and_old_body_compatible(self):
        from app.backend.api import devices
        key = devices._DEVICE_KEY_PREFIX + "cam"
        redis = MemoryRedis({key: {"id": "cam"}})
        with patch.object(devices, "get_redis", return_value=redis):
            await heartbeat("cam", DeviceHeartbeat(ai_status="no_recent_inference"))
            self.assertEqual(redis.values[key]["status"], "online")
            self.assertEqual(redis.values[key]["ai_status"], "no_recent_inference")
            await heartbeat("cam")
            self.assertEqual(redis.values[key]["ai_status"], "no_recent_inference")

    async def test_synced_device_with_line_retries_start_and_marks_readiness(self):
        key = wvp_sync._DEVICE_KEY_PREFIX + "cam"
        redis = MemoryRedis({key: {"id": "cam", "name": "cam", "gb_device_id": "gb",
                                   "gb_channel_id": "ch", "line_coords": "0,0,1,1", "status": "synced"}})
        with patch.object(wvp_sync, "get_redis", return_value=redis), \
             patch.object(wvp_sync, "get_wvp_client", return_value=FakeWvp()), \
             patch.object(wvp_sync.device_info, "cache_size", return_value=0), \
             patch.object(wvp_sync, "_get_ai_running_devices", AsyncMock(return_value=set())), \
             patch.object(wvp_sync, "_stop_ai_pipeline", AsyncMock()), \
             patch.object(wvp_sync, "_start_ai_pipeline", AsyncMock(return_value=True)) as start, \
             patch.object(wvp_sync, "to_internal", side_effect=lambda value: value):
            result = await wvp_sync.sync_once()
        start.assert_awaited_once()
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(redis.values[key]["ai_status"], "waiting_first_frame")

    async def test_explicit_gpu_failure_does_not_enable_legacy(self):
        pool = ModelPool()
        with patch("app.ai.model_pool.settings.infer_mode", "gpu_batch"), \
             patch.object(pool, "_probe_cuda", return_value=(False, "no cuda")):
            await pool.initialize()
        self.assertEqual(pool.mode, "unavailable")
        self.assertIsNone(pool.acquire("cam"))

    async def test_health_reports_effective_next_connection_config(self):
        from app.ai import service
        snapshot = {"mode": "gpu_batch", "reason": "ok", "engines": []}
        with patch.object(service.model_pool, "snapshot", return_value=snapshot), \
             patch.object(service.event_outbox, "snapshot", AsyncMock(return_value={"pending": 0})), \
             patch.object(service, "get_rule", return_value=960), \
             patch.object(service, "_decode_max_fps", return_value=8), \
             patch.object(service, "_decode_target_short_side", return_value=540):
            result = await service.health()
        self.assertEqual(result["effective_config"]["decode_short_side_next_connection"], 540)
        self.assertEqual(result["effective_config"]["decode_max_fps_next_connection"], 8)
