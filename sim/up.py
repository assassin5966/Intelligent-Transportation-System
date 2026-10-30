# -*- coding: utf-8 -*-
"""把 build/sim/ 下的文件上传到服务器 /home/sim/"""
import os
import sys
import posixpath
import paramiko

HOST = os.environ.get("ST_HOST", "10.10.10.11")
PORT = int(os.environ.get("ST_PORT", "22"))
USER = os.environ.get("ST_USER", "root")
PASSWORD = os.environ.get("ST_PASS", "")
if not PASSWORD:
    raise SystemExit("未设置环境变量 ST_PASS（SSH 密码）")
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = "/home/sim"


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, PORT, USER, PASSWORD, timeout=20,
                look_for_keys=False, allow_agent=False)
    # 目录准备（同时去掉 Windows 换行）
    stdin, stdout, stderr = ssh.exec_command("mkdir -p %s" % REMOTE_DIR)
    stdout.channel.recv_exit_status()

    sf = ssh.open_sftp()
    files = [f for f in sorted(os.listdir(LOCAL_DIR))
             if f.endswith((".py", ".json", ".sh"))]
    for name in files:
        local = os.path.join(LOCAL_DIR, name)
        raw = open(local, "rb").read().replace(b"\r\n", b"\n")
        remote = posixpath.join(REMOTE_DIR, name)
        with sf.open(remote, "wb") as f:
            f.write(raw)
        print("uploaded %-28s -> %s (%d bytes)" % (name, remote, len(raw)))
    sf.close()
    ssh.close()


if __name__ == "__main__":
    sys.exit(main())
