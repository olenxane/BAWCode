"""前瞻软限幅器（Look-ahead Soft Limiter）。

5ms 前瞻延迟线：增益在瞬态到达输出端之前已经压下，瞬态不被硬削。
包络取滑动窗口（= 前瞻长度）峰值 → 软膝增益 → 瞬时攻击 / 平滑释放
（min(目标增益, 释放平滑) 技巧向量化实现，无逐采样 Python 循环）。

增益为全声道共享（取各声道最大包络），不破坏立体声平衡。
Seek 时调用 reset() 清空延迟线与包络状态；前瞻延迟会在流起点引入约 5ms 静音。
"""
from __future__ import annotations
import numpy as np

try:
    from scipy.signal import lfilter
    from scipy.ndimage import maximum_filter1d
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


class SoftLimiter:
    """前瞻软限幅器。

    threshold_db: 输出天花板（默认 -0.5dB）
    release_ms:   增益恢复时间常数
    knee_db:      软膝宽度（threshold ~ threshold·10^(knee/20) 之间平滑过渡）
    """

    def __init__(self, fs: int,
                 threshold_db: float = -0.5,
                 release_ms: float = 150.0,
                 knee_db: float = 3.0):
        if not _HAS_SCIPY:
            raise RuntimeError("限幅器需要 scipy")

        self.fs = fs
        self.threshold = float(10 ** (threshold_db / 20.0))
        self.knee = float(knee_db)
        self.release_coeff = float(np.exp(-1.0 / (fs * release_ms / 1000.0)))
        self.enabled = True

        # 前瞻延迟长度（样本数）
        self._delay = max(1, int(fs * 0.005))
        # maximum_filter1d 的 origin：窗口起点偏移 s = -(origin + size//2)，
        # 要得到前向窗口 [i, i+delay) 需 origin = -delay//2（实测验证）
        self._max_origin = -(self._delay // 2)
        # 各声道输入延迟历史（上一块的最后 delay 个样本）
        self._x_hist = np.zeros((0, 0))
        self._channels = 0
        # 释放平滑的一极滤波器状态
        self._zi_release = np.zeros(1)

    def process(self, data: np.ndarray) -> np.ndarray:
        """处理音频块。data: float32, (n,) 或 (n, channels)。返回同形状数组。"""
        if not self.enabled:
            return data
        if not _HAS_SCIPY:
            return data

        single = data.ndim == 1
        x = data.reshape(-1, 1) if single else data
        n, n_ch = x.shape
        if self._channels != n_ch:
            self._channels = n_ch
            self._x_hist = np.zeros((self._delay, n_ch))

        # 拼接延迟历史：combined 的前 delay 个样本是 5ms 前的输入
        combined = np.vstack([self._x_hist, x]) if n > 0 else self._x_hist

        # 延迟后的输出块（当前输出的样本在输入端已是 5ms 前的值）
        delayed = combined[:n, :]

        # 前瞻峰值：对 combined 取滑动窗 [i, i+delay) 的最大绝对值，
        # 输出样本 i 的增益在瞬态到达前已就绪
        if n > 0:
            abs_comb = np.max(np.abs(combined), axis=1)
            lookahead_peak = maximum_filter1d(
                abs_comb, size=self._delay,
                origin=self._max_origin, mode="nearest")[:n]
        else:
            lookahead_peak = np.zeros(0)

        # 软膝目标增益
        gain = _soft_knee_gain(lookahead_peak, self.threshold, self.knee)

        # 瞬时攻击 + 平滑释放：min(目标, 释放平滑)
        if n > 0:
            smoothed, self._zi_release = lfilter(
                [1.0 - self.release_coeff], [1.0, -self.release_coeff],
                gain, zi=self._zi_release)
            gain = np.minimum(gain, smoothed)

        out = delayed * gain[:, np.newaxis]

        # 更新延迟历史为最近 delay 个输入样本
        if n > 0:
            self._x_hist = combined[-self._delay:, :].copy()

        return out[:, 0] if single else out

    def reset(self):
        """清空延迟线与包络状态（Seek 时调用）"""
        self._x_hist = np.zeros((self._delay, max(self._channels, 1)))
        self._zi_release = np.zeros(1)


def _soft_knee_gain(peak: np.ndarray, threshold: float, knee_db: float) -> np.ndarray:
    """由前瞻峰值计算软膝目标增益（≤1），向量化分段实现。"""
    gain = np.ones_like(peak)
    if peak.size == 0:
        return gain
    knee_upper = threshold * (10 ** (knee_db / 20.0))

    above = peak > threshold
    if not above.any():
        return gain

    p = peak[above]
    linear_gain = threshold / p
    # 软膝区间内做二次平滑过渡，膝外走线性
    t = np.clip((p - threshold) / (knee_upper - threshold), 0.0, 1.0)
    g = 1.0 + t * t * (linear_gain - 1.0)
    gain[above] = g
    return gain
