"""Regression checks for the log evidence extraction contract."""
import tempfile
import unittest
import gzip
import json
import subprocess
import sys
from pathlib import Path

from scripts.audit_pull_count_log import analyze


class LogAuditTest(unittest.TestCase):
    def test_extracts_incidents_windows_events_without_exporting_stream_url(self):
        stream = "http://example.invalid/private/stream?token=secret"
        lines = [
            f"2026-10-28 16:26:01.057 | INFO | app.ai.pipeline:_run_once:346 - [cam-1] 管道启动: {stream}",
            f"2026-10-28 16:26:01.598 | WARNING | app.ai.stream:_open_reader:541 - ffmpeg 解码不可用, 该路回退 opencv ({stream}), 300s 后重试: Unrecognized option 'fps_mode'",
            "2026-10-28 16:26:02.000 | INFO | app.ai.pipeline:_process_frame:315 - [cam-1] 越线事件 PersonEnter track=7 class=person dir=outer_to_inner 交点=(42,50) conf=0.87",
            "2026-10-28 16:26:02.005 | INFO | app.backend.api.events:receive_event:39 - 事件 PersonEnter <- cam-1",
            "2026-10-28 16:26:32.000 | INFO | app.ai.pipeline:_report_stats:616 - [cam-1] 运行统计(30s): 处理fps=4.00 grab帧=750 采样帧=150 丢帧=30(20%) 事件=1 | 拒绝: 历史不足=4",
            "2026-10-28 16:26:33.000 | ERROR | app.backend.core.wvp_client:start_play:146 - WVP 点播失败 123/456: {'code': 480, 'msg': 'Temporarily Unavailable'}",
            "2026-10-28 16:26:34.000 | WARNING | app.ai.pipeline:_watchdog_loop:475 - [cam-1] AI 处理异常: {'state': 'low_fps', 'fps_30s': 4.0}",
            "2026-10-28 16:26:35.000 | DEBUG | app.ai.pipeline:_report_congestion:697 - [cam-1] 拥挤上报 roi_vehicles=1 roi_persons=3 flow=1.5/min person_flow=2.5/min -> HTTP 200",
            "not a structured log line",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = analyze(path)

        self.assertEqual((report["total_lines"], report["parsed_lines"]), (9, 8))
        self.assertEqual(report["unmatched_line_numbers"], [9])
        self.assertEqual(report["statistics"]["weighted_drop_pct"], 20.0)
        self.assertEqual(report["devices"]["cam-1"]["rejections"], {"历史不足": 4})
        self.assertEqual(report["events"]["ai"], {"PersonEnter": 1})
        self.assertEqual(report["events"]["backend"], {"PersonEnter": 1})
        self.assertEqual(report["events"]["ai_records"][0]["cross_point"], [42.0, 50.0])
        self.assertEqual(report["wvp_failures"]["123/456"]["codes"], {"480": 1})
        self.assertEqual(len(report["warning_error_records"]), 3)
        self.assertEqual(report["watchdog_states"], {"low_fps": 1})
        congestion = report["congestion_reports"]["records"][0]
        self.assertEqual((congestion["roi_persons"], congestion["person_flow"], congestion["http_status"]), (3, 2.5, 200))
        self.assertEqual(report["stream_events"][0]["url_sha256"], report["stream_events"][1]["url_sha256"])
        self.assertNotIn(stream, str(report))

    def test_empty_file_has_explicit_empty_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.log"
            path.touch()
            report = analyze(path)
        self.assertEqual(report["total_lines"], 0)
        self.assertEqual(report["statistics"]["windows"], 0)
        self.assertIsNone(report["statistics"]["weighted_drop_pct"])
        self.assertEqual(report["devices"], {})

    def test_cli_writes_a_reproducible_compressed_json_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.log"
            output = Path(directory) / "report.json.gz"
            path.touch()
            script = Path(__file__).resolve().parents[1] / "scripts" / "audit_pull_count_log.py"
            command = [sys.executable, str(script), str(path), "--output", str(output)]
            subprocess.run(command, check=True)
            first = output.read_bytes()
            subprocess.run(command, check=True)
            self.assertEqual(first, output.read_bytes())
            self.assertEqual(json.loads(gzip.decompress(first))["total_lines"], 0)


if __name__ == "__main__":
    unittest.main()
