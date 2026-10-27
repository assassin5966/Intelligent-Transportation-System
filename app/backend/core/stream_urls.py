"""流地址翻译: 同一个 WVP 点播地址, 按消费方重写 host.

WVP/ZLM 返回地址的 host 固定是 sdp-ip (media.sdp-ip 配置):
  - 浏览器在外部网络访问 sdp-ip 通常可达 -> /play 用 to_browser()
    (配置 zlm_public_base 时重写为对外地址)
  - backend/AI 在容器内访问 sdp-ip 不一定可达 (云服务器公网 IP 不在网卡上,
    云平台 1:1 NAT 无回环) -> 内部取流用 to_internal()
    (配置 zlm_internal_base 时重写为容器网内 ZLM 服务名, 如 http://zlmediakit)

两处均只替换 scheme://host:port, path/query (含 stream_id) 原样保留;
对应配置为空时原样返回 (不重写).
"""
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from ...common.config import settings


def _swap_base(url: str, base: str) -> str:
    parts, b = urlsplit(url), urlsplit(base)
    return urlunsplit((b.scheme, b.netloc, parts.path, parts.query, parts.fragment))


def to_browser(url: Optional[str]) -> Optional[str]:
    """浏览器可播地址 (/play 返回给前端大屏/预览)."""
    if url and settings.zlm_public_base:
        return _swap_base(url, settings.zlm_public_base.rstrip("/"))
    return url


def to_internal(url: Optional[str]) -> Optional[str]:
    """容器内取流地址 (backend 截帧 cv2 / AI 拉流 / 同步启流)."""
    if url and settings.zlm_internal_base:
        return _swap_base(url, settings.zlm_internal_base)
    return url
