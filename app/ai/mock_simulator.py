"""Mock 数据模拟器 (MOCK_ENABLED=true 时替代视频拉流推理).

按真实交通规律为全部已注册设备生成车流/人流越线事件, 走正常链路推送后端
POST /api/events -> Redis 实时统计 -> MySQL 归档 -> 前端展示, 全链路可见.

数据规律 (与真实场景对齐):
  - 城门日总量: 永泰东门最繁华 (1.3~1.4 万辆/天), 武定西门最少 (~9 千),
    其余城门介于两者之间; 人流约为同城门车流的 40%.
  - 时段曲线: 夜间 (0-6 点) 极少, 上午缓慢爬升, 下午+晚上 (14-22 点) 最多且增速快.
  - 事件节奏: 每设备每 2~5 秒一个 tick, 按 泊松期望 λ 抽取事件数 (实时随机事件流);
    λ = 日总量 × 当前小时权重 × tick秒/3600 × 抖动(0.5~1.5).
  - 方向: 车流按设备 entrance_type (入口设备 enter 为主 / 出口设备 exit 为主 /
    出入口 55:45); 人流双向 55:45 模拟净流入.

不拉流/不推理/不依赖任何模型; 心跳照常发送, 保证前端设备在线状态正常.
"""
import asyncio
import json
import math
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from ..common.config import settings
from ..common.logger import logger

# 种子数据 (与 device_info.py 同源): data/device_geo.json
_GEO_FILE = Path(__file__).resolve().parents[2] / "data" / "device_geo.json"

# 城门日车流总量 (辆/天): 永泰东门 1.3~1.4 万取中值, 武定西门最少,
# 其余按地理常识在 9500~13000 间分配 (固定值保证日总量可复现/可核对)
_GATE_DAILY_VEHICLES: Dict[str, int] = {
    "永泰东门": 13500,
    "和阳北门": 12500,
    "清远南门": 11500,
    "永泰西门": 11000,
    "和阳南门": 10500,
    "武定东门": 10000,
    "清远北门": 9500,
    "武定西门": 9000,
}
# 人流占车流比例 (用户指定 ~40%)
_PERSON_RATIO = 0.40

# 24 小时时段权重 (和为 1.0): 夜间极少, 上午缓升, 14-22 高峰 (~60%)
_HOURLY_W: List[float] = [
    0.005, 0.004, 0.003, 0.003, 0.004, 0.007,  # 0-5  夜间 (~2.6%)
    0.014, 0.029, 0.044, 0.049, 0.049, 0.053,   # 6-11 早晨爬升
    0.058, 0.063, 0.068, 0.073, 0.078, 0.082,   # 12-17 午后走强
    0.078, 0.073, 0.068, 0.053, 0.028, 0.014,   # 18-23 晚高峰回落
]

# tick 随机区间 (秒) 与事件抖动幅度
_TICK_MIN, _TICK_MAX = 2.0, 5.0
_JITTER_MIN, _JITTER_MAX = 0.5, 1.5

# 心跳周期 (秒), 与 pipeline._heartbeat_loop 一致
_HEARTBEAT_INTERVAL = 30.0

# 拥挤模拟参数: 注册时写入设备的拥挤阈值 (record_congestion 要求阈值>0 才判定)
_VEHICLE_MAX = 10   # 车流卡口 ROI 最大车辆数阈值
_PERSON_MAX = 30    # 人流便道 ROI 最大人数阈值
# 拥挤上报周期 (秒), 与 pipeline 的拥挤上报节奏同量级
_CONGESTION_INTERVAL = 10.0


def _poisson(mut_mean: float) -> int:
    """小期望泊松采样 (Knuth 法): λ 通常 < 0.1, 循环次数极少."""
    L = math.exp(-mut_mean)
    k, prod = 0, random.random()
    while prod > L:
        k += 1
        prod *= random.random()
    return k


class _MockDevice:
    """单个模拟设备的静态参数 (从种子数据解析一次)."""

    __slots__ = ("device_id", "camera_type", "enter_bias", "daily_total")

    def __init__(self, device_id: str, camera_type: str, enter_bias: float, daily_total: float):
        self.device_id = device_id
        self.camera_type = camera_type          # "vehicle" | "person"
        self.enter_bias = enter_bias            # P(Enter)
        self.daily_total = daily_total          # 该设备日均事件数 (进+出)


