"""Chronos 时序大模型封装 (懒加载, chronos 库优先, 缺失时降级为趋势外推)."""
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
        self._device = "cuda" if torch.cuda.is_available() else "cpu"

    @classmethod
    def instance(cls) -> "ChronosPredictor":
        if cls._instance is None:
            cls._instance = ChronosPredictor()
        return cls._instance

    def _ensure_loaded(self) -> None:
        if self._mode is not None:
            return
        name = settings.chronos_model
        try:
            from chronos import ChronosPipeline

            logger.info(f"加载 ChronosPipeline: {name} (device={self._device})")
            self._pipeline = ChronosPipeline.from_pretrained(
                name, device_map=self._device
            )
            self._mode = "chronos"
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"chronos 库不可用 ({e}), 降级为线性趋势外推. "
                f"建议安装 chronos-forecasting 以获得大模型预测能力."
            )
            self._mode = "naive"

    def predict(self, history: list[float], horizon: int) -> list[float]:
        """给定历史序列, 预测未来 horizon 步."""
        self._ensure_loaded()
        if not history or horizon <= 0:
            return []
        if self._mode == "chronos":
            return self._predict_chronos(history, horizon)
        return self._predict_naive(history, horizon)

    def _predict_chronos(self, history: list[float], horizon: int) -> list[float]:
        ctx = torch.tensor([history], dtype=torch.float32)
        forecast = self._pipeline.predict(context=ctx, prediction_length=horizon)
        arr = forecast[0].cpu().numpy()
        # forecast 形状 [num_samples, horizon] -> 取均值
        if arr.ndim == 2:
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
