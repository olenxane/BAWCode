"""谐波激励器（Harmonic Exciter）。

提取高频段（默认 3.5kHz 以上）信号，经 tanh 软饱和产生附加谐波，
限带后以低比例混回原信号，为人声齿音、hi-hat 等高频成分补充光泽。
处理路径全部限带在 16kHz 以下，谐波落点不超过奈奎斯特频率，无需过采样。

流式处理：每声道独立保存滤波器状态，分块调用之间连续。
"""
from __future__ import annotations
import numpy as np

try:
    from scipy.signal import butter, lfilter
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


class Exciter:
    """激励器：HPF 提取高频 → tanh 软饱和 → LPF 限带 → 低比例混合。

    enabled 关闭或 scipy 缺失时由调用方旁路；seek 时调用 reset() 清空状态。
    """

    def __init__(self, fs: int, channels: int = 2,
                 freq_hz: float = 3500.0,
                 mix: float = 0.15,
                 drive: float = 2.0,
                 lpf_hz: float = 16000.0):
        if not _HAS_SCIPY:
            raise RuntimeError("激励器需要 scipy")

        self.fs = fs
        self.channels = channels
        self.mix = float(min(max(mix, 0.0), 0.5))
        self.drive = float(drive)
        self.enabled = True

        nyq = fs / 2.0
        # 高通截止限制在有效范围内；低通不超过奈奎斯特的 90%
        hp_fc = float(min(max(freq_hz, 1000.0), nyq * 0.45))
        lp_fc = float(min(lpf_hz, nyq * 0.9))

        self._b_hpf, self._a_hpf = butter(2, hp_fc / nyq, btype="high")
        self._b_lpf, self._a_lpf = butter(2, lp_fc / nyq, btype="low")

        def _zi_len(b, a):
            return max(len(b), len(a)) - 1

        self._zi_hpf = [np.zeros(_zi_len(self._b_hpf, self._a_hpf)) for _ in range(channels)]
        self._zi_lpf = [np.zeros(_zi_len(self._b_lpf, self._a_lpf)) for _ in range(channels)]

    def process(self, data: np.ndarray) -> np.ndarray:
        """处理音频块。data: shape (n, channels) 或 (n,) float32，返回同形状数组。"""
        if not self.enabled or self.mix <= 0.0:
            return data
        if not _HAS_SCIPY:
            return data

        single = data.ndim == 1
        out = data.copy() if not single else data.reshape(-1, 1).copy()
        n_ch = min(out.shape[1], self.channels)
        # 小信号归一化：tanh(k·x)/k ≈ x，保证 harm 只含非线性附加分量
        norm = self.drive

        for ch in range(n_ch):
            x = out[:, ch]
            # 提取高频驱动频段
            band, self._zi_hpf[ch] = lfilter(self._b_hpf, self._a_hpf, x, zi=self._zi_hpf[ch])
            # 软饱和：tanh(kx)/k - x 只保留非线性附加分量（以奇次谐波为主）
            saturated = np.tanh(self.drive * band) / norm
            harm = saturated - band
            # 限带，避免谐波折叠到高频刺耳
            harm, self._zi_lpf[ch] = lfilter(self._b_lpf, self._a_lpf, harm, zi=self._zi_lpf[ch])
            out[:, ch] = x + harm * self.mix

        return out[:, 0] if single else out

    def reset(self):
        """清空滤波器状态（seek 时调用）"""
        def _zi_len(b, a):
            return max(len(b), len(a)) - 1

        self._zi_hpf = [np.zeros(_zi_len(self._b_hpf, self._a_hpf)) for _ in range(self.channels)]
        self._zi_lpf = [np.zeros(_zi_len(self._b_lpf, self._a_lpf)) for _ in range(self.channels)]
