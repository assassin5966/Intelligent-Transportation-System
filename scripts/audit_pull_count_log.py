#!/usr/bin/env python3
"""Summarize pull-stream/counting log evidence; never export RTSP/HTTP URLs."""

import argparse
import ast
import collections
import gzip
import hashlib
import json
import re
import statistics
from pathlib import Path


HEADER = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) \|\s*(\w+)\s*\|\s*([^ ]+) - (.*)$")
DEVICE = re.compile(r"^\[([^]]+)\]")
AI_EVENT = re.compile(r"越线事件 (\w+) track=(\S+) class=(\S+).*?conf=([\d.]+)")
BACKEND_EVENT = re.compile(r"事件 (VehicleEnter|VehicleExit|PersonEnter|PersonExit) <- (.+)$")
STAT = re.compile(r"运行统计\((\d+)s\):")
METRICS = {
    "fps": r"处理fps=([\d.]+)", "grab": r"grab帧=(\d+)",
    "sampled": r"采样帧=(\d+)", "drop": r"丢帧=(\d+)\(([\d.]+)%\)",
    "busy": r"推理饱和丢帧=(\d+)", "shed": r"降载丢帧=(\d+)",
    "outbox_drop": r"outbox丢弃=(\d+)", "p95_ms": r"接收至持久化P95=([\d.]+)ms",
    "p99_ms": r"接收至持久化P99=([\d.]+)ms", "gap_s": r"最大处理帧间隔=([\d.]+)s",
    "infer_ms": r"推理=([\d.]+)ms/批", "batch_size": r"均批=([\d.]+)",
    "batch_wait_ms": r"组批等待=([\d.]+)ms", "executor_wait_ms": r"执行器等待=([\d.]+)ms",
    "track_ms": r"跟踪=([\d.]+)ms", "receive_to_complete_ms": r"接收至完成=([\d.]+)ms",
    "preprocess_ms": r"预处理=([\d.]+)ms/帧", "events": r"事件=(\d+)",
}
MARKERS = {
    "ffmpeg_fallback": "ffmpeg 解码不可用", "fps_mode_unsupported": "Unrecognized option 'fps_mode'",
    "opencv_scale_failed": "解码端缩放未生效", "pipeline_start": "管道启动:",
    "pipeline_stop": "管道停止", "low_fps": "有效帧率过低", "watchdog": "AI 处理异常",
    "wvp_play_failed": "WVP 点播失败", "wvp_480": "Temporarily Unavailable",
    "wvp_dns": "WVP 设备列表拉取失败", "connected": "视频流已连接",
    "outbox_start": "事件持久队列已启动", "outbox_storage_failure": "事件持久化失败",
    "outbox_delivery_failure": "投递失败，将重试", "outbox_read_failure": "事件队列存储异常",
    "quarantine": "已隔离，必须人工对账", "archive_failure": "MySQL 归档失败",
    "rules_updated": "触发热重载: business_rules.yaml",
}


