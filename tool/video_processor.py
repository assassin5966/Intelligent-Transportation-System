#!/usr/bin/env python3
"""
视频文件离线处理器

将视频文件逐帧送入算法流水线（检测→跟踪→越线计数→统计→预测→告警），
输出标注视频、事件JSON、统计CSV和告警报告。

用法:
    python3 video_processor.py --video <视频路径> [选项]

选项:
    --video       视频文件路径（必填）
    --output      输出目录（默认: ./output）
    --camera_id   摄像头ID（默认: CAM001）
    --frame_skip  跳帧间隔，1=每帧都处理，5=每5帧处理1帧（默认: 5）
    --no-annotated 不生成标注视频（加速处理）
    --line        计数线坐标，格式: x1,y1,x2,y2（归一化0-1，默认: 0.1,0.4,0.9,0.4）
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.ai.counter import LineCrossingCounter


class Statistics:
    def __init__(self):
        self.current_vehicles = 0
        self.current_persons = 0
        self.today_vehicle_enter = 0
        self.today_vehicle_exit = 0
        self.today_person_enter = 0
        self.today_person_exit = 0
        self.vehicle_flow_in = 0.0
        self.vehicle_flow_out = 0.0
        self.person_flow_in = 0.0
        self.person_flow_out = 0.0
        self.timestamp = ""

    def update_from_tracks(self, track_result):
        vehicle_count = sum(1 for t in track_result.tracks if t.class_name in ["car", "truck", "bus"])
        person_count = sum(1 for t in track_result.tracks if t.class_name == "person")
        self.current_vehicles = vehicle_count
        self.current_persons = person_count

    def update_from_events(self, events):
        for event in events:
            if event.event_type == "VehicleEnter":
                self.today_vehicle_enter += 1
            elif event.event_type == "VehicleExit":
                self.today_vehicle_exit += 1
            elif event.event_type == "PersonEnter":
                self.today_person_enter += 1
            elif event.event_type == "PersonExit":
                self.today_person_exit += 1

    def model_dump(self):
        return {
            "current_vehicles": self.current_vehicles,
            "current_persons": self.current_persons,
            "today_vehicle_enter": self.today_vehicle_enter,
            "today_vehicle_exit": self.today_vehicle_exit,
            "today_person_enter": self.today_person_enter,
            "today_person_exit": self.today_person_exit,
            "vehicle_flow_in": self.vehicle_flow_in,
            "vehicle_flow_out": self.vehicle_flow_out,
            "person_flow_in": self.person_flow_in,
            "person_flow_out": self.person_flow_out,
            "timestamp": self.timestamp,
        }


def load_rules():
    """从 configs/rules.yaml 加载告警规则 (离线评估, 不依赖 Redis)."""
    import yaml

    rules_path = PROJECT_DIR / "configs" / "rules.yaml"
    if not rules_path.exists():
        return []
    data = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    return data.get("rules", [])


def evaluate_alarms(statistics, rules):
    """评估当前统计是否触发告警规则, 返回触发的告警列表."""
    metrics = {
        "current_vehicles": statistics.current_vehicles,
        "current_persons": statistics.current_persons,
    }
    alarms = []
    for r in rules:
        value = metrics.get(r.get("metric"), 0)
        threshold = r.get("threshold", 0)
        if value >= threshold:
            alarms.append({
                "rule_id": r.get("id", ""),
                "level": r.get("level", "warning"),
                "category": r.get("category", ""),
                "message": r["message"].format(value=value, threshold=threshold),
                "value": value,
                "threshold": threshold,
                "timestamp": statistics.timestamp,
            })
    return alarms


def parse_args():
    parser = argparse.ArgumentParser(description="视频文件离线处理器")
    parser.add_argument("--video", required=True, help="视频文件路径")
    parser.add_argument("--output", default="./output", help="输出目录")
    parser.add_argument("--camera_id", default="CAM001", help="摄像头ID")
    parser.add_argument("--frame_skip", type=int, default=5, help="跳帧间隔（1=全处理，5=每5帧处理1帧）")
    parser.add_argument("--no-annotated", action="store_true", help="不生成标注视频")
    parser.add_argument("--line", default="0.5,0.1,0.5,0.9", help="计数线坐标（归一化 x1,y1,x2,y2, 默认垂直线）")
    parser.add_argument("--anchor", default="0.9,0.5", help="内侧锚点（归一化 x,y, 标识Enter方向所在侧）")
    parser.add_argument("--count-only", default=None, choices=["enter", "exit"], help="单向计数模式: enter=只计进入, exit=只计离开")
    return parser.parse_args()


def draw_annotations(frame, track_result, events, statistics, counter):
    annotated = frame.copy()

    line_start = counter._normalize_to_pixel(counter.line_points[0])
    line_end = counter._normalize_to_pixel(counter.line_points[1])

    cv2.line(annotated,
             (int(line_start[0]), int(line_start[1])),
             (int(line_end[0]), int(line_end[1])),
             (0, 0, 255), 2)
    cv2.putText(annotated, "counting line", (int(line_start[0]), int(line_start[1]) - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

    # 内侧锚点 (标识Enter方向所在侧)
    anchor_px = counter._normalize_to_pixel(counter.anchor_points)
    cv2.drawMarker(annotated, (int(anchor_px[0]), int(anchor_px[1])),
                   (255, 0, 0), cv2.MARKER_CROSS, 20, 2)
    cv2.putText(annotated, "inner(anchor)", (int(anchor_px[0]) + 12, int(anchor_px[1])),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)

    colors = {
        "car": (0, 255, 0),
        "truck": (0, 255, 255),
        "bus": (255, 255, 0),
        "person": (255, 0, 255)
    }

    for track in track_result.tracks:
        x1, y1, x2, y2 = [int(v) for v in track.bbox]
        color = colors.get(track.class_name, (0, 255, 0))

        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

        label = f"{track.track_id}:{track.class_name}({track.confidence:.2f})"
        cv2.putText(annotated, label, (x1, y1 - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        if track.history and len(track.history) > 1:
            for i in range(1, len(track.history)):
                pt1 = (int(track.history[i-1][0]), int(track.history[i-1][1]))
                pt2 = (int(track.history[i][0]), int(track.history[i][1]))
                cv2.line(annotated, pt1, pt2, color, 1)

    for event in events:
        cx, cy = int(event.cross_point[0]), int(event.cross_point[1])
        cv2.circle(annotated, (cx, cy), 8, (0, 0, 255), -1)
        cv2.putText(annotated, event.event_type, (cx + 12, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

    panel_y = 25
    info_lines = [
        f"Vehicles: {statistics.current_vehicles}  Persons: {statistics.current_persons}",
        f"V-In: {statistics.today_vehicle_enter}  V-Out: {statistics.today_vehicle_exit}",
        f"P-In: {statistics.today_person_enter}  P-Out: {statistics.today_person_exit}",
        f"V-Flow: {statistics.vehicle_flow_in:.1f}/{statistics.vehicle_flow_out:.1f} per min",
    ]

    for i, line in enumerate(info_lines):
        cv2.rectangle(annotated, (5, panel_y + i * 25 - 18), (320, panel_y + i * 25 + 5), (0, 0, 0), -1)
        cv2.putText(annotated, line, (10, panel_y + i * 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    return annotated


def process_video(args):
    video_path = args.video
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    video_start_time = datetime.now()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"错误: 无法打开视频文件 {video_path}")
        return

    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = 25.0  # 视频 FPS 元数据缺失时默认 25 帧, 避免除零
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if fps > 0 else 0

    print(f"\n{'='*60}")
    print(f"视频文件: {video_path}")
    print(f"分辨率: {frame_width}x{frame_height}")
    print(f"帧率: {fps:.2f} FPS")
    print(f"总帧数: {total_frames}")
    print(f"时长: {duration_sec:.1f}秒 ({duration_sec/60:.1f}分钟)")
    print(f"跳帧间隔: 每{args.frame_skip}帧处理1帧")
    print(f"输出目录: {output_dir}")
    print(f"{'='*60}\n")

    print("正在初始化算法模块...")

    tracker = None
    counter = None
    statistics = None

    try:
        from app.ai.tracker import ByteTracker
        tracker = ByteTracker()
        print("  [OK] 跟踪模块 (ByteTracker)")
    except Exception as e:
        print(f"  [FAIL] 跟踪模块: {e}")

    try:
        line_parts = [float(x) for x in args.line.split(",")]
        anchor_parts = [float(x) for x in args.anchor.split(",")]
        counter = LineCrossingCounter()
        counter.set_frame_size(frame_width, frame_height)
        counter.set_line([[line_parts[0], line_parts[1]], [line_parts[2], line_parts[3]]],
                         anchor=(anchor_parts[0], anchor_parts[1]))
        if args.count_only:
            counter.count_only = args.count_only
        print("  [OK] 越线计数模块 (LineCrossingCounter)")
    except Exception as e:
        print(f"  [FAIL] 越线计数模块: {e}")

    try:
        statistics = Statistics()
        print("  [OK] 统计模块")
    except Exception as e:
        print(f"  [FAIL] 统计模块: {e}")

    if tracker is None or counter is None:
        print("\n错误: 核心模块（跟踪/计数）初始化失败，无法处理视频")
        print("请确保 ultralytics 和 torch 已安装: pip3 install ultralytics")
        cap.release()
        return

    rules = load_rules()
    prev_alarming: set = set()

    video_name = Path(video_path).stem
    events_json_path = output_dir / f"{video_name}_events.json"
    stats_csv_path = output_dir / f"{video_name}_statistics.csv"
    alarms_json_path = output_dir / f"{video_name}_alarms.json"
    summary_json_path = output_dir / f"{video_name}_summary.json"

    if not args.no_annotated:
        annotated_video_path = output_dir / f"{video_name}_annotated.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out_writer = cv2.VideoWriter(
            str(annotated_video_path), fourcc, fps / args.frame_skip,
            (frame_width, frame_height)
        )
    else:
        annotated_video_path = None
        out_writer = None

    all_events = []
    all_alarms = []
    stats_timeline = []

    frame_idx = 0
    processed_idx = 0
    process_start_time = time.time()
    last_progress_time = process_start_time

    print(f"\n开始处理视频（共{total_frames}帧，每{args.frame_skip}帧处理1帧）...\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1

        if frame_idx % args.frame_skip != 0:
            continue

        processed_idx += 1

        video_time_sec = frame_idx / fps
        current_video_time = video_start_time + timedelta(seconds=video_time_sec)

        track_result = tracker.track(frame)

        events = counter.process_tracks(track_result, camera_id=args.camera_id)

        for event in events:
            event.timestamp = current_video_time.isoformat()

        if statistics is not None:
            statistics.update_from_tracks(track_result)
            statistics.update_from_events(events)
            current_stats = statistics
            current_stats.timestamp = current_video_time.isoformat()

            if processed_idx % max(1, int(fps / args.frame_skip)) == 0 or processed_idx == 1:
                stats_timeline.append({
                    "video_time": current_video_time.isoformat(),
                    "video_second": video_time_sec,
                    "frame": frame_idx,
                    **current_stats.model_dump()
                })
                # 告警边沿评估: 仅记录状态从正常->告警的时刻, 避免重复刷屏
                new_alarms = evaluate_alarms(current_stats, rules)
                current_alarming = {a["rule_id"] for a in new_alarms}
                for a in new_alarms:
                    if a["rule_id"] not in prev_alarming:
                        all_alarms.append(a)
                        print(f"  [告警] [{a['level']}] {a['message']}")
                prev_alarming = current_alarming
        else:
            current_stats = Statistics(
                current_vehicles=0, current_persons=0,
                today_vehicle_enter=0, today_vehicle_exit=0,
                today_person_enter=0, today_person_exit=0,
                vehicle_flow_in=0, vehicle_flow_out=0,
                person_flow_in=0, person_flow_out=0,
                timestamp=current_video_time.isoformat()
            )

        for event in events:
            event_dict = {
                "event_type": event.event_type,
                "track_id": event.track_id,
                "class_name": event.class_name,
                "timestamp": event.timestamp,
                "camera_id": event.camera_id,
                "cross_point": event.cross_point,
                "cross_line": event.cross_line,
                "direction": event.direction,
                "confidence": event.confidence,
            }
            all_events.append(event_dict)

        if out_writer is not None:
            annotated = draw_annotations(
                frame, track_result, events, current_stats, counter
            )

            time_str = current_video_time.strftime("%Y-%m-%d %H:%M:%S")
            cv2.rectangle(annotated, (frame_width - 260, 5), (frame_width - 5, 30), (0, 0, 0), -1)
            cv2.putText(annotated, time_str, (frame_width - 255, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

            out_writer.write(annotated)

        current_time = time.time()
        if current_time - last_progress_time >= 5.0 or frame_idx >= total_frames:
            progress = frame_idx / total_frames * 100
            elapsed = current_time - process_start_time if processed_idx > 1 else 0
            speed = processed_idx / max(elapsed, 1) if elapsed > 0 else 0

            print(f"  进度: {frame_idx}/{total_frames} ({progress:.1f}%) | "
                  f"已处理: {processed_idx}帧 | "
                  f"速度: {speed:.1f}fps | "
                  f"事件: {len(all_events)} | "
                  f"车辆: {current_stats.current_vehicles} | "
                  f"人员: {current_stats.current_persons}")
            last_progress_time = current_time

    cap.release()
    if out_writer is not None:
        out_writer.release()

    final_stats = statistics

    summary = {
        "video_info": {
            "path": video_path,
            "resolution": f"{frame_width}x{frame_height}",
            "fps": fps,
            "total_frames": total_frames,
            "duration_seconds": duration_sec,
            "duration_minutes": duration_sec / 60,
            "start_time": video_start_time.isoformat(),
            "camera_id": args.camera_id
        },
        "processing_info": {
            "frame_skip": args.frame_skip,
            "frames_processed": processed_idx,
            "processing_date": datetime.now().isoformat()
        },
        "final_statistics": final_stats.model_dump() if final_stats else None,
        "totals": {
            "total_events": len(all_events),
            "total_alarms": len(all_alarms),
            "vehicle_enter": sum(1 for e in all_events if e["event_type"] == "VehicleEnter"),
            "vehicle_exit": sum(1 for e in all_events if e["event_type"] == "VehicleExit"),
            "person_enter": sum(1 for e in all_events if e["event_type"] == "PersonEnter"),
            "person_exit": sum(1 for e in all_events if e["event_type"] == "PersonExit"),
        }
    }

    with open(events_json_path, "w", encoding="utf-8") as f:
        json.dump(all_events, f, ensure_ascii=False, indent=2)
    print(f"\n事件数据已保存: {events_json_path} ({len(all_events)}条)")

    with open(alarms_json_path, "w", encoding="utf-8") as f:
        json.dump(all_alarms, f, ensure_ascii=False, indent=2)
    print(f"告警数据已保存: {alarms_json_path} ({len(all_alarms)}条)")

    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"汇总报告已保存: {summary_json_path}")

    if stats_timeline:
        with open(stats_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=stats_timeline[0].keys())
            writer.writeheader()
            writer.writerows(stats_timeline)
        print(f"统计时间线已保存: {stats_csv_path} ({len(stats_timeline)}条)")

    if annotated_video_path:
        print(f"标注视频已保存: {annotated_video_path}")

    print(f"\n{'='*60}")
    print(f"处理完成！汇总:")
    print(f"  视频时长: {duration_sec/60:.1f}分钟")
    print(f"  处理帧数: {processed_idx}/{total_frames}")
    print(f"  越线事件: {len(all_events)}条")
    print(f"    - 车辆进入: {summary['totals']['vehicle_enter']}")
    print(f"    - 车辆离开: {summary['totals']['vehicle_exit']}")
    print(f"    - 人员进入: {summary['totals']['person_enter']}")
    print(f"    - 人员离开: {summary['totals']['person_exit']}")
    print(f"  告警数量: {len(all_alarms)}条")
    if final_stats:
        print(f"  最终在场车辆: {final_stats.current_vehicles}")
        print(f"  最终在场人员: {final_stats.current_persons}")
    print(f"{'='*60}")


if __name__ == "__main__":
    args = parse_args()
    process_video(args)