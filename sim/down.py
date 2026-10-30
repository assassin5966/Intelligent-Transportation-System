# -*- coding: utf-8 -*-
"""把服务器 /home/sim 下的截图下载到本地 build/sim/out/"""
import os
import paramiko

HOST = os.environ.get("ST_HOST", "10.10.10.11")
PORT = int(os.environ.get("ST_PORT", "22"))
USER = os.environ.get("ST_USER", "root")
PASSWORD = os.environ.get("ST_PASS", "")
if not PASSWORD:
    raise SystemExit("未设置环境变量 ST_PASS（SSH 密码）")
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
FILES = ["snap1.jpg", "snap2.jpg", "snap3.jpg", "snap4.jpg"]


def main():
    os.makedirs(LOCAL, exist_ok=True)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, PORT, USER, PASSWORD, timeout=20,
                look_for_keys=False, allow_agent=False)
    sf = ssh.open_sftp()
    for name in FILES:
        try:
            sf.get("/home/sim/" + name, os.path.join(LOCAL, name))
            print("downloaded %s (%d bytes)" % (name, os.path.getsize(os.path.join(LOCAL, name))))
        except Exception as e:
            print("fail %s: %s" % (name, e))
    sf.close()
    ssh.close()


if __name__ == "__main__":
    main()
