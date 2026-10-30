#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GB28181 下级设备模拟器
=====================
模拟一台国标 IPC / 下级设备，向上级平台（WVP-PRO）完成：

    1. SIP REGISTER（Digest 摘要认证，401 质询 -> 带 Authorization 重发）
    2. 周期性重新注册 + Keepalive 心跳报文
    3. 响应平台的 Catalog / DeviceInfo / DeviceStatus 查询，上报通道目录
    4. 收到 INVITE（s=Play）后回 100 Trying / 200 OK + SDP，等 ACK 后
       用 ffmpeg 生成 MPEG-PS 流，封装成 RTP(PT=96) 推给平台媒体服务器
    5. 收到 BYE 停流

用法：
    python3 gb28181_device.py [--config dev.json] [--verbose]

依赖：仅 Python3 标准库 + 一个可用的 ffmpeg（默认通过
     `docker exec -i <容器> ffmpeg` 复用容器内的 ffmpeg）。
"""

import argparse
import hashlib
import json
import os
import random
import re
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

CRLF = "\r\n"

# --------------------------------------------------------------------------- #
# 默认配置（可通过 --config 覆盖）
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "server_ip": "10.10.10.11",       # 上级平台 SIP IP
    "server_port": 5060,              # 上级平台 SIP 端口
    "domain": "1402000000",           # SIP 域（国标 10 位）
    "server_id": "14020000002090000006",
    "device_id": "44010200491100000001",   # 本设备国标编号（20 位）
    "device_name": "Sim-GB28181-IPC",
    "manufacturer": "SimCorp",
    "model": "SIM-IPC-1",
    "password": "Gb@202608",           # 设备注册密码
    "local_ip": "10.10.10.11",        # 本机对外的 IP
    "local_port": 15060,              # 本机 SIP 监听端口
    "register_expires": 3600,
    "keepalive_interval": 60,
    # 自动补拉 (持续推流测试): 通道没有活跃会话时, 自动调 WVP 点播接口触发 INVITE
    # 通过 --keep 或 sim.json "auto_replay": {"enabled": true} 开启
    "auto_replay": {
        "enabled": False,
        "interval": 60,                       # 巡检周期(秒)
        "cooldown": 180,                      # 单通道补拉失败后的冷却(秒), 防打爆 WVP
        "wvp_base": "http://127.0.0.1:18080",  # WVP Web API
        "username": "admin",
        "password": "admin",                  # 与 wvp_api.sh 同款默认, 自动尝试 md5/明文
    },
    "charset": "GB2312",              # 国标 XML 传统编码
    # ffmpeg 调用前缀：默认复用 wvp-server 容器里的 ffmpeg
    "ffmpeg_cmd": ["docker", "exec", "-i", "wvp-server", "ffmpeg"],
    "channels": [
        {"id": "44010200491320000001", "name": "永泰东门1口", "rtsp": "", "mark": "YTDM1",
         "video": "/opt/sim-media/cam1.mp4"},
        {"id": "44010200491320000002", "name": "永泰东门2口", "rtsp": "", "mark": "YTDM2",
         "video": "/opt/sim-media/cam2.mp4"},
        {"id": "44010200491320000003", "name": "永泰东门3口", "rtsp": "", "mark": "YTDM3",
         "video": "/opt/sim-media/cam3.mp4"},
        {"id": "44010200491320000004", "name": "永泰东门4口", "rtsp": "", "mark": "YTDM4",
         "video": "/opt/sim-media/cam4.mp4"},
        # 新增: 永泰东门实采视频 (每路一个独立通道, 推到自身名称对应的管道)
        #   video 文件名即素材名; name 用文件名(去扩展)便于平台目录对照; mark 取设备编码前缀
        #   素材需放入执行环境媒体目录 (默认 /opt/sim-media, 或本机 sim/media/),
        #   缺失时 _heal_paths 会从本目录 media/ 自动 docker cp 注入
        {"id": "44010200491320000005", "name": "GAJK-4925永泰东门入口处路东以南106米(枪-北)031206", "rtsp": "", "mark": "GAJK4925",
         "video": "/opt/sim-media/GAJK-4925永泰东门入口处路东以南106米(枪-北).mp4"},
        {"id": "44010200491320000006", "name": "GAJK-4926永泰东门入口处路西以南106米(枪-南)031208", "rtsp": "", "mark": "GAJK4926",
         "video": "/opt/sim-media/GAJK-4926永泰东门入口处路西以南106米(枪-南).mp4"},
        {"id": "44010200491320000007", "name": "GAKK-0015永泰东门北都街-卡口223014", "rtsp": "", "mark": "GAKK0015A",
         "video": "/opt/sim-media/GAKK-0015永泰东门北都街-卡口.mp4"},
        {"id": "44010200491320000008", "name": "GAKK-0015永泰东门北都街-卡口223015", "rtsp": "", "mark": "GAKK0015B",
         "video": "/opt/sim-media/GAKK-0015永泰东门北都街-卡口223015.mp4"},
    ],
    "video": {
        "size": "1280x720",
        "fps": 25,
        "bitrate": "2500k",
        "gop": 50,
        # 容器内中文字体（drawtext 用）
        "font": "/opt/sim-fonts/wqy-zenhei.ttc",
    },
}


# --------------------------------------------------------------------------- #
# ffmpeg 执行环境解析 (启动时自动探测 + 自愈)
# --------------------------------------------------------------------------- #
# 旧部署: ffmpeg + 媒体 + 字体都放在名为 wvp-server 的容器里;
# 新部署 (start_all_gpu.sh / setting-server-gpu): 容器名是 wvp-upper-wvp。
# 本段在启动时找出真正可用的执行环境:
#   - 探测一律用 `command -v` (精简镜像可能没有 which 命令)
#   - 容器里有 ffmpeg 但缺 cam*.mp4 时, 自动 docker cp 本目录 media/ 的素材进容器
#   - 每个候选容器失败时打印具体原因 (缺 ffmpeg / 缺媒体), 不再笼统报错
#   - 找不到任何可用环境时早失败并给出修复指引
SIM_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_MEDIA = os.path.join(SIM_DIR, "media")          # 服务器上即 /home/sim/media
MEDIA_DIRS = ["/opt/sim-media", "/data/sim-media", "/home/sim/media"]
FONT_FILES = [
    "/opt/sim-fonts/wqy-zenhei.ttc",
    "/home/sim/fonts/wqy-zenhei.ttc",
    "/home/sim/media/wqy-zenhei.ttc",
]
CONTAINER_PRIORITY = [
    "wvp-upper-wvp",   # start_all_gpu.sh / setting-server-gpu (当前部署)
    "wvp-server",      # 旧独立部署
    "wvp",
    "wvp-pro",
    "wvp-upper-zlm",   # ZLM 容器 (一般无 ffmpeg, 兜底)
]
CAM_FILES = ("cam1.mp4", "cam2.mp4", "cam3.mp4", "cam4.mp4")


def _run(args, timeout=10):
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return -1, ""


def _sh(prefix, cmd, timeout=10):
    """在给定执行前缀 (容器 exec / 本机空前缀) 里跑一条 sh 命令。"""
    return _run(prefix + ["sh", "-c", cmd], timeout)


def _running_containers():
    code, out = _run(["docker", "ps", "--format", "{{.Names}}"])
    return out.split() if code == 0 else []


def _exec_prefix(cfg):
    """由 ffmpeg_cmd 推出校验用的执行前缀 (容器 exec / 本机空)。"""
    fc = cfg.get("ffmpeg_cmd") or []
    if len(fc) >= 5 and fc[0] == "docker" and fc[1] == "exec":
        return ["docker", "exec", fc[3]]
    return []


def _heal_paths(cfg):
    """最后一道防线: 校验最终 video / font 路径在执行环境里真实存在。

    - 通道 video 不存在 -> 尝试从本目录 media/ docker cp 注入后复验, 仍缺则硬失败
    - font 不存在 -> 置 None (不叠加 OSD), 绝不让 drawtext 拿着坏路径把 ffmpeg 打挂
    """
    prefix = _exec_prefix(cfg)
    cont = prefix[2] if prefix else None
    for ch in cfg.get("channels", []):
        v = ch.get("video")
        if not v or not v.strip():
            continue          # 未配视频文件 -> RtpPusher 自动用 testsrc2 测试源
        if _sh(prefix, "[ -f '%s' ]" % v)[0] == 0:
            continue
        base = _base(v)
        src = os.path.join(LOCAL_MEDIA, base)
        if cont and os.path.isfile(src):
            target_dir = _dirname(v) or "/opt/sim-media"
            _run(["docker", "exec", cont, "mkdir", "-p", target_dir])
            if _run(["docker", "cp", src, "%s:%s/" % (cont, target_dir)],
                    timeout=180)[0] == 0:
                log("已补注入缺失素材 %s -> %s:%s/" % (base, cont, target_dir))
        if _sh(prefix, "[ -f '%s' ]" % v)[0] != 0:
            log("!! 通道 [%s] 视频文件在执行环境中不存在: %s" % (ch.get("name"), v))
            log("   本机素材目录: %s  (需包含 %s)" % (LOCAL_MEDIA, base))
            sys.exit(1)
    f = (cfg.get("video") or {}).get("font")
    if f and _sh(prefix, "[ -f '%s' ]" % f)[0] != 0:
        log("!! 字体在执行环境中不存在(%s) -> 本次运行不叠加 OSD 字幕" % f)
        cfg["video"]["font"] = None


def _base(name):
    """POSIX 语义取文件名 (不依赖运行平台, '//cam1.mp4' -> 'cam1.mp4')。"""
    return name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _dirname(name):
    """POSIX 语义取目录 ('/opt/sim-media/cam1.mp4' -> '/opt/sim-media')。"""
    p = name.replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _apply_env(cfg, ffmpeg_cmd, media_dir, font, where):
    cfg["ffmpeg_cmd"] = ffmpeg_cmd
    for ch in cfg.get("channels", []):
        if ch.get("video"):
            ch["video"] = media_dir + "/" + _base(ch["video"])
    cfg.setdefault("video", {})["font"] = font
    log("ffmpeg 执行环境: %s  媒体目录=%s  字体=%s"
        % (where, media_dir, font or "无(视频将不叠加 OSD 字幕)"))
    _heal_paths(cfg)


def _probe_container(c):
    """探测一个容器。返回 (media_dir, font) 或 (None, 失败原因字符串)。"""
    prefix = ["docker", "exec", c]
    # 1) ffmpeg —— 用 command -v (POSIX 内建), 精简镜像没有 which
    code, out = _sh(prefix, "command -v ffmpeg")
    if code != 0 or not out:
        return None, "容器内无 ffmpeg"
    # 2) 媒体: 容器内已有?
    media_dir = None
    for d in MEDIA_DIRS:
        if _sh(prefix, "[ -f %s/cam1.mp4 ]" % d)[0] == 0:
            media_dir = d
            break
    # 2b) 没有 -> 从本目录 media/ 自动注入 (docker cp)
    if media_dir is None and os.path.isfile(os.path.join(LOCAL_MEDIA, "cam1.mp4")):
        _run(["docker", "exec", c, "mkdir", "-p", "/opt/sim-media"])
        ok = True
        for f in CAM_FILES:
            src = os.path.join(LOCAL_MEDIA, f)
            if os.path.isfile(src) and _run(
                    ["docker", "cp", src, "%s:/opt/sim-media/" % c],
                    timeout=180)[0] != 0:
                ok = False
        if ok and _sh(prefix, "[ -f /opt/sim-media/cam1.mp4 ]")[0] == 0:
            media_dir = "/opt/sim-media"
            log("已自动注入媒体 %s -> %s:/opt/sim-media "
                "(注意: 容器重建后需重新注入或固化进镜像)" % (",".join(CAM_FILES), c))
    if media_dir is None:
        return None, ("有 ffmpeg 但缺 cam*.mp4 (容器内 %s 均无, 本机 %s 无法自动注入)"
                      % ("/".join(MEDIA_DIRS), LOCAL_MEDIA))
    # 3) 字体: 容器内有就用; 本机有就补; 都没有 -> None (不叠加 OSD)
    font = None
    for f in FONT_FILES:
        if _sh(prefix, "[ -f %s ]" % f)[0] == 0:
            font = f
            break
    if font is None:
        local_font = os.path.join(LOCAL_MEDIA, "wqy-zenhei.ttc")
        if os.path.isfile(local_font):
            _run(["docker", "exec", c, "mkdir", "-p", "/opt/sim-fonts"])
            if _run(["docker", "cp", local_font, "%s:/opt/sim-fonts/" % c],
                    timeout=60)[0] == 0:
                font = "/opt/sim-fonts/wqy-zenhei.ttc"
                log("已自动注入字体 -> %s:/opt/sim-fonts/" % c)
    return media_dir, font


def _ffmpeg_exec_prefix(cfg):
    """返回在 ffmpeg 所在环境执行命令的前缀 (容器内 docker exec / 本机为空列表)."""
    fc = cfg.get("ffmpeg_cmd") or []
    if len(fc) >= 5 and fc[0] == "docker" and fc[1] == "exec":
        return ["docker", "exec", fc[3]]
    return []


def _list_ffmpeg_pids(cfg, needles):
    """列出命令行包含任一 needle(字面量) 的 ffmpeg pid.

    用 ps 输出 + Python 端字面量判断, 不用 pkill -f —— 后者走 ERE, 设备名里的
    ( ) . 等元字符会让模式失配 (历史孤儿进程泄漏的根因)。
    """
    if not needles:
        return []
    prefix = _ffmpeg_exec_prefix(cfg)
    cmd = prefix + ["ps", "-eo", "pid,args"] if prefix else ["ps", "-eo", "pid,args"]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=15, universal_newlines=True).stdout or ""
    except Exception:
        return []
    pids = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        pid, _, args = line.partition(" ")
        if "ffmpeg" not in args:
            continue
        if any(n and n in args for n in needles):
            pids.append(pid.strip())
    return pids


def kill_container_ffmpeg(cfg, needles, quiet=False):
    """强杀命中特征的 ffmpeg (幂等, 失败静默), 返回清理数量."""
    pids = _list_ffmpeg_pids(cfg, needles)
    if not pids:
        return 0
    prefix = _ffmpeg_exec_prefix(cfg)
    cmd = prefix + ["kill", "-9"] + pids if prefix else ["kill", "-9"] + pids
    try:
        subprocess.call(cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        return 0
    if not quiet:
        log("清理残留 ffmpeg 进程 %d 个: %s" % (len(pids), " ".join(pids[:8])))
    return len(pids)


def sweep_stale_ffmpeg(cfg):
    """启动时清理上一轮遗留的 ffmpeg (sim 被 kill 时容器内子进程不会自动退出)."""
    if not cfg.get("channels"):
        return 0
    needles = ["stream_loop -1 -i"]
    for ch in cfg["channels"]:
        if ch.get("video"):
            needles.append(ch["video"])
        if ch.get("rtsp"):
            needles.append(ch["rtsp"])
    return kill_container_ffmpeg(cfg, needles)


def _kill_pids(cfg, pids):
    """精确 kill 指定 pid 列表 (容器内 / 本机自适应), 返回清理数量."""
    if not pids:
        return 0
    prefix = _ffmpeg_exec_prefix(cfg)
    cmd = prefix + ["kill", "-9"] + list(pids) if prefix else ["kill", "-9"] + list(pids)
    try:
        subprocess.call(cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        return 0
    return len(pids)


def start_ffmpeg_watchdog(dev):
    """兜底巡检: 容器内 ffmpeg 数量明显超过活跃会话时, 清掉不属于任何活跃会话的孤儿.

    覆盖「线程异常退出没走到 _stop_proc」「sim 被 kill -9」等无法在退出路径清理的
    场景 —— 孤儿累积会打满 CPU 并让 WVP 持续报收流超时。
    """
    def _loop():
        while getattr(dev, "running", False):
            time.sleep(60)
            try:
                cfg = dev.cfg
                active = [p for p in list(dev.sessions.values())
                          if getattr(p, "running", False)]
                allneedles = ["stream_loop -1 -i"]
                for ch in cfg.get("channels") or []:
                    if ch.get("video"):
                        allneedles.append(ch["video"])
                    if ch.get("rtsp"):
                        allneedles.append(ch["rtsp"])
                total = _list_ffmpeg_pids(cfg, allneedles)
                if len(total) <= len(active) + 2:
                    continue
                keep = set()
                for p in active:
                    keep.update(_list_ffmpeg_pids(cfg, p._needles()))
                stale = [pid for pid in total if pid not in keep]
                n = _kill_pids(cfg, stale)
                if n:
                    log("巡检清理: 残留 ffmpeg %d 个 (活跃会话 %d 路)" % (n, len(active)))
            except Exception:
                pass
    t = threading.Thread(target=_loop, name="ffmpeg-watchdog")
    t.daemon = True
    t.start()


def resolve_ffmpeg_env(cfg):
    """把 ffmpeg_cmd / channels[*].video / video.font 解析到真实可用环境。

    探测顺序:
      1. 环境变量 SIM_FFMPEG_CONTAINER 指定的容器
      2. 配置 ffmpeg_cmd 里指定的容器 (若仍指向 docker exec)
      3. CONTAINER_PRIORITY + 运行中且名字含 wvp/zlm/sim 的容器
      4. 本机 (运行本脚本的进程环境) ffmpeg + 本目录 media/
    """
    if not cfg.get("channels"):
        return
    running = _running_containers()
    if not running:
        log("!! docker ps 不可用 (sim 必须运行在有 docker 命令权限的环境)")
    candidates = []
    env_c = os.environ.get("SIM_FFMPEG_CONTAINER")
    if env_c:
        candidates.append(env_c)
    fc = cfg.get("ffmpeg_cmd") or []
    if len(fc) >= 4 and fc[0] == "docker" and fc[1] == "exec":
        candidates.append(fc[3])
    candidates += CONTAINER_PRIORITY
    candidates += [c for c in running
                   if any(k in c.lower() for k in ("wvp", "zlm", "sim"))]

    seen = set()
    for c in candidates:
        if not c or c in seen or c not in running:
            continue
        seen.add(c)
        got, why = _probe_container(c)
        if got:
            _apply_env(cfg, ["docker", "exec", "-i", c, "ffmpeg"],
                       got[0], got[1], "容器 %s" % c)
            if got[1] is None:
                log("!! %s 内无中文字体, 视频将不叠加 OSD 字幕; 如需要:"
                    " docker cp wqy-zenhei.ttc %s:/opt/sim-fonts/ 后重启 sim" % (c, c))
            return
        log("   - 候选容器 %-18s 不可用: %s" % (c, why))

    # 本机兜底: 本机 ffmpeg + 本目录 media/
    code, out = _run(["sh", "-c", "command -v ffmpeg"])
    has_local_media = os.path.isfile(os.path.join(LOCAL_MEDIA, "cam1.mp4"))
    if code == 0 and out and has_local_media:
        font = None
        for f in FONT_FILES + [os.path.join(LOCAL_MEDIA, "wqy-zenhei.ttc")]:
            if os.path.isfile(f):
                font = f
                break
        _apply_env(cfg, ["ffmpeg"], LOCAL_MEDIA, font, "本机")
        return

    log("=" * 66)
    log("!! 未找到可用的 ffmpeg 推流环境")
    log("   已检查容器: %s" % (", ".join(sorted(seen)) or "无"))
    log("   本机 ffmpeg: %s   本机媒体(%s): %s"
        % ("有" if (code == 0 and out) else "无", LOCAL_MEDIA,
           "有" if has_local_media else "无"))
    log("   修复方式 (任选其一), 然后重启 sim:")
    log("   A) 容器有 ffmpeg 但缺媒体: 把 cam*.mp4 放到 %s 后重启 (会自动注入)," % LOCAL_MEDIA)
    log("      或手动: docker exec wvp-upper-wvp mkdir -p /opt/sim-media && "
        "docker cp cam1.mp4 wvp-upper-wvp:/opt/sim-media/ (cam1-4)")
    log("   B) 本机装 ffmpeg: apt install -y ffmpeg, 素材放 %s" % LOCAL_MEDIA)
    log("   C) 写 sim.json 精确覆盖 ffmpeg_cmd / channels[*].video / video.font")
    log("=" * 66)
    sys.exit(1)


def log(msg):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def md5_hex(s):
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.md5(s).hexdigest()


def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


# --------------------------------------------------------------------------- #
# SIP 报文解析 / 构造
# --------------------------------------------------------------------------- #
def parse_sip(data):
    """返回 (start_line, headers[dict 小写键], raw_header_list, body_str)"""
    text = data.decode("utf-8", "replace")
    if "\r\n\r\n" in text:
        head, _, body = text.partition("\r\n\r\n")
    elif "\n\n" in text:
        head, _, body = text.partition("\n\n")
    else:
        head, body = text, ""
    lines = head.replace("\r\n", "\n").split("\n")
    start = lines[0].strip() if lines else ""
    headers = {}
    raw = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        k, _, v = ln.partition(":")
        k = k.strip()
        v = v.strip()
        headers[k.lower()] = v
        raw.append((k, v))
    return start, headers, raw, body


def parse_auth(header_value):
    """解析 WWW-Authenticate / Authorization 的 Digest 参数"""
    params = {}
    if not header_value:
        return params
    v = header_value.strip()
    if v.lower().startswith("digest"):
        v = v[6:]
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', v):
        params[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return params


def build_message(start_line, headers, body=""):
    """headers: list[(name, value)]；自动补 Content-Length"""
    lines = [start_line]
    has_cl = False
    for k, v in headers:
        if k.lower() == "content-length":
            has_cl = True
        lines.append("%s: %s" % (k, v))
    if not has_cl:
        lines.append("Content-Length: %d" % len(body.encode("utf-8", "replace")))
    lines.append("")
    lines.append("")
    return (CRLF.join(lines) + body).encode("utf-8", "replace")


def build_response(start_line, headers, code, reason, extra=None, body=""):
    """依据请求构造响应：复制 Via/From/To/Call-ID/CSeq"""
    extra = extra or {}
    hdrs = []
    for key in ("via", "from", "to", "call-id", "cseq"):
        if key in headers:
            name = {"via": "Via", "from": "From", "to": "To",
                    "call-id": "Call-ID", "cseq": "CSeq"}[key]
            val = headers[key]
            if key == "to" and "tag=" not in val:
                # 同一会话（Call-ID）内保持 To-tag 一致
                val = "%s;tag=%s" % (val, md5_hex(headers.get("call-id", ""))[:12])
            hdrs.append((name, val))
    for k, v in extra.items():
        hdrs.append((k, v))
    hdrs.append(("User-Agent", "SimGB28181/1.0"))
    resp_line = "SIP/2.0 %d %s" % (code, reason)
    return build_message(resp_line, hdrs, body)


# --------------------------------------------------------------------------- #
# 媒体推送：ffmpeg -> 解析重组 -> 重建 GB28181 MPEG-2 PS -> RTP
# --------------------------------------------------------------------------- #
class RtpPusher(object):
    """ffmpeg 生成 PS 流 -> 解析重组每帧 ES -> 重建 GB28181 MPEG-2 PS -> RTP 推流

    ffmpeg -f mpeg 输出的是 MPEG-1 系统流 (MPEG-1 pack 头 / stream_id E2 /
    MPEG-1 PES 头 / 无 PSM), 不是国标要求的 MPEG-2 PS, ZLM 直接按 MPEG-2
    解析只能解出第一帧 —— 所以这里逐起始码解析 ffmpeg 输出, 按帧拼回 ES,
    再以 [BA + BB + BC(PSM) + E0 PES(MPEG-2 头 + PTS)] 模式重新封装发送,
    与海康/大华等真实国标设备的字节流一致 (见 _flush_packs)。
    """

    def __init__(self, cfg, channel, dst_ip, dst_port, ssrc, verbose=False):
        self.cfg = cfg
        self.ch = channel
        self.dst = (dst_ip, int(dst_port))
        self.ssrc = ssrc & 0xFFFFFFFF
        self.verbose = verbose
        self.seq = random.randint(0, 30000)
        self.proc = None
        self.sock = None
        self.running = False
        self.bytes_sent = 0
        self.pkts_sent = 0
        self.thread = None
        # PS 流解析/重建状态
        self._ps_buf = b""
        self._rtp_ts = None       # 最近一次发送使用的 RTP 时间戳 (90kHz, 未回绕)
        self._pts_off = 0         # PTS -> RTP 时间戳偏移 (处理素材循环重绕时的时间戳回跳)
        self._cur_pts = None      # 当前帧的输入 PTS (ffmpeg PES 解析所得)
        self._cur_es = []         # 当前帧的 ES 载荷分片 (跨 PES 累积)

    def _ffmpeg_args(self):
        v = self.cfg["video"]
        mark = self.ch.get("mark") or self.ch["id"][-4:]
        name = self.ch.get("name") or mark
        w, h = v["size"].split("x")
        if self.ch.get("rtsp"):
            src = ["-rtsp_transport", "tcp", "-i", self.ch["rtsp"]]
        elif self.ch.get("video"):
            # 本地视频文件无限循环推流（道路监控素材），容器内路径
            src = ["-stream_loop", "-1", "-i", self.ch["video"]]
        else:
            src = ["-f", "lavfi", "-i",
                   "testsrc2=size=%s:rate=%s" % (v["size"], v["fps"])]
        fo = "fontfile=%s:" % v["font"] if v.get("font") else ""
        # 无字体时必须整个去掉 drawtext (缺 fontfile 的 drawtext 会让 ffmpeg 报错退出)
        parts = [
            "crop=iw:ih*92/100:0:0",                   # 裁掉素材底部水印
            "scale={w}:{h}:force_original_aspect_ratio=increase",
            "crop={w}:{h}",
            "fps={fps}",
        ]
        if v.get("font"):
            parts.append(
                "drawtext={fo}text='{mark} {name}':fontcolor=white:fontsize=34:"
                "box=1:boxcolor=black@0.45:x=20:y=18")
            parts.append(
                "drawtext={fo}text='%{{localtime}}':fontcolor=white:fontsize=30:"
                "box=1:boxcolor=black@0.45:x=w-tw-20:y=18")
        vf = ",".join(parts).format(w=w, h=h, fps=v["fps"], fo=fo, mark=mark, name=name)
        return self.cfg["ffmpeg_cmd"] + [
            "-hide_banner", "-loglevel", "error", "-re",
        ] + src + [
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p",
            "-g", str(v["gop"]), "-b:v", v["bitrate"],
            "-an",
            "-f", "vob", "-",
        ]

    def _open_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.cfg["local_ip"], 0))
        except Exception:
            pass
        return s

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, name="push-%s" % self.ch["id"])
        self.thread.daemon = True
        self.thread.start()

    def _run(self):
        args = self._ffmpeg_args()
        log("推流启动 -> %s:%d  (ssrc=0x%08x)  %s" %
            (self.dst[0], self.dst[1], self.ssrc, self.ch["name"]))
        if self.verbose:
            log("ffmpeg: " + " ".join(args))
        # 同一通道的上一条 ffmpeg 可能还残留在容器里 (被 BYE / 重试打断), 先清掉,
        # 否则两路编码同一素材会白烧 CPU 并互相抢显存/线程
        leftover = kill_container_ffmpeg(self.cfg, self._needles(), quiet=True)
        if leftover:
            log("同通道残留 ffmpeg 已清理 %d 个 (%s)" % (leftover, self.ch["name"]))
        try:
            # ffmpeg stderr 落盘，便于排查滤镜/输入问题 (跟着脚本所在目录走)
            err_path = os.path.join(
                SIM_DIR, "ffmpeg_%s.err" % (self.ch["id"][-4:],))
            self.errf = open(err_path, "wb")
            self.proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=self.errf, bufsize=0)
        except Exception as e:
            log("!! ffmpeg 启动失败: %s" % e)
            self.running = False
            return

        self.sock = self._open_socket()
        fd = self.proc.stdout.fileno()
        self._ps_buf = b""
        last_stat = time.time()
        while self.running:
            try:
                r, _, _ = select.select([fd], [], [], 1.0)
                if not r:
                    if self.proc.poll() is not None:
                        break
                    continue
                data = os.read(fd, 16384)
                if not data:
                    break
            except Exception:
                break
            self._ps_buf += data
            # 按 PS pack 边界切帧发送 (时间戳取帧内 PES 的 PTS, M 位置帧尾)
            self._flush_packs(final=False)
            if time.time() - last_stat >= 10:
                last_stat = time.time()
                log("推流中 %s: 累计 %d 包 / %.2f MB" %
                    (self.ch["name"], self.pkts_sent, self.bytes_sent / 1048576.0))
        self._flush_packs(final=True)

        self._stop_proc()
        if self.sock:
            self.sock.close()
            self.sock = None
        # ffmpeg 异常退出时，把 stderr 末尾写进日志
        if self.proc is not None and self.proc.returncode not in (0, None):
            try:
                self.errf.flush()
                with open(self.errf.name, "rb") as f:
                    tail = f.read()[-600:].decode("utf-8", "replace")
                if tail.strip():
                    log("!! ffmpeg 退出码 %s: %s" % (self.proc.returncode, tail))
            except Exception:
                pass
        try:
            self.errf.close()
        except Exception:
            pass
        log("推流结束 %s (%d 包 / %.2f MB)" %
            (self.ch["name"], self.pkts_sent, self.bytes_sent / 1048576.0))

    # ---- GB28181 MPEG-2 PS 重建所需的静态头 (与真实国标设备字节模式一致) ----
    # MPEG-2 pack 头 (14B): 00 00 01 BA + '0100' + SCR + mux_rate + stuffing
    _PS_PACK = bytes.fromhex("000001ba4400040004018666cff8")
    # 系统头 (15B): 视频流 E0, bounds 取 ffmpeg vob 同款
    _PS_SYS = bytes.fromhex("000001bb0009c3336700 21ffe0e0e6".replace(" ", ""))

    @staticmethod
    def _crc_mpeg2(data):
        """MPEG-2 CRC_32 (poly 0x04C11DB7, init 全 1, 无反射)."""
        crc = 0xFFFFFFFF
        for b in data:
            crc ^= b << 24
            for _ in range(8):
                if crc & 0x80000000:
                    crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                else:
                    crc = (crc << 1) & 0xFFFFFFFF
        return crc

    @classmethod
    def _make_psm(cls):
        """构造 PSM (00 00 01 BC): stream_type 0x1B (H.264) -> elementary_stream_id E0.

        ffmpeg 的 mpeg/vob 封装器都不写 PSM, 收流端 (ZLM) 无法从 stream_id 得知
        视频编码类型 —— 真实国标设备每个 IDR 前都会发 PSM, 这里每帧都发。
        """
        body = bytearray([0xE1, 0xFF])          # marker+cni+version / reserved+marker
        body += b"\x00\x00"                      # program_stream_info_length = 0
        body += b"\x00\x04"                      # elementary_stream_map_length = 4
        body += b"\x1b\xe0\x00\x00"              # H.264 -> E0, es_info_length = 0
        crc = cls._crc_mpeg2(bytes(body))
        return (b"\x00\x00\x01\xbc" +
                (len(body) + 4).to_bytes(2, "big") + bytes(body) +
                crc.to_bytes(4, "big"))

    _PS_PSM = None      # 类级缓存 (首次使用时构造)

    @staticmethod
    def _ts_encode(v):
        """90kHz 时戳 -> 5 字节 PTS ('0010' + 33 位 + 4 个标记位)."""
        return (bytes([(0x2 << 4) | (((v >> 30) & 0x07) << 1) | 1]) +
                ((((v >> 15) & 0x7FFF) << 1) | 1).to_bytes(2, "big") +
                (((v & 0x7FFF) << 1) | 1).to_bytes(2, "big"))

    @staticmethod
    def _decode_ts(b):
        """5 字节 PTS -> 90kHz 时戳。"""
        return ((((b[0] >> 1) & 0x07) << 30) |
                (int.from_bytes(b[1:3], "big") >> 1) << 15 |
                (int.from_bytes(b[3:5], "big") >> 1))

    def _pes_header(self, ts, es_len):
        """重建 MPEG-2 视频 PES 头: E0 + 长度 + flags(80 80) + hdrlen(5) + PTS。"""
        plen = 8 + es_len                     # flags3B + PTS5B + ES
        if plen > 0xFFFF:
            plen = 0                          # 不定长 PES (PS 视频流允许)
        return (b"\x00\x00\x01\xe0" + plen.to_bytes(2, "big") +
                bytes([0x80, 0x80, 5]) + self._ts_encode(ts))

    def _pes_unit_parse(self, unit):
        """解析一个完整视频 PES 单元 (00 00 01 Ex 起, 至下一起始码前)。

        自动兼容两种 PES 头布局:
          MPEG-2: [起始码4][len2][flags1 '10xx'][flags2][hdrlen][可选字段][载荷]
          MPEG-1: [起始码4][len2][0xFF 填充...]['0010'PTS / '0011'PTS+DTS / 0x0f][载荷]
        返回 (pts|None, payload)。
        """
        n = len(unit)
        p = 6                                   # 跳过起始码 + PES_packet_length
        if n <= p:
            return None, b""
        pts = None
        if (unit[p] & 0xC0) == 0x80 and n >= p + 3:
            # MPEG-2: flags1 高 2 位 '10'
            flags2, hlen = unit[p + 1], unit[p + 2]
            q = p + 3                           # 可选字段区 (PTS 在最前)
            if (flags2 & 0xC0) == 0x80 and n >= q + 5:      # '10' 仅 PTS
                pts = self._decode_ts(unit[q:q + 5])
            elif (flags2 & 0xC0) == 0xC0 and n >= q + 10:   # '11' PTS+DTS
                pts = self._decode_ts(unit[q:q + 5])
            payload_off = p + 3 + hlen
        else:
            # MPEG-1: 先跳过 0xFF 填充, 再看可选时戳
            q = p
            while q < n and unit[q] == 0xFF:
                q += 1
            if q < n and (unit[q] >> 4) == 0x2 and n >= q + 5:      # '0010' PTS
                pts = self._decode_ts(unit[q:q + 5])
                payload_off = q + 5
            elif q < n and (unit[q] >> 4) == 0x3 and n >= q + 10:  # '0011' PTS+DTS
                pts = self._decode_ts(unit[q:q + 5])
                payload_off = q + 10
            else:                                                    # 0x0f 等无时戳
                payload_off = q + 1 if q < n else q
        if payload_off > n:
            payload_off = n
        return pts, unit[payload_off:]

    def _flush_packs(self, final=False):
        """解析 ffmpeg 输出的 PS 字节流, 按帧重组 ES, 重建 GB28181 MPEG-2 PS 发送。

        ffmpeg -f mpeg 输出的其实是 MPEG-1 系统流: MPEG-1 pack 头 (10B 布局)、
        视频 stream_id 是 0xE2 而非 E0、MPEG-1 PES 头 (无 flags/hdrlen 三字节)、
        每帧拆成 ~2048 字节分片、且没有 PSM —— 与 GB28181 要求的 MPEG-2 PS 完全
        不同, ZLM 按 MPEG-2 解析只能侥幸解出第一帧, 之后画面冻结。

        这里逐起始码扫描 (ES 载荷因 H.264 emulation prevention 不会出现
        00 00 01 xx>=0x80 的假起始码), 把每帧的 ES 分片拼回完整帧, 再按
        [BA pack 头 + BB 系统头 + BC PSM + E0 PES(MPEG-2 头+PTS)] 重新封装,
        与海康/大华等真实国标设备的字节模式一致。带 PTS 的 PES 即新帧起点。
        """
        if self._PS_PSM is None:
            type(self)._PS_PSM = self._make_psm()
        buf = self._ps_buf
        if not buf:
            if final:
                self._emit_frame()
            return
        # 1. 找出所有包边界: 00 00 01 + 第4字节 >= 0xB9 (系统码/视频PES)
        starts = []
        i = buf.find(b"\x00\x00\x01")
        while i >= 0:
            b = buf[i + 3] if i + 3 < len(buf) else -1
            if b >= 0xB9:
                starts.append(i)
            i = buf.find(b"\x00\x00\x01", i + 3)
        if not starts:
            if len(buf) > 4 * 1024 * 1024:     # 异常流无边界, 防内存膨胀
                self._ps_buf = b""
            if final:
                self._emit_frame()
            return
        # 2. 完整单元 = [starts[k], starts[k+1]); 末尾未闭合的留在缓冲等下批
        n_units = len(starts) - (0 if final else 1)
        for k in range(n_units):
            s = starts[k]
            e = starts[k + 1] if k + 1 < len(starts) else len(buf)
            u = buf[s:e]
            sid = u[3] if len(u) > 3 else 0
            if 0xE0 <= sid <= 0xEF:            # 视频 PES (ffmpeg 用 E0-EF, 我们重建为 E0)
                pts, payload = self._pes_unit_parse(u)
                if pts is not None:
                    if self._cur_es:           # 上一帧收尾
                        self._emit_frame()
                    self._cur_pts = pts
                    self._cur_es = [payload]
                elif self._cur_es:             # 续片 (无 PTS), 累积到当前帧
                    self._cur_es.append(payload)
            # BA/BB/BC/BE/BF/B9/BD 等系统单元: 跳过, 重建时自行生成
        self._ps_buf = b"" if final else buf[starts[-1]:]
        if final:
            self._emit_frame()

    def _emit_frame(self):
        """把累积好的当前帧重新封装成 GB28181 MPEG-2 PS 并切 RTP 发送。"""
        if not self._cur_es:
            return
        es = b"".join(self._cur_es)
        pts_in = self._cur_pts
        self._cur_es, self._cur_pts = [], None
        if self._PS_PSM is None:
            type(self)._PS_PSM = self._make_psm()
        ts = self._ts_from_pts(pts_in) & 0xFFFFFFFF
        frame = (self._PS_PACK + self._PS_SYS + self._PS_PSM +
                 self._pes_header(ts, len(es)) + es)
        self._send_frame(ts, frame)

    def _ts_from_pts(self, pts):
        """RTP/PES 共用时间戳: 输入 PTS 单调化; 无 PTS 按 90000/fps 递增。"""
        try:
            step = int(90000 / max(1, int(
                (self.cfg.get("video") or {}).get("fps", 25))))
        except Exception:
            step = 3600
        if pts is not None:
            cand = pts + self._pts_off
            if self._rtp_ts is not None and cand <= self._rtp_ts:
                # 时间戳停滞/回跳 (素材 stream_loop 重绕) -> 抬偏移保持严格递增
                self._pts_off += self._rtp_ts - cand + step
                cand = pts + self._pts_off
            self._rtp_ts = cand
        else:
            self._rtp_ts = (self._rtp_ts or 0) + step
        return self._rtp_ts

    def _send_frame(self, ts, frame):
        """发送一个重建好的 PS 帧: 帧内 RTP 包共用时间戳, 帧尾置 M。"""
        MTU = 1400
        n = len(frame)
        off = 0
        pkt_i = 0
        while off < n:
            chunk = frame[off:off + MTU]
            last = off + MTU >= n
            hdr = bytearray(12)
            hdr[0] = 0x80                       # V=2, P=0, X=0, CC=0
            hdr[1] = 96 | (0x80 if last else 0x00)   # M 只在帧尾最后一个包置位
            hdr[2] = (self.seq >> 8) & 0xFF
            hdr[3] = self.seq & 0xFF
            hdr[4] = (ts >> 24) & 0xFF
            hdr[5] = (ts >> 16) & 0xFF
            hdr[6] = (ts >> 8) & 0xFF
            hdr[7] = ts & 0xFF
            hdr[8] = (self.ssrc >> 24) & 0xFF
            hdr[9] = (self.ssrc >> 16) & 0xFF
            hdr[10] = (self.ssrc >> 8) & 0xFF
            hdr[11] = self.ssrc & 0xFF
            try:
                self.sock.sendto(bytes(hdr) + chunk, self.dst)
            except Exception:
                return
            self.seq = (self.seq + 1) & 0xFFFF
            self.pkts_sent += 1
            self.bytes_sent += len(chunk)
            off += MTU
            pkt_i += 1
            # 大帧 (关键帧) 平滑发送, 避免背靠背突发打爆接收端 UDP 缓冲
            # 造成丢包卡帧; 16 包 1ms
            if n > 65536 and pkt_i % 16 == 0:
                time.sleep(0.001)

    def _needles(self):
        """本通道可用于识别自身 ffmpeg 的命令行特征 (字面量, 非正则)."""
        out = []
        if self.ch.get("video"):
            out.append(self.ch["video"])
        if self.ch.get("rtsp"):
            out.append(self.ch["rtsp"])
        return out

    def _stop_proc(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        # 容器内 ffmpeg 可能残留 (docker exec 的客户端被 kill 不会终止容器内
        # 进程), 必须按命令行特征精确清理.
        # 历史实现用 `pkill -f "stream_loop -1 -i <路径>"`: pkill 走 ERE, 而设备名
        # 里带 ( ) 等元字符 → 模式匹配不上 → 每次重试泄漏一个 ffmpeg, 累积到上百个
        # 后 CPU 打满, 新推流一帧都编不出来 → WVP 报「收流超时」并继续重试 →
        # 正反馈雪崩. 改为 ps + 字面量包含判断 + 精确 kill.
        kill_container_ffmpeg(self.cfg, self._needles())

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# 自动补拉 (持续推流测试)
# --------------------------------------------------------------------------- #
class WvpReplayer(object):
    """巡检线程: 通道没有活跃推流时, 调 WVP 点播接口触发一次 INVITE。

    设备端是被叫方, 会话被 BYE (播放器关闭 / 无人观看回收 / 平台重启) 后
    自己无法主动恢复 —— 通过 WVP Web API 重新点播即可拉起。
    """

    def __init__(self, cfg, device):
        self.cfg = cfg
        self.device = device
        ar = cfg["auto_replay"]
        self.base = ar["wvp_base"].rstrip("/")
        self.user = ar["username"]
        self.password = ar["password"]
        self.interval = max(5, int(ar.get("interval", 60)))
        self.cooldown = max(0, int(ar.get("cooldown", 180)))
        self.token = None
        self.last_try = {}
        self.running = True

    # ---- WVP Web API ----
    def _http_get(self, path, headers=None):
        url = self.base + path
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=15) as r:
                return 0, r.read().decode("utf-8", "replace")
        except Exception as e:
            return -1, str(e)

    def _login(self):
        """登录 WVP 拿 token: 先试 md5 密码, 再试明文 (与 wvp_api.sh 一致)。
        返回 (ok, 失败原因)。"""
        q = urllib.parse.urlencode({"username": self.user})
        detail = "未知"
        for pwd in (hashlib.md5(self.password.encode()).hexdigest(), self.password):
            code, out = self._http_get(
                "/api/user/login?%s&password=%s" % (q, urllib.parse.quote(pwd, safe="")))
            if code != 0:
                detail = "连不上 WVP (%s), 检查 auto_replay.wvp_base" % out[:100]
                continue
            try:
                d = json.loads(out)
            except Exception:
                detail = "登录响应非 JSON: %s" % out[:100]
                continue
            if d.get("code") != 0:
                detail = "WVP 拒绝登录 code=%s msg=%s (检查 username/password)" % (
                    d.get("code"), d.get("msg"))
                continue
            v = d.get("data")
            token = None
            if isinstance(v, str):
                token = v
            elif isinstance(v, dict):
                # 各版本 WVP 登录返回的字段名不同:
                # 新版 data.accessToken / 官方文档 data.access-token / 旧版 data.token
                token = v.get("accessToken") or v.get("access-token") or v.get("token")
            if token:
                self.token = token
                return True, ""
            detail = "登录成功但响应无 token 字段"
        return False, detail

    def _start_play(self, ch_id):
        """请求 WVP 对某通道发起点播 (会触发一通新的 INVITE)。
        返回 (ok, 失败原因) —— 原因会写进日志, 方便定位。"""
        for _ in range(2):                   # token 失效时重登后再试一次
            if not self.token:
                ok, why = self._login()
                if not ok:
                    return False, "WVP 登录失败: %s" % why
            code, out = self._http_get(
                "/api/play/start/%s/%s" % (self.cfg["device_id"], ch_id),
                {"access-token": self.token})
            if code != 0:
                return False, "HTTP 请求失败: %s" % out[:120]
            try:
                j = json.loads(out)
            except Exception:
                return False, "响应非 JSON: %s" % out[:120]
            if j.get("code") == 0:
                return True, ""
            msg = str(j.get("msg", ""))
            if j.get("code") in (401, 403) or "token" in msg.lower():
                self.token = None            # 失效 -> 重登重试
                continue
            return False, "WVP code=%s msg=%s" % (j.get("code"), msg or "(空)")
        return False, "token 失效且重登后仍失败"

    # ---- 巡检主循环 ----
    def run(self):
        log("自动补拉已开启: 周期=%ds 冷却=%ds WVP=%s"
            % (self.interval, self.cooldown, self.base))
        while self.running:
            time.sleep(self.interval)
            if not self.running or not self.device.registered:
                continue
            active = set()
            for p in list(self.device.sessions.values()):
                if p.running:
                    active.add(p.ch["id"])
            now = time.time()
            for ch in self.cfg["channels"]:
                cid = ch["id"]
                if cid in active:
                    self.last_try.pop(cid, None)
                    continue
                if now - self.last_try.get(cid, 0) < self.cooldown:
                    continue
                self.last_try[cid] = now
                ok, why = self._start_play(cid)
                log("自动补拉 %s -> %s" % (
                    ch["name"],
                    "已请求 WVP 点播" if ok else "失败: %s (冷却后重试)" % why))

    def stop(self):
        self.running = False


# --------------------------------------------------------------------------- #
# 设备主体
# --------------------------------------------------------------------------- #
class SimDevice(object):

    def __init__(self, cfg, verbose=False):
        self.cfg = cfg
        self.verbose = verbose
        self.sock = None
        self.cseq = 1
        self.sn = 1
        self.call_id_base = md5_hex(str(random.random()))[:20]
        self.sessions = {}       # call_id -> RtpPusher
        self.pending = {}        # call_id -> dict(invite info)
        self.replayer = None     # 自动补拉巡检线程 (auto_replay.enabled 时创建)
        self.running = True
        self.last_register = 0
        self.last_keepalive = 0
        self.registered = False
        self.ww_auth = None

    # ---------------- 网络 ----------------
    def open_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.cfg["local_port"]))
        s.settimeout(1.0)
        self.sock = s
        log("SIP 监听 %s:%d (UDP)" % (self.cfg["local_ip"], self.cfg["local_port"]))

    def send(self, payload, addr=None):
        self.sock.sendto(payload, addr or (self.cfg["server_ip"], self.cfg["server_port"]))
        if self.verbose:
            log(">>>\n" + payload.decode("utf-8", "replace"))

    def next_cseq(self):
        self.cseq += 1
        return self.cseq

    def new_call_id(self):
        return "%s@%s" % (md5_hex("%s%d" % (self.call_id_base, time.time())), self.cfg["local_ip"])

    def sip_uri(self):
        return "sip:%s@%s" % (self.cfg["device_id"], self.cfg["domain"])

    def contact(self):
        return "<sip:%s@%s:%d>" % (self.cfg["device_id"], self.cfg["local_ip"],
                                   self.cfg["local_port"])

    # ---------------- REGISTER ----------------
    def register(self):
        call_id = self.new_call_id()
        cseq = self.next_cseq()
        hdrs = [
            ("Via", "SIP/2.0/UDP %s:%d;rport;branch=z9hG4bK%s" %
             (self.cfg["local_ip"], self.cfg["local_port"], md5_hex(str(time.time()))[:12])),
            ("From", "<%s>;tag=%s" % (self.sip_uri(), md5_hex("from")[:12])),
            ("To", "<%s>" % self.sip_uri()),
            ("Call-ID", call_id),
            ("CSeq", "%d REGISTER" % cseq),
            ("Contact", self.contact()),
            ("Max-Forwards", "70"),
            ("User-Agent", "SimGB28181/1.0"),
            ("Expires", str(self.cfg["register_expires"])),
        ]
        if self.ww_auth:
            hdrs.append(("Authorization", self._digest_header("REGISTER",
                                                              "sip:%s@%s" % (self.cfg["domain"], self.cfg["domain"]))))
        msg = build_message("REGISTER sip:%s@%s SIP/2.0" % (self.cfg["domain"], self.cfg["domain"]), hdrs)
        self._register_call_id = call_id
        self.send(msg)
        log("发送 REGISTER (cseq=%d)%s" % (cseq, " [带鉴权]" if self.ww_auth else ""))

    def _digest_header(self, method, uri):
        a = self.ww_auth
        realm = a.get("realm", self.cfg["domain"])
        nonce = a.get("nonce", "")
        qop = a.get("qop", "")
        user = self.cfg["device_id"]
        pwd = self.cfg["password"]
        ha1 = md5_hex("%s:%s:%s" % (user, realm, pwd))
        ha2 = md5_hex("%s:%s" % (method, uri))
        if qop:
            nc = "00000001"
            cnonce = md5_hex(str(random.random()))[:16]
            resp = md5_hex("%s:%s:%s:%s:%s:%s" % (ha1, nonce, nc, cnonce, qop, ha2))
            return ('Digest username="%s", realm="%s", nonce="%s", uri="%s", '
                    'response="%s", qop=%s, nc=%s, cnonce="%s", algorithm=MD5'
                    % (user, realm, nonce, uri, resp, qop, nc, cnonce))
        resp = md5_hex("%s:%s:%s" % (ha1, nonce, ha2))
        return ('Digest username="%s", realm="%s", nonce="%s", uri="%s", '
                'response="%s", algorithm=MD5' % (user, realm, nonce, uri, resp))

    # ---------------- 心跳 ----------------
    def keepalive(self):
        body = ('<?xml version="1.0" encoding="%s"?>\r\n'
                '<Notify>\r\n<CmdType>Keepalive</CmdType>\r\n<SN>%d</SN>\r\n'
                '<DeviceID>%s</DeviceID>\r\n<Status>OK</Status>\r\n</Notify>\r\n'
                % (self.cfg["charset"], self.next_sn(), self.cfg["device_id"]))
        self.send_message(body)
        log("发送 Keepalive")

    def next_sn(self):
        self.sn += 1
        return self.sn

    def send_message(self, xml_body):
        uri = "sip:%s@%s" % (self.cfg["server_id"], self.cfg["domain"])
        data = xml_body.encode(self.cfg["charset"], "replace")
        hdrs = [
            ("Via", "SIP/2.0/UDP %s:%d;rport;branch=z9hG4bK%s" %
             (self.cfg["local_ip"], self.cfg["local_port"], md5_hex(str(time.time()))[:12])),
            ("From", "<%s>;tag=%s" % (self.sip_uri(), md5_hex("from")[:12])),
            ("To", "<%s>" % uri),
            ("Call-ID", self.new_call_id()),
            ("CSeq", "%d MESSAGE" % self.next_cseq()),
            ("Max-Forwards", "70"),
            ("User-Agent", "SimGB28181/1.0"),
            ("Content-Type", "Application/MANSCDP+xml"),
            ("Content-Length", str(len(data))),
        ]
        raw = CRLF.join(["MESSAGE %s SIP/2.0" % uri] +
                        ["%s: %s" % (k, v) for k, v in hdrs] + ["", ""]).encode("utf-8")
        self.send(raw + data)

    # ---------------- 目录 ----------------
    def catalog_response(self, sn, device_id):
        items = []
        for ch in self.cfg["channels"]:
            items.append(
                "<Item>\r\n"
                "<DeviceID>%s</DeviceID>\r\n"
                "<Name>%s</Name>\r\n"
                "<Manufacturer>%s</Manufacturer>\r\n"
                "<Model>%s</Model>\r\n"
                "<Owner>Owner</Owner>\r\n"
                "<CivilCode>440102</CivilCode>\r\n"
                "<Address>广东省广州市</Address>\r\n"
                "<Parental>0</Parental>\r\n"
                "<ParentID>%s</ParentID>\r\n"
                "<SafetyWay>0</SafetyWay>\r\n"
                "<RegisterWay>1</RegisterWay>\r\n"
                "<Secrecy>0</Secrecy>\r\n"
                "<Status>ON</Status>\r\n"
                "</Item>\r\n"
                % (ch["id"], xml_escape(ch["name"]), self.cfg["manufacturer"],
                   self.cfg["model"], device_id))
        body = ('<?xml version="1.0" encoding="%s"?>\r\n'
                '<Response>\r\n<CmdType>Catalog</CmdType>\r\n<SN>%d</SN>\r\n'
                '<DeviceID>%s</DeviceID>\r\n<SumNum>%d</SumNum>\r\n'
                '<DeviceList Num="%d">\r\n%s</DeviceList>\r\n</Response>\r\n'
                % (self.cfg["charset"], sn, device_id, len(items), len(items),
                   "".join(items)))
        self.send_message(body)
        log("上报目录：%d 个通道" % len(items))

    def device_info_response(self, sn, device_id):
        body = ('<?xml version="1.0" encoding="%s"?>\r\n'
                '<Response>\r\n<CmdType>DeviceInfo</CmdType>\r\n<SN>%d</SN>\r\n'
                '<DeviceID>%s</DeviceID>\r\n<Result>OK</Result>\r\n'
                '<DeviceName>%s</DeviceName>\r\n<Manufacturer>%s</Manufacturer>\r\n'
                '<Model>%s</Model>\r\n<Firmware>V1.0</Firmware>\r\n'
                '<Channel>%d</Channel>\r\n</Response>\r\n'
                % (self.cfg["charset"], sn, device_id, self.cfg["device_name"],
                   self.cfg["manufacturer"], self.cfg["model"],
                   len(self.cfg["channels"])))
        self.send_message(body)
        log("响应 DeviceInfo 查询")

    def device_status_response(self, sn, device_id):
        body = ('<?xml version="1.0" encoding="%s"?>\r\n'
                '<Response>\r\n<CmdType>DeviceStatus</CmdType>\r\n<SN>%d</SN>\r\n'
                '<DeviceID>%s</DeviceID>\r\n<Result>OK</Result>\r\n'
                '<Online>ONLINE</Online>\r\n<Status>OK</Status>\r\n'
                '<Encode>ON</Encode>\r\n<Record>OFF</Record>\r\n'
                '</Response>\r\n' % (self.cfg["charset"], sn, device_id))
        self.send_message(body)
        log("响应 DeviceStatus 查询")

    # ---------------- 收包处理 ----------------
    def _on_message(self, headers, body):
        try:
            self.send(build_response("MESSAGE sip:%s SIP/2.0" % self.cfg["domain"],
                                     headers, 200, "OK"))
        except Exception:
            pass
        if "<CmdType>Catalog</CmdType>" in body:
            sn = self._pick(body, "SN", "1")
            dev = self._pick(body, "DeviceID", self.cfg["device_id"])
            self.catalog_response(int(sn or 1), dev)
        elif "<CmdType>DeviceInfo</CmdType>" in body:
            sn = self._pick(body, "SN", "1")
            dev = self._pick(body, "DeviceID", self.cfg["device_id"])
            self.device_info_response(int(sn or 1), dev)
        elif "<CmdType>DeviceStatus</CmdType>" in body:
            sn = self._pick(body, "SN", "1")
            dev = self._pick(body, "DeviceID", self.cfg["device_id"])
            self.device_status_response(int(sn or 1), dev)

    @staticmethod
    def _pick(xml, tag, default):
        m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), xml)
        return m.group(1).strip() if m else default

    def _on_invite(self, headers, body, start):
        call_id = headers.get("call-id", "")
        # 100 Trying
        try:
            self.send(build_response(start, headers, 100, "Trying"))
        except Exception:
            pass
        # 解析 SDP
        ip = self._pick_sdp(body, "c") or self.cfg["server_ip"]
        port = self._pick_sdp(body, "m")
        ssrc = 0
        m = re.search(r"y=(\d+)", body)
        if m:
            ssrc = int(m.group(1))
        if not port:
            log("!! INVITE 未解析到 m= 端口，忽略")
            return
        # 反查通道：优先用 To 头里的通道编号
        ch = self._match_channel("%s %s" % (headers.get("to", ""), start))
        if ssrc == 0 and ch:
            ssrc = self._ssrc_of(ch)
        log("收到 INVITE -> 推流目标 %s:%s (ssrc=0x%08x, channel=%s)"
            % (ip, port, ssrc, ch["name"] if ch else "?"))

        # 200 OK + SDP
        our_sdp = ("v=0\r\n"
                   "o=%s 0 0 IN IP4 %s\r\n"
                   "s=Play\r\n"
                   "c=IN IP4 %s\r\n"
                   "t=0 0\r\n"
                   "m=video %d RTP/AVP 96\r\n"
                   "a=sendonly\r\n"
                   "a=rtpmap:96 PS/90000\r\n"
                   "y=%d\r\n"
                   "f=v/2/5/25/1/4000\r\n"
                   % (self.cfg["device_id"], self.cfg["local_ip"],
                      self.cfg["local_ip"], self.cfg["local_port"], ssrc))
        extra = {"Contact": self.contact(),
                 "Content-Type": "application/sdp"}
        ok = build_response(start, headers, 200, "OK", extra=extra, body=our_sdp)
        # build_response 用 utf-8 计算 Content-Length，SDP 为纯 ASCII，安全
        self.send(ok)

        # 记录会话，等 ACK 后开推
        self.pending[call_id] = {"ch": ch, "ip": ip, "port": port, "ssrc": ssrc,
                                 "t": time.time()}

    def _on_bye(self, headers):
        call_id = headers.get("call-id", "")
        try:
            self.send(build_response("BYE sip:%s SIP/2.0" % self.cfg["domain"],
                                     headers, 200, "OK"))
        except Exception:
            pass
        log("收到 BYE，停止推流 %s" % call_id)
        self.pending.pop(call_id, None)
        p = self.sessions.pop(call_id, None)
        if p:
            threading.Thread(target=p.stop).start()

    def _on_ack(self, headers):
        call_id = headers.get("call-id", "")
        info = self.pending.pop(call_id, None)
        if not info:
            return
        ch = info["ch"]
        if not ch:
            log("!! ACK 但未匹配到通道，跳过")
            return
        if call_id in self.sessions:
            return
        pusher = RtpPusher(self.cfg, ch, info["ip"], info["port"], info["ssrc"],
                           verbose=self.verbose)
        self.sessions[call_id] = pusher
        pusher.start()
        log("已开始推送 %s -> %s:%s" % (ch["name"], info["ip"], info["port"]))

    def _match_channel(self, needle):
        for ch in self.cfg["channels"]:
            if ch["id"] in needle:
                return ch
        return self.cfg["channels"][0]

    @staticmethod
    def _ssrc_of(ch):
        # 通道号末 4 位作为 SSRC 低位，模拟真实设备
        try:
            return (0x0A000000 | (int(ch["id"][-4:]) & 0x00FFFFFF)) & 0xFFFFFFFF
        except Exception:
            return random.randint(1, 0x7FFFFFFF)

    @staticmethod
    def _pick_sdp(sdp, kind):
        for line in sdp.replace("\r\n", "\n").split("\n"):
            if kind == "c" and line.startswith("c=IN IP4 "):
                return line.split()[2]
            if kind == "m" and line.startswith("m=video "):
                return line.split()[1]
        return None

    # ---------------- 主循环 ----------------
    def run(self):
        self.open_socket()
        self.register()
        self.last_register = self.last_keepalive = time.time()
        # 自动补拉 (持续推流测试): 巡检无流通道, 调 WVP 点播接口重新拉起
        if self.cfg.get("auto_replay", {}).get("enabled"):
            self.replayer = WvpReplayer(self.cfg, self)
            threading.Thread(target=self.replayer.run, daemon=True,
                             name="auto-replay").start()
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
                if data:
                    self._dispatch(data, addr)
            except socket.timeout:
                pass
            except Exception as e:
                log("!! 收包异常: %s" % e)

            now = time.time()
            # 重新注册（有效期过半）
            if now - self.last_register > self.cfg["register_expires"] * 0.5:
                self.register()
                self.last_register = now
            # 心跳
            if now - self.last_keepalive > self.cfg["keepalive_interval"]:
                self.keepalive()
                self.last_keepalive = now
            # 处理未收到 ACK 的会话（兜底：2s 后直接开推）
            for cid, info in list(self.pending.items()):
                if now - info["t"] > 2.0:
                    self.pending.pop(cid, None)
                    if cid not in self.sessions and info["ch"]:
                        pusher = RtpPusher(self.cfg, info["ch"], info["ip"],
                                           info["port"], info["ssrc"], self.verbose)
                        self.sessions[cid] = pusher
                        pusher.start()
                        log("(未收到 ACK，超时兜底) 开始推送 %s" % info["ch"]["name"])

    def _dispatch(self, data, addr):
        start, headers, raw, body = parse_sip(data)
        if not start:
            return
        # 把响应状态码补进 headers，便于处理
        method = start.split()[0].upper()
        if method == "SIP/2.0":
            if self.verbose:
                log("<<< 响应: %s" % start)
            self._handle_sip_response(start, headers)
            return
        if self.verbose:
            log("<<< %s  (%s:%d)\n%s" % (method, addr[0], addr[1],
                                         data.decode("utf-8", "replace")))
        if method == "MESSAGE":
            self._on_message(headers, body)
        elif method == "INVITE":
            self._on_invite(headers, body, start)
        elif method == "ACK":
            self._on_ack(headers)
        elif method == "BYE":
            self._on_bye(headers)
        else:
            try:
                self.send(build_response(start, headers, 200, "OK"), addr)
            except Exception:
                pass

    def _handle_sip_response(self, start, headers):
        code = int(start.split()[1]) if len(start.split()) > 1 else 0
        cseq = headers.get("cseq", "")
        m = re.match(r"(\d+)\s+(\w+)", cseq)
        method = m.group(2).upper() if m else ""
        if method == "REGISTER":
            if code == 401 or code == 407:
                hdr = headers.get("www-authenticate") or headers.get("proxy-authenticate")
                self.ww_auth = parse_auth(hdr)
                log("收到 401 质询 (realm=%s)，带鉴权重发" % self.ww_auth.get("realm"))
                self.register()
            elif code == 200:
                self.registered = True
                log("注册成功 ✔ (%s)" % self.cfg["device_id"])
            else:
                log("!! 注册响应 %d" % code)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="JSON 配置文件路径")
    ap.add_argument("--verbose", action="store_true", help="打印完整 SIP 报文")
    ap.add_argument("--server-ip")
    ap.add_argument("--device-id")
    ap.add_argument("--password")
    ap.add_argument("--local-ip")
    ap.add_argument("--check", action="store_true",
                    help="只做推流环境自检 (ffmpeg/媒体/字体), 不启动 SIP 注册")
    ap.add_argument("--keep", action="store_true",
                    help="开启自动补拉: 通道无流时自动调 WVP 点播接口, 持续推流")
    args = ap.parse_args()

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if args.config and os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            user = json.load(f)
        # 深合并: channels 按通道合并 (保留默认字段), video 做 dict 合并,
        # 避免手工 sim.json 只写部分字段把默认值整体冲掉
        for k, v in user.items():
            if k == "channels" and isinstance(v, list):
                merged = []
                for i, c in enumerate(v):
                    base = dict(cfg["channels"][i]) if i < len(cfg["channels"]) else {}
                    if isinstance(c, dict):
                        base.update(c)
                    merged.append(base)
                cfg["channels"] = merged
            elif k == "video" and isinstance(v, dict):
                cfg.setdefault("video", {}).update(v)
            elif k == "auto_replay" and isinstance(v, dict):
                cfg.setdefault("auto_replay", {}).update(v)
            else:
                cfg[k] = v
    for k in ("server_ip", "device_id", "password", "local_ip"):
        v = getattr(args, k)
        if v:
            cfg[k] = v
    if args.keep:
        cfg.setdefault("auto_replay", {})["enabled"] = True

    # 关键: 启动前确认 ffmpeg / 媒体 / 字体真正可用 (自动适配新容器名 + 自愈注入)
    resolve_ffmpeg_env(cfg)

    # 启动清场: 上一轮 sim 被强制结束时, 容器内 ffmpeg 不会随之退出 (docker exec
    # 客户端断开不影响容器内进程), 残留会累积打满 CPU 并让 WVP 持续报收流超时
    _stale = sweep_stale_ffmpeg(cfg)
    if _stale:
        log("启动清理: 已终止上一轮残留 ffmpeg %d 个" % _stale)

    if args.check:
        log("自检通过 ✔")
        log("  ffmpeg : %s" % " ".join(cfg["ffmpeg_cmd"]))
        for ch in cfg["channels"]:
            log("  通道 %-14s video=%s" % (ch.get("name") or ch["id"],
                                           ch.get("video") or "(testsrc2 测试源)"))
        log("  字体   : %s" % ((cfg.get("video") or {}).get("font")
                               or "无 (不叠加 OSD 字幕)"))
        return

    log("=" * 66)
    log("GB28181 下级设备模拟器")
    log("  上级平台: %s:%d   域=%s" % (cfg["server_ip"], cfg["server_port"], cfg["domain"]))
    log("  本设备  : %s   IP=%s:%d" % (cfg["device_id"], cfg["local_ip"], cfg["local_port"]))
    log("  通道数  : %d   %s" % (len(cfg["channels"]),
                                 ", ".join(c["name"] for c in cfg["channels"])))
    log("=" * 66)

    dev = SimDevice(cfg, verbose=args.verbose)
    # 兜底巡检: 防止任何异常路径下的 ffmpeg 孤儿累积打满 CPU
    start_ffmpeg_watchdog(dev)
    # 修正：ACK 处理已通过 _dispatch 路由
    # SIGTERM (simctl.sh stop 的 kill) 也走清理路径, 避免孤儿 ffmpeg 残留
    import signal

    def _term(_sig, _frm):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _term)
    except Exception:
        pass
    try:
        dev.run()
    except KeyboardInterrupt:
        log("退出，停止所有推流…")
        if dev.replayer:
            dev.replayer.stop()
        for p in list(dev.sessions.values()):
            p.stop()
        dev.running = False


if __name__ == "__main__":
    main()
