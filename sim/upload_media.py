# -*- coding: utf-8 -*-
"""上传道路监控视频素材到目标机 /home/sim/media/"""
import io
import os
import sys
import paramiko

HOST = "10.10.10.11"
PORT = 22
USER = "root"
PASSWORD = os.environ.get("ST_PASS", "")
if not PASSWORD:
    raise SystemExit("未设置环境变量 ST_PASS（SSH 密码）")
REMOTE_DIR = "/home/sim/media"

BASE = os.path.dirname(os.path.abspath(__file__))
FILES = ["cam1.mp4", "cam2.mp4", "cam3.mp4", "cam4.mp4"]


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, PORT, USER, PASSWORD, timeout=20,
                look_for_keys=False, allow_agent=False)
    ssh.exec_command("mkdir -p %s" % REMOTE_DIR)

    sf = ssh.open_sftp()
    total = 0
    for name in FILES:
        local = os.path.join(BASE, "media", name)
        if not os.path.exists(local):
            print("!! 本地缺失: %s" % local)
            continue
        remote = "%s/%s" % (REMOTE_DIR, name)
        sf.put(local, remote)
        size = os.path.getsize(local)
        total += size
        print("uploaded %-12s -> %s  (%.1f MB)" % (name, remote, size / 1048576.0))
    print("合计 %.1f MB" % (total / 1048576.0))

    # 跑一次上传后的确认
    stdin, stdout, stderr = ssh.exec_command("ls -la %s" % REMOTE_DIR)
    print(stdout.read().decode("utf-8", "replace"))
    sf.close()
    ssh.close()


if __name__ == "__main__":
    sys.exit(main())
