"""多摄像头模拟联调脚本.

读取 configs/sim_cameras.yaml, 向 AI 分析服务注册多路摄像头 (本地录像视频循环播放,
模拟持续 GB28181/RTSP 信号源), 然后周期性轮询后端 /api/stats/realtime 验证多路联合计数.

前置:
  1. docker compose -p smartcity up -d   (启动 ai + backend + redis)
  2. 将录像视频放入 ./videos/ 目录
  3. 复制 configs/sim_cameras.yaml.example 为 configs/sim_cameras.yaml 并填写
  4. python scripts/simulate_multi_camera.py [config_path]
"""
import asyncio
import sys
from pathlib import Path

import httpx
import yaml

AI_URL = "http://localhost:8001"
BACKEND_URL = "http://localhost:8000"
CONFIG_PATH = "configs/sim_cameras.yaml"
POLL_INTERVAL = 5  # 联合计数轮询间隔 (秒)


async def _health(client: httpx.AsyncClient, name: str, url: str) -> bool:
    try:
        r = await client.get(f"{url}/health", timeout=5.0)
        print(f"[健康] {name}: {r.json()}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[健康] {name} 不可达: {e}")
        return False


async def register_camera(client: httpx.AsyncClient, cam: dict) -> None:
    payload = {
        "device_id": cam["id"],
        "stream_url": cam["video"],
        "line": cam["line"],
    }
    if cam.get("anchor") is not None:
        payload["anchor"] = cam["anchor"]
    if cam.get("count_only"):
        payload["count_only"] = cam["count_only"]
    # 已注册则先删除再注册 (刷新配置)
    resp = await client.post(f"{AI_URL}/devices", json=payload)
    if resp.status_code == 409:
        await client.delete(f"{AI_URL}/devices/{cam['id']}")
        resp = await client.post(f"{AI_URL}/devices", json=payload)
    status = "OK" if resp.status_code in (200, 201) else f"FAIL({resp.status_code})"
    print(f"  [{status}] {cam['id']} {cam.get('name', '')} <- {cam['video']}")


async def main() -> None:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH
    cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
    cameras = cfg.get("cameras", [])
    if not cameras:
        print(f"未在 {cfg_path} 中配置摄像头")
        return

    async with httpx.AsyncClient(timeout=10.0) as client:
        if not await _health(client, "AI", AI_URL):
            return
        if not await _health(client, "backend", BACKEND_URL):
            return

        print(f"\n注册 {len(cameras)} 路模拟摄像头...")
        for cam in cameras:
            await register_camera(client, cam)

        r = await client.get(f"{AI_URL}/devices")
        running = [d["device_id"] for d in r.json() if d.get("running")]
        print(f"\n运行中设备: {running}")

        print(f"\n开始轮询联合计数 (每 {POLL_INTERVAL}s, Ctrl+C 退出)...\n")
        try:
            while True:
                stats = (await client.get(f"{BACKEND_URL}/api/stats/realtime")).json()
                print(
                    f"  车辆={stats['current_vehicles']} 人员={stats['current_persons']} | "
                    f"今日 车进={stats['today_vehicle_in']} 车出={stats['today_vehicle_out']} "
                    f"人进={stats['today_person_in']} 人出={stats['today_person_out']} | "
                    f"活跃设备={stats['active_devices']}"
                )
                await asyncio.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n停止轮询")
        finally:
            print("\n注销模拟摄像头...")
            for cam in cameras:
                try:
                    await client.delete(f"{AI_URL}/devices/{cam['id']}")
                    print(f"  [OK] {cam['id']} 已注销")
                except Exception as e:  # noqa: BLE001
                    print(f"  [FAIL] {cam['id']}: {e}")
            print("完成")


if __name__ == "__main__":
    asyncio.run(main())
