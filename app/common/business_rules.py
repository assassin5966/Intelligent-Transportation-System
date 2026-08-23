"""业务规则热重载加载器.

集中管理 configs/business_rules.yaml, 基于文件 mtime 自动热重载:
- 修改业务规则文件后无需重启服务即可生效.
- 优先级: business_rules.yaml 覆盖 config.py 默认值; 未配置项回落 config.py.
- 使用: get_rule("counting", "hold_frames", default=3) -> 每次调用返回最新值.
"""
import threading
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import settings
from .logger import logger

_lock = threading.Lock()
_cache: Optional[dict] = None
_mtime: Optional[float] = None


def _reload() -> dict:
    """读取业务规则文件 (文件缺失/损坏时返回空 dict, 不影响服务)."""
    p = Path(settings.business_rules_file)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except OSError as e:
        logger.warning(f"业务规则文件读取失败: {e}")
        return {}
    except yaml.YAMLError as e:
        logger.warning(f"业务规则文件 YAML 解析失败: {e}")
        return {}


def get_business_rules() -> dict:
    """返回业务规则全量 dict (mtime 变化时自动重载)."""
    global _cache, _mtime
    p = Path(settings.business_rules_file)
    try:
        cur_mtime = p.stat().st_mtime
    except OSError:
        cur_mtime = None
    if _cache is None or (cur_mtime is not None and _mtime != cur_mtime):
        with _lock:
            if _cache is None or (cur_mtime is not None and _mtime != cur_mtime):
                _cache = _reload()
                _mtime = cur_mtime
    return _cache or {}


def get_rule(section: str, key: str, default: Any = None) -> Any:
    """读取单条业务规则 (section.key); 未配置时返回 default."""
    return get_business_rules().get(section, {}).get(key, default)


def get_section(section: str) -> dict:
    """读取整个业务域配置; 未配置时返回空 dict."""
    sec = get_business_rules().get(section)
    return sec if isinstance(sec, dict) else {}
