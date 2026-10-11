"""10 段图形均衡器：使用 RBJ peaking-EQ biquad 级联实现，
支持流式（分块）处理，块间保留滤波器状态（zi）以避免爆音。
"""
from __future__ import annotations
import numpy as np
from scipy.signal import iirpeak, sosfilt, tf2sos, lfilter

from .config import BAND_CENTER_HZ, BAND_CENTER_HZ_30


def _peaking_eq_coeffs(freq, gain_db, q, fs):
    """RBJ Audio EQ Cookbook - peaking EQ 系数"""
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / fs
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    b0 = 1 + alpha * A
    b1 = -2 * cos_w0
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cos_w0
    a2 = 1 - alpha / A

    b = np.array([b0, b1, b2]) / a0
    a = np.array([1.0, a1 / a0, a2 / a0])
    return b, a


class Equalizer:
    """多段级联 EQ，支持逐块流式处理（保持滤波器状态）"""

    def __init__(self, fs: int, bands_db=None, q_values=None, channels: int = 2):
        self.fs = fs
        self.q_values = list(q_values) if q_values else [1.0] * 10
        self.channels = channels
        self.enabled = True
        self.bands_db = list(bands_db) if bands_db else [0.0] * 10
        self._build_filters()
        # 每个声道、每个频段都要独立保存滤波器状态
        self._zi = [[np.zeros(2) for _ in range(len(self.filters))] for _ in range(channels)]

    def _build_filters(self):
        # 依据增益列表长度选择频段表：10 段或 30 段（1/3 倍频程）
        if len(self.bands_db) == len(BAND_CENTER_HZ_30):
            center_hz = BAND_CENTER_HZ_30
        else:
            center_hz = BAND_CENTER_HZ
        self.filters = []
        for freq, gain, q in zip(center_hz, self.bands_db, self.q_values):
            if abs(gain) < 1e-9:
                self.filters.append(None)  # 增益为0时跳过滤波，节省算力
                continue
            nyq = self.fs / 2.0
            if freq >= nyq:
                self.filters.append(None)
                continue
            b, a = _peaking_eq_coeffs(freq, gain, q, self.fs)
            self.filters.append((b, a))

    def set_bands(self, bands_db, q_values=None):
        self.bands_db = list(bands_db)
        if q_values is not None:
            self.q_values = list(q_values)
        self._build_filters()
        self._zi = [[np.zeros(2) for _ in range(len(self.filters))] for _ in range(self.channels)]

    def set_band(self, index: int, gain_db: float, q: float | None = None):
        self.bands_db[index] = gain_db
        if q is not None:
            self.q_values[index] = q
        self._build_filters()
        self._zi = [[np.zeros(2) for _ in range(len(self.filters))] for _ in range(self.channels)]

    def process(self, block: np.ndarray) -> np.ndarray:
        """block: shape (n_frames, n_channels) float32 in [-1, 1]"""
        if not self.enabled:
            return block
        out = block.copy()
        n_ch = out.shape[1] if out.ndim > 1 else 1
        if out.ndim == 1:
            out = out.reshape(-1, 1)
        for ch in range(min(n_ch, self.channels)):
            x = out[:, ch]
            for i, filt in enumerate(self.filters):
                if filt is None:
                    continue
                b, a = filt
                x, self._zi[ch][i] = lfilter(b, a, x, zi=self._zi[ch][i])
            out[:, ch] = x
        return out if block.ndim > 1 else out[:, 0]

    def reset(self):
        self._zi = [[np.zeros(2) for _ in range(len(self.filters))] for _ in range(self.channels)]
