"""实时频谱分析：对最近一块 PCM 数据做 FFT，按对数频段分组，
输出平滑后的每个柱子的 0~1 高度值，供 UI 绘制。
"""
from __future__ import annotations
import numpy as np


class SpectrumAnalyzer:
    def __init__(self, fs: int, bars: int = 48, fft_size: int = 2048,
                 smoothing: float = 0.6, min_db: float = -60, max_db: float = 0,
                 min_freq: float = 30.0, max_freq: float = 16000.0):
        self.fs = fs
        self.bars = bars
        self.fft_size = fft_size
        self.smoothing = float(np.clip(smoothing, 0.0, 0.97))
        self.min_db = min_db
        self.max_db = max_db
        self._window = np.hanning(fft_size).astype(np.float32)
        self._levels = np.zeros(bars, dtype=np.float32)
        self._edges = self._make_log_bins(min_freq, min(max_freq, fs / 2 - 1))
        self._buffer = np.zeros(fft_size, dtype=np.float32)

    def _make_log_bins(self, fmin, fmax):
        edges = np.logspace(np.log10(fmin), np.log10(fmax), self.bars + 1)
        return edges

    def feed(self, mono_block: np.ndarray):
        """喂入最新的单声道 PCM 数据（float32, -1~1），内部维护滚动缓冲区"""
        n = len(mono_block)
        if n >= self.fft_size:
            self._buffer[:] = mono_block[-self.fft_size:]
        else:
            self._buffer[:-n] = self._buffer[n:]
            self._buffer[-n:] = mono_block

    def compute(self) -> np.ndarray:
        """返回长度为 bars 的数组，每个值范围 0~1，代表该频段能量（已平滑）"""
        windowed = self._buffer * self._window
        spec = np.fft.rfft(windowed)
        mag = np.abs(spec) / self.fft_size
        freqs = np.fft.rfftfreq(self.fft_size, d=1.0 / self.fs)

        # 向量化分箱：将每个 FFT bin 分配到对应的对数频段
        valid = (freqs >= self._edges[0]) & (freqs < self._edges[-1])
        bar_idx = np.searchsorted(self._edges[1:-1], freqs[valid], side='right')
        bar_idx = np.clip(bar_idx, 0, self.bars - 1)
        raw = np.zeros(self.bars, dtype=np.float32)
        np.maximum.at(raw, bar_idx, mag[valid])
        # 低频频段窄于 FFT 分辨率时会有空 bar，从相邻非空 bar 线性插值避免结构性零值
        empty = raw == 0
        if np.any(empty) and np.any(~empty):
            idx = np.arange(self.bars)
            raw[empty] = np.interp(idx[empty], idx[~empty], raw[~empty])

        with np.errstate(divide="ignore"):
            db = 20 * np.log10(np.maximum(raw, 1e-9))
        norm = (db - self.min_db) / (self.max_db - self.min_db)
        norm = np.clip(norm, 0.0, 1.0)

        self._levels = self.smoothing * self._levels + (1 - self.smoothing) * norm
        return self._levels.copy()

    def silence_decay(self):
        """无音频输出时（暂停/停止）让频谱自然衰减到 0"""
        self._levels *= 0.85
        return self._levels.copy()
