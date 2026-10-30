"""计数配置和多段 ROI 的业务回归。"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.ai.counter import LineCrossingCounter
from app.ai.traj_state import RawTrack, TrajectoryState
from app.backend.api import devices
from app.backend.core import wvp_sync
from app.common.counting_config import parse_counting_config, validate_counting_geometry
from tests.test_device_health import MemoryRedis, FakeWvp


U_ROI = [[.1, .1], [.9, .1], [.9, .9], [.65, .9],
         [.65, .3], [.25, .3], [.25, .9], [.1, .9]]


class CountingGeometryTest(unittest.TestCase):
    def test_concave_roi_retains_both_valid_segments(self):
        counter = LineCrossingCounter(line=((0, .5), (1, .5)), anchor=(.5, .9))
        counter.set_frame_size(1000, 1000)
        counter.set_roi(U_ROI)
        intervals = counter.crossing_detector.clip_intervals
        self.assertEqual(len(intervals), 2)
        for actual, expected in zip(intervals, [(.1, .25), (.65, .9)]):
            self.assertAlmostEqual(actual[0], expected[0])
            self.assertAlmostEqual(actual[1], expected[1])
        for x, expected in ((150, True), (450, False), (750, True), (-1, False), (100, True)):
            self.assertEqual(counter._in_segment([x, 500]), expected)

    def test_roi_boundary_overlapping_line_is_valid(self):
        counter = LineCrossingCounter(line=((0, .2), (1, .2)), anchor=(.5, .9))
        counter.set_frame_size(1000, 1000)
        counter.set_roi([[.2, .2], [.8, .2], [.8, .8], [.2, .8]])
        self.assertTrue(counter._in_segment([300, 200]))
        self.assertFalse(counter._in_segment([100, 200]))
        self.assertTrue(counter._point_in_roi([200, 300]))

    def test_crossing_narrow_roi_between_two_outside_observations(self):
        counter = LineCrossingCounter(line=((.5, .1), (.5, .9)), anchor=(.9, .5))
        counter.set_frame_size(1000, 1000)
        counter._apply_business_rules = lambda: None
        counter.anti_jitter = False
        counter.set_roi([[.49, .3], [.51, .3], [.51, .7], [.49, .7]])
        state = TrajectoryState()
        for stamp, x in ((1, 470), (1.4, 530)):
            track = RawTrack(1, 2, 'car', [x - 10, 490, x + 10, 510], .9)
            result = state.apply([track], current_time=stamp)
            events = counter.process_tracks(result, current_time=stamp, current_wall_time=1000 + stamp)
        self.assertEqual([event.event_type for event in events], ['VehicleEnter'])

    def test_invalid_geometry_rejected_and_blank_roi_allowed(self):
        line = [[.5, .1], [.5, .9]]
        self.assertIsNone(validate_counting_geometry(line, None, None)[2])
        for args in [
            ('nan,0,1,1', None, None), ('0,0,0,0', None, None),
            ('0,0,1,1', '0.5,0.5', None), ('0,0,1,1', None, '0,0,1'),
            ('0,0,1,1', None, '0,0,1,1,1,0,0,1'),
            ('0,0,1,0', None, '.2,.2,.8,.2,.8,.8,.2,.8'),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_counting_config(*args)


class ConfigApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_enable_keeps_previous_configuration(self):
        key = devices._DEVICE_KEY_PREFIX + 'cam'
        old = {'id': 'cam', 'line_coords': '.5,.1,.5,.9', 'status': 'online'}
        redis = MemoryRedis({key: dict(old)})
        with patch.object(devices, 'get_redis', return_value=redis):
            with self.assertRaises(HTTPException) as error:
                await devices.enable_device('cam', devices.DeviceEnableIn(line_coords='bad'))
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(redis.values[key], old)

    async def test_older_client_cannot_save_an_unpreviewed_multi_segment_roi(self):
        key = devices._DEVICE_KEY_PREFIX + 'cam'
        original = {'id': 'cam', 'status': 'online'}
        redis = MemoryRedis({key: dict(original)})
        roi = ','.join(str(value) for point in U_ROI for value in point)
        body = devices.DeviceEnableIn(line_coords='0,.5,1,.5', anchor_coords='.5,.9', roi_coords=roi)
        with patch.object(devices, 'get_redis', return_value=redis):
            with self.assertRaises(HTTPException) as error:
                await devices.enable_device('cam', body)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(redis.values[key], original)

    async def test_updated_client_can_save_multi_segment_roi(self):
        from unittest.mock import AsyncMock
        key = devices._DEVICE_KEY_PREFIX + 'cam'
        redis = MemoryRedis({key: {'id': 'cam', 'stream_url': 'test://stream', 'status': 'registered'}})
        roi = ','.join(str(value) for point in U_ROI for value in point)
        body = devices.DeviceEnableIn(line_coords='0,.5,1,.5', anchor_coords='.5,.9',
                                      roi_coords=roi, allow_multi_roi=True)
        with patch.object(devices, 'get_redis', return_value=redis), \
             patch.object(devices.settings, 'wvp_enabled', False), \
             patch.object(devices, '_forward_to_ai', AsyncMock(return_value=True)), \
             patch.object(wvp_sync, '_start_ai_pipeline', AsyncMock(return_value=True)):
            result = await devices.enable_device('cam', body)
        self.assertEqual(result['status'], 'online')
        self.assertEqual(redis.values[key]['roi_coords'], roi)

    async def test_historical_invalid_config_is_not_started(self):
        from unittest.mock import AsyncMock
        key = wvp_sync._DEVICE_KEY_PREFIX + 'cam'
        redis = MemoryRedis({key: {'id': 'cam', 'name': 'cam', 'gb_device_id': 'gb',
                                   'gb_channel_id': 'ch', 'line_coords': 'bad', 'status': 'synced'}})
        with patch.object(wvp_sync, 'get_redis', return_value=redis), \
             patch.object(wvp_sync, 'get_wvp_client', return_value=FakeWvp()), \
             patch.object(wvp_sync.device_info, 'cache_size', return_value=0), \
             patch.object(wvp_sync, '_get_ai_running_devices', AsyncMock(return_value=set())), \
             patch.object(wvp_sync, '_start_ai_pipeline', AsyncMock()) as start:
            await wvp_sync.sync_once()
        start.assert_not_awaited()
        self.assertEqual(redis.values[key]['ai_status'], 'config_invalid')
