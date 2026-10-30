# -*- coding: utf-8 -*-
"""通用文件推送：python push.py <local> <remote> [<local> <remote> ...]"""
import io
import os
import sys
import time
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
        print("用法: push.py <local> <remote> [<local> <remote> ...]")
        return 2
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, PORT, USER, PASSWORD, timeout=20,
                look_for_keys=False, allow_agent=False)
    sf = ssh.open_sftp()
    try:
        print("远端 /home/sim 内容: %s" % sf.listdir("/home/sim"))
    except Exception as e:
        print("!! 无法列出 /home/sim: %s" % e)
    import tempfile
    for i in range(0, len(pairs), 2):
        local, remote = pairs[i], pairs[i + 1]
        with io.open(local, "r", encoding="utf-8", newline="") as f:
            data = f.read().replace("\r\n", "\n").replace("\r", "\n")
        tmp = os.path.join(tempfile.gettempdir(), os.path.basename(remote))
        with io.open(tmp, "wb") as f:
            f.write(data.encode("utf-8"))
        last = None
        for attempt in range(1, 5):
            try:
                # 该主机的 sftp-server 覆盖已有文件会报 ENOENT，先删除再新建
                try:
                    sf.remove(remote)
                except Exception:
                    pass
                with sf.open(remote, "wb") as f:
                    f.write(data.encode("utf-8"))
                last = None
                break
            except Exception as e:
                last = e
                print("  retry %d/4 %s -> %s" % (attempt, remote, e))
                time.sleep(1.0)
        if last:
            raise last
        os.remove(tmp)
        print("pushed %-42s -> %s (%d bytes, LF)" % (local, remote, len(data.encode("utf-8"))))
    sf.close()
    ssh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
