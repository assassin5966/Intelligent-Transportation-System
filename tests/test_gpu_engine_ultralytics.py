"""Optional integration checks using the installed Ultralytics tracker implementation."""
import importlib.util
import unittest
from pathlib import Path

import numpy as np

HAS_ULTRALYTICS = importlib.util.find_spec('ultralytics') is not None


@unittest.skipUnless(HAS_ULTRALYTICS, 'Ultralytics is not installed')
class UltralyticsTrackerTests(unittest.TestCase):
    def test_construction_does_not_reset_other_tracker_ids(self):
        from app.ai.gpu_engine import _build_tracker, _load_tracker_args
        from ultralytics.engine.results import Boxes
        from ultralytics.trackers.basetrack import BaseTrack

        cfg = Path(__file__).resolve().parents[1] / 'configs' / 'bytetrack.yaml'
        for kind in ('bytetrack', 'botsort'):
            with self.subTest(kind=kind):
                args = _load_tracker_args(str(cfg))
                args.tracker_type = kind
                frame = np.zeros((100, 100, 3), np.uint8)
                old = Boxes(np.array([[10, 10, 30, 30, .95, 2]], dtype=np.float32), (100, 100))
                both = Boxes(np.array([[10, 10, 30, 30, .95, 2],
                                       [65, 65, 85, 85, .95, 2]], dtype=np.float32), (100, 100))
                first = _build_tracker(args)
                row = first.update(old, frame)
                self.assertEqual(int(row[0][4]), 1)
                BaseTrack._count = 91
                other = _build_tracker(args)
                self.assertEqual(BaseTrack._count, 91)
                self.assertEqual(int(other.update(old, frame)[0][4]), 1)
                first.update(both, frame)
                rows = first.update(both, frame)
                self.assertEqual({int(row[4]) for row in rows}, {1, 2})
                self.assertEqual(BaseTrack._count, 91)
    def test_empty_frames_advance_and_expire_lost_tracks(self):
        from app.ai.gpu_engine import GpuInferEngine, _Req, _build_tracker, _load_tracker_args
        from types import SimpleNamespace
        from ultralytics.engine.results import Boxes

        cfg = Path(__file__).resolve().parents[1] / 'configs' / 'bytetrack.yaml'
        for kind in ('bytetrack', 'botsort'):
            with self.subTest(kind=kind):
                args = _load_tracker_args(str(cfg))
                args.tracker_type = kind
                args.track_buffer = 2
                frame = np.zeros((100, 100, 3), np.uint8)
                det = Boxes(np.array([[10, 10, 30, 30, .95, 2]], dtype=np.float32), (100, 100))
                tracker = _build_tracker(args)
                self.assertEqual(int(tracker.update(det, frame)[0][4]), 1)
                engine = GpuInferEngine('test', device='cpu', model_path='', slots=1,
                    batch_timeout_s=.04, queue_max_batches=1, tracker_cfg=str(cfg), imgsz=64, half=False)
                engine._det_boxed = True
                req = _Req('a', frame, (1, 1))
                for _ in range(6):
                    self.assertEqual(engine._tracks_for(req, SimpleNamespace(boxes=None), tracker), [])
                self.assertEqual(tracker.frame_id, 7)
                self.assertFalse(tracker.lost_stracks)
                tracker.update(det, frame)
                rows = tracker.update(det, frame)
                self.assertEqual(int(rows[0][4]), 2)


if __name__ == '__main__':
    unittest.main()