def _load_mock_devices() -> List[_MockDevice]:
    """从 device_geo.json 解析 32 个注册设备为模拟参数.

    - 车流/人流: point_type 含"卡口" -> vehicle, 否则 person
    - 城门: 设备名匹配 8 城门关键词; 未匹配到的设备跳过 (不模拟)
    - 方向偏好: entrance_type 入口->enter 0.85, 出口->enter 0.15, 出入口->0.55
    """
    data = json.loads(_GEO_FILE.read_text(encoding="utf-8"))
    devices: List[_MockDevice] = []
    for name, info in data.get("devices", {}).items():
        gate = next((g for g in _GATE_DAILY_VEHICLES if g in name), None)
        if gate is None:
            continue
        point_type = info.get("point_type") or ""
        category = info.get("category") or ""
        is_vehicle = "卡口" in point_type or "卡口" in category or "车" in category
        camera_type = "vehicle" if is_vehicle else "person"

        entrance = info.get("entrance_type") or "出入口"
        if is_vehicle:
            enter_bias = {"入口": 0.85, "出口": 0.15}.get(entrance, 0.55)
        else:
            enter_bias = 0.55  # 人流双向, 净流入

        gate_daily_veh = _GATE_DAILY_VEHICLES[gate]
        if camera_type == "vehicle":
            # 城门 2 个卡口均分车流; Enter+Exit 双向计, 单方向即日总量
            daily = gate_daily_veh
        else:
            # 人流 = 车流 × 40%, 2 个便道设备均分
            daily = gate_daily_veh * _PERSON_RATIO / 2
        devices.append(_MockDevice(name, camera_type, enter_bias, daily))
    return devices


