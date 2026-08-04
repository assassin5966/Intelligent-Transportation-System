"""WVP-GB28181 REST 客户端.

封装 WVP-Pro 的设备查询与点播接口, 供后端设备同步 (wvp_sync) 与流地址刷新使用.
WVP 无标准对外 HTTP webhook, 本客户端配合 wvp_sync 定时轮询使用.

认证: POST /api/login 取 access-token, 缓存在内存, 遇 401 自动重登.
点播: GET /api/play/start/{deviceId}/{channelId} 返回 flv/rtsp 地址 (跨版本结构兼容).
"""
from typing import Optional

import httpx

from ...common.config import settings
from ...common.logger import logger


class WVPError(Exception):
    pass


class WVPClient:
    """WVP-Pro REST API 客户端 (异步, 模块级单例).

    wvp_enabled=False 时所有方法返回空值, 调用方无需额外判空.
    """

    def __init__(self) -> None:
        self.enabled = settings.wvp_enabled
        self._base = settings.wvp_api_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _login(self) -> None:
        """POST /api/login 获取 access-token."""
        client = self._get_client()
        resp = await client.post(
            f"{self._base}/api/login",
            json={"username": settings.wvp_username, "password": settings.wvp_password},
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        # 兼容字段名: accessToken / access-token
        token = data.get("accessToken") or data.get("access-token")
        if not token:
            raise WVPError(f"WVP 登录未返回 token: {body}")
        self._token = token
        logger.info("WVP 登录成功, token 已缓存")

    def _headers(self) -> dict:
        return {"access-token": self._token} if self._token else {}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """统一请求: 首次无 token 先登录, 401 自动重登一次后重试."""
        client = self._get_client()
        url = f"{self._base}{path}"
        if self._token is None:
            await self._login()
        resp = await client.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            logger.warning("WVP token 失效, 重新登录")
            await self._login()
            resp = await client.request(method, url, headers=self._headers(), **kwargs)
        resp.raise_for_status()
        body = resp.json()
        # WVP 约定 code=0 成功; 200 为部分接口兼容; None 为无 code 字段
        if isinstance(body, dict) and body.get("code") not in (0, 200, None):
            raise WVPError(f"WVP 接口返回错误 {path}: {body}")
        return body

    async def list_devices(self) -> list[dict]:
        """分页拉取全部国标设备. 返回 [{deviceId, name, online, ...}]."""
        if not self.enabled:
            return []
        devices: list[dict] = []
        page = 1
        count = 100
        while True:
            body = await self._request(
                "GET", "/api/device/query/devices", params={"page": page, "count": count}
            )
            data = body.get("data", {})
            batch = data.get("list") or data.get("deviceList") or []
            devices.extend(batch)
            total = data.get("total", 0)
            if len(devices) >= total or not batch:
                break
            page += 1
        return devices

    async def list_channels(self, device_id: str) -> list[dict]:
        """拉取指定设备的通道列表. 返回 [{channelId, name, ...}]."""
        if not self.enabled:
            return []
        channels: list[dict] = []
        page = 1
        count = 100
        while True:
            body = await self._request(
                "GET",
                f"/api/device/query/devices/{device_id}/channels",
                params={"page": page, "count": count},
            )
            data = body.get("data", {})
            batch = data.get("list") or data.get("channelList") or []
            channels.extend(batch)
            total = data.get("total", 0)
            if len(channels) >= total or not batch:
                break
            page += 1
        return channels

    async def start_play(self, device_id: str, channel_id: str) -> Optional[dict]:
        """开始点播, 返回 {flv, rtsp, stream_id}. 失败返回 None.

        兼容新版 data.flv 与旧版 data.stream.flv 两种返回结构.
        """
        if not self.enabled:
            return None
        params = {}
        if settings.wvp_stream_sub:
            params["streamType"] = "sub"
        try:
            body = await self._request(
                "GET", f"/api/play/start/{device_id}/{channel_id}", params=params
            )
        except (WVPError, httpx.HTTPError) as e:
            logger.error(f"WVP 点播失败 {device_id}/{channel_id}: {e}")
            return None
        data = body.get("data", {}) if isinstance(body, dict) else {}
        stream = data.get("stream") if isinstance(data, dict) else None
        flv = (data.get("flv") if isinstance(data, dict) else None) or (
            stream.get("flv") if stream else None
        )
        rtsp = (data.get("rtsp") if isinstance(data, dict) else None) or (
            stream.get("rtsp") if stream else None
        )
        stream_id = (
            (data.get("streamId") if isinstance(data, dict) else None)
            or (stream.get("id") if stream else None)
            or f"{device_id}_{channel_id}"
        )
        return {"flv": flv, "rtsp": rtsp, "stream_id": stream_id}

    async def stop_play(self, device_id: str, channel_id: str) -> bool:
        """停止点播."""
        if not self.enabled:
            return False
        try:
            await self._request("GET", f"/api/play/stop/{device_id}/{channel_id}")
            return True
        except (WVPError, httpx.HTTPError) as e:
            logger.warning(f"WVP 停播失败 {device_id}/{channel_id}: {e}")
            return False

    def select_stream_url(self, play_result: Optional[dict]) -> Optional[str]:
        """按 wvp_play_protocol 从点播结果取地址 (flv 优先回退 rtsp, 反之亦然)."""
        if not play_result:
            return None
        if settings.wvp_play_protocol == "rtsp":
            return play_result.get("rtsp") or play_result.get("flv")
        return play_result.get("flv") or play_result.get("rtsp")


_wvp_client: Optional[WVPClient] = None


def get_wvp_client() -> WVPClient:
    """模块级单例."""
    global _wvp_client
    if _wvp_client is None:
        _wvp_client = WVPClient()
    return _wvp_client


async def close_wvp_client() -> None:
    global _wvp_client
    if _wvp_client is not None:
        await _wvp_client.aclose()
        _wvp_client = None
