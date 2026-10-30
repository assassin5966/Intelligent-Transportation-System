# -*- coding: utf-8 -*-
"""通用文件拉取：python pull.py <remote> <local> [<remote> <local> ...]"""
import os
import sys
import paramiko

HOST = os.environ.get("ST_HOST", "10.10.10.11")
PORT = int(os.environ.get("ST_PORT", "22"))
USER = os.environ.get("ST_USER", "root")
PASSWORD = os.environ.get("ST_PASS", "")
if not PASSWORD:
    raise SystemExit("未设置环境变量 ST_PASS（SSH 密码）")


def main(argv):
    pairs = argv[1:]
    if not pairs or len(pairs) % 2:
        print("用法: pull.py <remote> <local> [<remote> <local> ...]")
        return 2
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, PORT, USER, PASSWORD, timeout=20,
                look_for_keys=False, allow_agent=False)
    sf = ssh.open_sftp()
    for i in range(0, len(pairs), 2):
        remote, local = pairs[i], pairs[i + 1]
        d = os.path.dirname(os.path.abspath(local))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        sf.get(remote, local)
        print("pulled %-46s -> %s (%d bytes)" % (remote, local, os.path.getsize(local)))
    sf.close()
    ssh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
