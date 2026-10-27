"""Chronos-2 时序大模型封装 (懒加载, chronos 库优先, 缺失时降级为趋势外推).

本地模型: models/ 目录 (Chronos-2-Small, 28M 参数, Chronos2Pipeline)
配置文件: models/config.json (chronos_pipeline_class: Chronos2Pipeline)
"""
from pathlib import Path
from typing import Optional

import torch

from ..common.config import settings
from ..common.logger import logger


class ChronosPredictor:
    """单例预测器."""

    _instance: Optional["ChronosPredictor"] = None

    def __init__(self):
        self._pipeline = None
        self._mode: Optional[str] = None  # "chronos" | "naive"
        self._degraded = False  # True 表示已降级 (模型加载失败或推理异常)
        self._device = "cuda" if torch.cuda.is_available() else "cpu"

    @classmethod
    def instance(cls) -> "ChronosPredictor":
        if cls._instance is None:
            cls._instance = ChronosPredictor()
        return cls._instance

    @property
    def is_degraded(self) -> bool:
        """是否处于降级状态 (naive 外推而非 Chronos 大模型)."""
        return self._degraded or self._mode == "naive"

    def _ensure_loaded(self) -> None:
        if self._mode is not None:
            return
        model_path = settings.chronos_model
        try:
            from chronos import Chronos2Pipeline

            logger.info(f"加载 Chronos2Pipeline: {model_path} (device={self._device})")
            self._pipeline = Chronos2Pipeline.from_pretrained(
                model_path, device_map=self._device
            )
            self._mode = "chronos"
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Chronos-2 模型加载失败 ({e}), 降级为线性趋势外推. "
                f"模型路径: {model_path}"
            )
            self._mode = "naive"
            self._degraded = True

    def predict(self, history: list[float], horizon: int) -> list[float]:
        """给定历史序列, 预测未来 horizon 步. 推理异常时自动降级为趋势外推."""
        self._ensure_loaded()
        if not history or horizon <= 0:
            return []
        if self._mode == "chronos":
            try:
                return self._predict_chronos(history, horizon)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Chronos 推理失败, 降级为趋势外推: {e}")
                self._degraded = True
                return self._predict_naive(history, horizon)
        return self._predict_naive(history, horizon)

    def _predict_chronos(self, history: list[float], horizon: int) -> list[float]:
        # Chronos-2 输入: 3维张量 (batch=1, n_variates=1, history_length)
        ctx = torch.tensor([history], dtype=torch.float32).unsqueeze(0)
        forecast = self._pipeline.predict(inputs=ctx, prediction_length=horizon)
        # 返回 list[torch.Tensor], 每个元素形状 [n_variates, num_quantiles, horizon]
        arr = forecast[0].cpu().numpy()
        # 沿 quantiles 维取均值 -> [n_variates, horizon], 取第0维 -> [horizon]
        if arr.ndim == 3:
            arr = arr.mean(axis=1)[0]
        elif arr.ndim == 2:
            arr = arr.mean(axis=0)
        return [max(0.0, float(v)) for v in arr.tolist()]

    @staticmethod
    def _predict_naive(history: list[float], horizon: int) -> list[float]:
        """降级方案: 用最近窗口的线性趋势外推 (非负)."""
        window = history[-min(len(history), 24) :]
        if len(window) < 2:
            return [max(0.0, window[-1])] * horizon
        first, last = window[0], window[-1]
        trend = (last - first) / max(1, len(window) - 1)
        return [max(0.0, last + trend * (i + 1)) for i in range(horizon)]
