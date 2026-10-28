"""时间口径回归：真实观测间隔、遮挡、失联和跨线时间。"""
import unittest
from datetime import datetime

from app.ai.counter import LineCrossingCounter
from app.ai.traj_state import RawTrack, TrajectoryState


def raw(tid, x, name='car'):
    return RawTrack(tid, 2, name, [x - 10, 490, x + 10, 510], .9)


def make_counter():
    counter = LineCrossingCounter(line=((.5, .1), (.5, .9)), anchor=(.9, .5))
    counter.set_frame_size(1000, 1000)
    counter._apply_business_rules = lambda: None
    counter.anti_jitter = False
    return counter


class CountingTimeTest(unittest.TestCase):
    def test_same_motion_at_various_frame_rates(self):
        for fps in (2, 3, 6, 8, 12, 25):
            with self.subTest(fps=fps):
                state, counter = TrajectoryState(), make_counter()
                events = []
                for i in range(int(1.6 * fps) + 1):
                    stamp = 1 + i / fps
                    result = state.apply([raw(1, 400 + 100 * (stamp - 1))], current_time=stamp)
                    events.extend(counter.process_tracks(result, current_time=stamp,
                                                         current_wall_time=1000 + stamp))
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].event_type, 'VehicleEnter')

    def test_occlusion_uses_target_interval_not_last_frame(self):
        state, counter = TrajectoryState(), make_counter()
        for stamp, detections in ((10, [raw(1, 490)]), (10.9, []), (11, [raw(1, 510)])):
            result = state.apply(detections, current_time=stamp)
            events = counter.process_tracks(result, current_time=stamp, current_wall_time=1000 + stamp)
        self.assertEqual(events, [])  # 20px / 1s < 50px/s
        self.assertEqual(counter.pop_reject_stats().get('夹角过滤'), 1)

    def test_event_time_is_interpolated_crossing_not_confirmation(self):
        state, counter = TrajectoryState(), make_counter()
        result = state.apply([raw(1, 480)], current_time=10)
        counter.process_tracks(result, current_time=10, current_wall_time=3599.8)
        result = state.apply([raw(1, 520)], current_time=10.4)
        events = counter.process_tracks(result, current_time=10.4, current_wall_time=3600.2)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(datetime.fromisoformat(events[0].timestamp).timestamp(), 3600.0, places=5)
        self.assertEqual(events[0].event_type, 'VehicleEnter')

    def test_long_gap_does_not_cross_or_reuse_old_history(self):
        state, counter = TrajectoryState(), make_counter()
        result = state.apply([raw(1, 450)], current_time=1)
        counter.process_tracks(result, current_time=1, current_wall_time=1001)
        result = state.apply([raw(1, 550)], current_time=12)
        self.assertEqual(len(result.tracks[0].history), 1)
        self.assertEqual(result.expired_track_ids, ['1'])
        self.assertEqual(counter.process_tracks(result, current_time=12, current_wall_time=1012), [])

    def test_gap_and_stitch_use_elapsed_time(self):
        state = TrajectoryState()
        for stamp, x in ((1, 400), (1.1, 410), (1.2, 420)):
            state.apply([raw(1, x)], current_time=stamp)
        state.apply([], current_time=1.3)
        stitched = state.apply([raw(2, 440)], current_time=1.4)
        self.assertEqual(len(stitched.tracks[0].history), 4)
        self.assertEqual(stitched.tracks[0].history_times, [1, 1.1, 1.2, 1.4])
        self.assertEqual(stitched.stitched_ids, [('1', '2')])
        other = TrajectoryState()
        for stamp, x in ((1, 400), (1.1, 410), (1.2, 420)):
            other.apply([raw(1, x)], current_time=stamp)
        other.apply([], current_time=1.21)
        no_stitch = other.apply([raw(2, 480)], current_time=1.8)
        self.assertEqual(len(no_stitch.tracks[0].history), 1)

    def test_stitch_does_not_steal_target_visible_in_same_frame(self):
        state = TrajectoryState()
        for stamp, x in ((1, 400), (1.1, 410), (1.2, 420)):
            state.apply([raw(1, x)], current_time=stamp)
        state.apply([], current_time=1.3)
        result = state.apply([raw(2, 440), raw(1, 440)], current_time=1.4)
        self.assertEqual(result.stitched_ids, [])
        self.assertEqual([len(t.history) for t in result.tracks], [1, 4])

    def test_stitch_transfers_count_dedup(self):
        state, counter = TrajectoryState(), make_counter()
        for stamp, x in ((1, 450), (1.1, 460), (1.2, 470)):
            result = state.apply([raw(1, x)], current_time=stamp)
            counter.process_tracks(result, current_time=stamp, current_wall_time=1000 + stamp)
        counter.state_manager.mark_counted('1', 'enter', 1.2, [500, 500])
        result = state.apply([], current_time=1.3)
        counter.process_tracks(result, current_time=1.3, current_wall_time=1001.3)
        result = state.apply([raw(2, 490)], current_time=1.4)
        counter.process_tracks(result, current_time=1.4, current_wall_time=1001.4)
        self.assertNotIn('1|enter', counter.state_manager.counted_tracks)
        self.assertIn('2|enter', counter.state_manager.counted_tracks)

    def test_out_of_order_rejected_and_reset_counter_clock(self):
        state, counter = TrajectoryState(), make_counter()
        result = state.apply([raw(1, 400)], current_time=5)
        counter.process_tracks(result, current_time=5, current_wall_time=1005)
        with self.assertRaises(ValueError):
            state.apply([], current_time=5)
        with self.assertRaises(ValueError):
            counter.process_tracks(result, current_time=4, current_wall_time=1004)
        counter.reset()
        counter.process_tracks(result, current_time=1, current_wall_time=1001)


if __name__ == '__main__':
    unittest.main()