class MockSimulator:
    """MOCK 模式总控: 为全部设备启动事件生成 task 与心跳 task."""

    def __init__(self):
        self._tasks: List[asyncio.Task] = []
        self._devices: List[_MockDevice] = []
        self._client: Optional[httpx.AsyncClient] = None
        self._stats_pushed = 0  # 已推送事件计数 (观测用)

    async def start(self) -> None:
        self._devices = _load_mock_devices()
        if not self._devices:
            logger.warning("[Mock] device_geo.json 未解析到任何城门设备, 模拟器空转")
            return
        self._client = httpx.AsyncClient(timeout=5.0)
        # 先注册设备表 (POST /api/devices 仅写 Redis 配置, 不启流):
        # 心跳接口要求设备已存在, 否则 404; 设备状态也是前端 online/abnormal 判定依据
        registered = 0
        for d in self._devices:
            try:
                payload = {
                    "id": d.device_id,
                    "name": d.device_id,
                    "stream_url": "",
                    "camera_type": d.camera_type,
                }
                # 拥挤阈值: mock 模式也要模拟拥挤状态, 必须配置阈值才会触发判定
                if d.camera_type == "vehicle":
                    payload["max_vehicles"] = _VEHICLE_MAX
                else:
                    payload["max_persons"] = _PERSON_MAX
                resp = await self._client.post(
                    f"{settings.backend_url}/api/devices", json=payload
                )
                if resp.status_code in (200, 201):
                    registered += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Mock] 注册设备 {d.device_id[:40]} 失败: {e}")
        logger.info(f"[Mock] 设备注册完成: {registered}/{len(self._devices)}")
        for d in self._devices:
            self._tasks.append(asyncio.create_task(self._event_loop(d)))
            self._tasks.append(asyncio.create_task(self._heartbeat_loop(d)))
            self._tasks.append(asyncio.create_task(self._congestion_loop(d)))
        veh = sum(1 for d in self._devices if d.camera_type == "vehicle")
        per = len(self._devices) - veh
        logger.info(
            f"[Mock] 模拟器已启动: {len(self._devices)} 设备 (车流 {veh} / 人流 {per}), "
            f"日总量目标 ~{sum(_GATE_DAILY_VEHICLES.values())} 车次 + "
            f"~{int(sum(_GATE_DAILY_VEHICLES.values()) * _PERSON_RATIO)} 人次"
        )

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        logger.info(f"[Mock] 模拟器已停止 (累计推送事件 {self._stats_pushed})")

    @property
    def device_ids(self) -> List[str]:
        return [d.device_id for d in self._devices]

    # ---- 内部循环 ----

    async def _event_loop(self, d: _MockDevice) -> None:
        """单设备事件生成主循环: tick -> 泊松采样 -> 批量推送."""
        while True:
            tick = random.uniform(_TICK_MIN, _TICK_MAX)
            await asyncio.sleep(tick)
            now = datetime.now()
            lam = (
                d.daily_total
                * _HOURLY_W[now.hour]
                * (tick / 3600.0)
                * random.uniform(_JITTER_MIN, _JITTER_MAX)
            )
            n = _poisson(lam)
            if n <= 0:
                continue
            for _ in range(n):
                event_type = (
                    f"{'Vehicle' if d.camera_type == 'vehicle' else 'Person'}"
                    f"{'Enter' if random.random() < d.enter_bias else 'Exit'}"
                )
                occurred = now.timestamp() - random.uniform(0, tick)
                await self._push_event(d.device_id, event_type, occurred)

    async def _push_event(self, device_id: str, event_type: str, occurred_ts: float) -> None:
        """推送单条事件到后端 (失败仅记日志, 与 outbox 不重试策略一致)."""
        payload = {
            "device_id": device_id,
            "event_type": event_type,
            "occurred_at": datetime.fromtimestamp(occurred_ts).isoformat(),
        }
        try:
            resp = await self._client.post(
                f"{settings.backend_url}/api/events", json=payload
            )
            if resp.status_code == 201:
                self._stats_pushed += 1
            else:
                logger.warning(f"[Mock] {device_id} {event_type} -> HTTP {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Mock] {device_id} {event_type} 推送失败: {e}")

    async def _heartbeat_loop(self, d: _MockDevice) -> None:
        """心跳: 维持设备在线状态 (前端/离线检测依赖)."""
        while True:
            try:
                await self._client.post(
                    f"{settings.backend_url}/api/devices/{d.device_id}/heartbeat"
                )
            except Exception:  # noqa: BLE001
                pass  # 心跳失败不影响事件生成
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    async def _congestion_loop(self, d: _MockDevice) -> None:
        """拥挤状态模拟: 周期上报 ROI 瞬时数量 + 每分钟流量, 走真实拥挤判定链路.

        - 正常态: ROI 数量按当前时段流量泊松采样 (远低于阈值), 流速 = 日总量×权重/60.
        - 拥挤片段: 高峰期更易触发 (概率随时段权重上升), 持续 2~5 个周期 (20~50 秒):
          ROI 数量超过设备阈值 且 流速压到 min_flow 以下, 正好命中双阈值拥挤条件,
          触发后端 onset(critical) 告警; 片段结束后流速恢复, 触发 recovery(info).
        """
        remain = 0  # 拥挤片段剩余周期数 (>0 表示正在拥堵)
        while True:
            await asyncio.sleep(_CONGESTION_INTERVAL)
            now = datetime.now()
            w = _HOURLY_W[now.hour]
            base_flow = d.daily_total * w / 60.0 * random.uniform(0.6, 1.4)
            if remain > 0:
                remain -= 1
                congested = True
            elif random.random() < min(0.18, w * 2.2):
                congested = True
                remain = random.randint(2, 5)
            else:
                congested = False
            # 拥堵时流速骤降 (低于 congestion_min_flow / person_congestion_min_flow)
            flow = base_flow * random.uniform(0.05, 0.3) if congested else base_flow
            payload: Dict = {"device_id": d.device_id}
            if d.camera_type == "vehicle":
                payload["vehicle_flow_per_min"] = round(flow, 2)
                if congested:
                    payload["roi_vehicles"] = random.randint(_VEHICLE_MAX, _VEHICLE_MAX + 8)
                else:
                    payload["roi_vehicles"] = min(
                        _VEHICLE_MAX - 1, _poisson(max(0.05, base_flow * 0.8))
                    )
            else:
                payload["person_flow_per_min"] = round(flow, 2)
                if congested:
                    payload["roi_persons"] = random.randint(_PERSON_MAX, _PERSON_MAX + 20)
                else:
                    payload["roi_persons"] = min(
                        _PERSON_MAX - 1, _poisson(max(0.1, base_flow * 2.0))
                    )
            try:
                await self._client.post(
                    f"{settings.backend_url}/api/stats/congestion", json=payload
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Mock] {d.device_id} 拥挤上报失败: {e}")