def summarize(values):
    values = sorted(values)
    if not values:
        return None
    # Nearest-rank percentile: explicit and reproducible across Python versions.
    return {"n": len(values), "mean": round(statistics.mean(values), 3),
            "p50": round(statistics.median(values), 3),
            "p95": values[(95 * len(values) + 99) // 100 - 1],
            "min": values[0], "max": values[-1]}


def analyze(path):
    levels, functions, markers = collections.Counter(), collections.Counter(), collections.Counter()
    references = collections.defaultdict(list)
    devices = collections.defaultdict(lambda: {"stats": [], "events": collections.Counter(),
                                                "backend": collections.Counter(), "reject": collections.Counter(),
                                                "starts": [], "stops": [], "size": collections.Counter()})
    stats, confidences, channel_failures = [], [], collections.defaultdict(list)
    ai_events, backend_events, incidents, stream_events, congestion_records = [], [], [], [], []
    congestion_status = collections.Counter()
    congestion_by_device = collections.Counter()
    watchdog_states = collections.Counter()
    urls = set()
    all_ai, all_backend = collections.Counter(), collections.Counter()
    raw_hash, parsed, unmatched, first, last, line_count = hashlib.sha256(), 0, [], None, None, 0
    with path.open("rb") as source:
        for line_count, raw in enumerate(source, 1):
            raw_hash.update(raw)
            match = HEADER.match(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            if not match:
                unmatched.append(line_count)
                continue
            parsed += 1
            ts, level, fn, message = match.groups()
            first = first or ts
            last = ts
            levels[level] += 1
            functions[f"{level} {fn.rsplit(':', 1)[0]}"] += 1
            for key, text in MARKERS.items():
                if text in message:
                    markers[key] += 1
                    if len(references[key]) < 4:
                        references[key].append({"line": line_count, "time": ts})
            match = DEVICE.match(message)
            name = match.group(1) if match else None
            if level in ("WARNING", "ERROR"):
                incident = {"line": line_count, "time": ts, "level": level,
                            "function": fn.rsplit(":", 1)[0], "device": name,
                            "markers": [key for key, marker in MARKERS.items() if marker in message]}
                if "AI 处理异常: " in message:
                    try:
                        health = ast.literal_eval(message.split("AI 处理异常: ", 1)[1])
                        if isinstance(health, dict):
                            incident["health"] = health
                            watchdog_states[health.get("state", "unknown")] += 1
                    except (ValueError, SyntaxError):
                        watchdog_states["unparsed"] += 1
                incidents.append(incident)
            if name and "管道启动:" in message:
                devices[name]["starts"].append({"line": line_count, "time": ts})
                address = message.split("管道启动:", 1)[1].strip()
                stream_events.append({"line": line_count, "time": ts, "kind": "start", "device": name,
                                      "url_sha256": hashlib.sha256(address.encode()).hexdigest()})
            if name and "管道停止" in message:
                devices[name]["stops"].append({"line": line_count, "time": ts})
                stream_events.append({"line": line_count, "time": ts, "kind": "stop", "device": name})
            if name and (match := re.search(r"计数线帧尺寸: (\d+x\d+)", message)):
                devices[name]["size"][match.group(1)] += 1
            if "ffmpeg 解码不可用" in message and (match := re.search(r"回退 opencv \(([^)]+)\)", message)):
                urls.add(match.group(1))
                stream_events.append({"line": line_count, "time": ts, "kind": "ffmpeg_fallback",
                                      "url_sha256": hashlib.sha256(match.group(1).encode()).hexdigest(),
                                      "fps_mode_unsupported": "Unrecognized option 'fps_mode'" in message})
            if match := re.search(r"视频流已连接: (.+) \((\d+x\d+)\)", message):
                stream_events.append({"line": line_count, "time": ts, "kind": "connected",
                                      "url_sha256": hashlib.sha256(match.group(1).encode()).hexdigest(),
                                      "source_size": match.group(2)})
            if "WVP 点播失败" in message and (match := re.search(r"WVP 点播失败 ([\d]+/[\d]+)", message)):
                code = re.search(r"'code':\s*(-?\d+)", message)
                channel_failures[match.group(1)].append({"line": line_count, "time": ts,
                                                         "code": int(code.group(1)) if code else None})
            if name and (match := AI_EVENT.search(message)):
                event_type, _track, _class, confidence = match.groups()
                all_ai[event_type] += 1
                devices[name]["events"][event_type] += 1
                confidences.append(float(confidence))
                detail = {"line": line_count, "time": ts, "device": name, "event_type": event_type,
                          "track": _track, "class": _class, "confidence": float(confidence)}
                if direction := re.search(r"dir=(\S+)", message):
                    detail["direction"] = direction.group(1)
                if point := re.search(r"交点=\(([-\d.]+),([-\d.]+)\)", message):
                    detail["cross_point"] = [float(point.group(1)), float(point.group(2))]
                ai_events.append(detail)
            if fn.startswith("app.backend.api.events:receive_event") and (match := BACKEND_EVENT.search(message)):
                event_type, event_device = match.groups()
                all_backend[event_type] += 1
                devices[event_device]["backend"][event_type] += 1
                backend_events.append({"line": line_count, "time": ts, "device": event_device,
                                       "event_type": event_type})
            if fn.startswith("app.ai.pipeline:_report_congestion"):
                status = re.search(r"HTTP (\d+)", message)
                congestion_status[status.group(1) if status else "unknown"] += 1
                if name:
                    congestion_by_device[name] += 1
                record = {"line": line_count, "time": ts, "device": name,
                          "http_status": int(status.group(1)) if status else None}
                for field in ("roi_vehicles", "roi_persons", "flow", "person_flow"):
                    value = re.search(rf"(?:^|\s){field}=([\d.]+)", message)
                    if value:
                        record[field] = float(value.group(1))
                congestion_records.append(record)
            if not name or not (match := STAT.search(message)):
                continue
            row = {"line": line_count, "time": ts, "device": name, "seconds": int(match.group(1))}
            for key, pattern in METRICS.items():
                if match := re.search(pattern, message):
                    row[key] = float(match.group(1))
                    if key == "drop":
                        row["drop_pct"] = float(match.group(2))
            row["reject"] = {}
            if " | 拒绝: " in message:
                row["reject"] = {key: int(n) for key, n in re.findall(r"([^\s=]+)=(\d+)", message.split(" | 拒绝: ", 1)[1])}
                devices[name]["reject"].update(row["reject"])
            stats.append(row)
            devices[name]["stats"].append(row)

    total_sampled = sum(row.get("sampled", 0) for row in stats)
    total_drop = sum(row.get("drop", 0) for row in stats)
    seconds = sum(row["seconds"] for row in stats)
    metrics = {key: summarize([row[key] for row in stats if key in row]) for key in (*METRICS, "drop_pct")}
    result_devices = {}
    for name, data in sorted(devices.items()):
        rows = data["stats"]
        result_devices[name] = {
            "windows": len(rows), "first_window": rows[0] if rows else None,
            "last_window": rows[-1] if rows else None,
            "fps": summarize([row["fps"] for row in rows if "fps" in row]),
            "ai_events": dict(data["events"]), "backend_events": dict(data["backend"]),
            "window_events": int(sum(row.get("events", 0) for row in rows)),
            "rejections": dict(data["reject"]), "starts": data["starts"], "stops": data["stops"],
            "source_sizes": dict(data["size"]),
        }
    return {
        "source_file": path.name, "sha256": raw_hash.hexdigest(), "total_lines": line_count,
        "parsed_lines": parsed, "unmatched_line_numbers": unmatched, "first_timestamp": first,
        "last_timestamp": last, "levels": dict(levels), "function_counts": dict(functions),
        "markers": dict(markers), "first_references": dict(references),
        "statistics": {"windows": len(stats), "window_lengths": dict(collections.Counter(r["seconds"] for r in stats)),
                       "devices": sum(bool(d["stats"]) for d in devices.values()), "duration_sum_s": seconds,
                       "sampled_total": int(total_sampled), "drop_total": int(total_drop),
                       "weighted_drop_pct": round(100 * total_drop / total_sampled, 3) if total_sampled else None,
                       "sample_fps": round(total_sampled / seconds, 3) if seconds else None,
                       "grab_fps": round(sum(r.get("grab", 0) for r in stats) / seconds, 3) if seconds else None,
                       "metrics": metrics,
                       "rejections": dict(sum((collections.Counter(r["reject"]) for r in stats), collections.Counter())),
                       "first_window": stats[0] if stats else None, "last_window": stats[-1] if stats else None},
        "events": {"ai": dict(all_ai), "backend": dict(all_backend), "confidence": summarize(confidences),
                   "window_events": int(sum(r.get("events", 0) for r in stats)),
                   "ai_records": ai_events, "backend_records": backend_events},
        "fallback_unique_urls": len(urls),
        "wvp_failures": {key: {"count": len(v), "first": v[0], "last": v[-1],
                                "codes": dict(collections.Counter(str(x["code"]) for x in v)), "records": v}
                         for key, v in sorted(channel_failures.items())},
        "devices": result_devices, "statistics_windows": stats,
        "warning_error_records": incidents, "stream_events": stream_events,
        "watchdog_states": dict(watchdog_states),
        "congestion_reports": {"http_status_counts": dict(congestion_status),
                               "device_counts": dict(congestion_by_device), "records": congestion_records},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path, help="JSON output; .gz suffix compresses it (default: stdout)")
    args = parser.parse_args()
    content = json.dumps(analyze(args.log), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        if args.output.suffix == ".gz":
            args.output.write_bytes(gzip.compress(content.encode("utf-8"), mtime=0))
        else:
            args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
