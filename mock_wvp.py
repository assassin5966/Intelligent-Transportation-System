"""Mock WVP 服务器 - 用于测试设备配置工具完整流程.

模拟 WVP-GB28181-pro 的关键接口:
  POST /api/login                         -> 返回 token
  GET  /api/device/query/devices           -> 返回模拟设备列表
  GET  /api/device/query/devices/{id}/channels -> 返回模拟通道
  GET  /api/play/start/{dev}/{ch}          -> 返回流地址 (指向本地测试视频)
  GET  /api/play/stop/{dev}/{ch}           -> 返回成功

同时挂载 /data 目录提供视频文件 HTTP 下载 (供 backend cv2 拉流).

启动: python3 mock_wvp.py
端口: 18080 (WVP API) + /data 静态文件
"""
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"

app = FastAPI(title="Mock WVP")

# 挂载测试视频目录 (供 backend cv2.VideoCapture 拉流)
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

MOCK_DEVICES = [
    {
        "deviceId": "34020000001320000001",
        "name": "北门摄像头-车辆",
        "manufacturer": "mock",
        "model": "IPC-MOCK",
        "online": True,
        "hostAddress": "192.168.1.100:5060",
    },
    {
        "deviceId": "34020000001320000002",
        "name": "南门摄像头-人流",
        "manufacturer": "mock",
        "model": "IPC-MOCK",
        "online": True,
        "hostAddress": "192.168.1.101:5060",
    },
]

MOCK_CHANNELS = {
    "34020000001320000001": [
        {"channelId": "34020000001320000001", "name": "北门车辆通道", "subCount": 0},
    ],
    "34020000001320000002": [
        {"channelId": "34020000001320000002", "name": "南门人流通道", "subCount": 0},
    ],
}

# 视频文件映射
STREAM_VIDEOS = {
    "34020000001320000001": "车辆识别20s.mp4",
    "34020000001320000002": "人流识别20s.mp4",
}


@app.post("/api/login")
async def login():
    return {"code": 0, "data": {"accessToken": "mock-token-12345"}}


@app.get("/api/device/query/devices")
async def query_devices(page: int = 1, count: int = 100):
    return {"code": 0, "data": {"total": len(MOCK_DEVICES), "list": MOCK_DEVICES}}


@app.get("/api/device/query/devices/{device_id}/channels")
async def query_channels(device_id: str, page: int = 1, count: int = 100):
    channels = MOCK_CHANNELS.get(device_id, [])
    return {"code": 0, "data": {"total": len(channels), "list": channels}}


@app.get("/api/play/start/{device_id}/{channel_id}")
async def play_start(device_id: str, channel_id: str, request: Request):
    video = STREAM_VIDEOS.get(device_id, "车辆识别20s.mp4")
    # 返回容器内文件路径 (data 目录已挂载到 backend 容器)
    stream_url = f"/app/data/{video}"
    return {
        "code": 0,
        "data": {
            "flv": stream_url,
            "rtsp": f"rtsp://localhost:8554/{video}",
            "streamId": f"{device_id}_{channel_id}",
        },
    }


@app.get("/api/play/stop/{device_id}/{channel_id}")
async def play_stop(device_id: str, channel_id: str):
    return {"code": 0, "data": "ok"}


if __name__ == "__main__":
    print(f"Mock WVP 启动中... 数据目录: {DATA_DIR}")
    print(f"  测试视频: {list(STREAM_VIDEOS.values())}")
    print(f"  API: http://localhost:18080")
    print(f"  视频: http://localhost:18080/data/车辆识别20s.mp4")
    uvicorn.run(app, host="0.0.0.0", port=18080)
