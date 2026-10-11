"""声场展宽器（Stereo Width Enhancer）。

M/S（中侧）处理消除"头中效应"、扩大声场：
    M = (L+R)/2（中，含全部低频能量）
    S = (L-R)/2（侧，立体声信息）
    S 通道高通（默认 250Hz）→ width 增益 → 重建 L' = M + width·S'，R' = M - width·S'
侧通道被高通后，低频自动保持单声道居中（低频底盘与单声道兼容性不受损）。

可选 Freeverb 风格房间混响（8 反馈梳状 + 4 全通，立体声错位延迟），
以极低湿量叠加环境声（对应流媒体音效的"环境强度"）。混响以加法混合
（dry 全保留 + wet·room_mix），不改变整体响度结构。

反馈梳状/全通滤波器采用"段式向量化"实现：段长不超过延迟样本数时，
段内输出只依赖段外历史，可用 numpy 整段运算，避免逐采样 Python 循环。

非立体声（单声道/多于2声道）自动旁路。
"""
from __future__ import annotations
import numpy as np

try:
    from scipy.signal import butter, lfilter
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# Freeverb 标准梳状延迟（44.1kHz 基准，按实际采样率缩放）
_COMB_DELAYS = [1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617]
# Freeverb 标准全通延迟
_ALLPASS_DELAYS = [556, 441, 341, 225]
_COMB_FEEDBACK = 0.84     # 房间尺寸对应的反馈系数
_ALLPASS_FEEDBACK = 0.5
_STEREO_SPREAD = 23       # 左右声道延迟错位样本数（44.1kHz 基准）


def _comb_process(x: np.ndarray, delay: int, gain: float,
                  x_hist: np.ndarray, y_hist: np.ndarray):
    """反馈梳状滤波器 y[n] = x[n] + gain·y[n-delay]（段式向量化）。

    x_hist/y_hist 为最近 delay 个输入/输出样本，返回 (y, 新x_hist, 新y_hist)。
    """
    n = len(x)
    y = np.empty_like(x)
    pos = 0
    while pos < n:
        seg = min(delay, n - pos)
        y[pos:pos + seg] = x[pos:pos + seg] + gain * y_hist[:seg]
        y_hist = np.concatenate([y_hist[seg:], y[pos:pos + seg]])
        x_hist = np.concatenate([x_hist[seg:], x[pos:pos + seg]])
        pos += seg
    return y, x_hist, y_hist


def _allpass_process(x: np.ndarray, delay: int, gain: float,
                     x_hist: np.ndarray, y_hist: np.ndarray):
    """Schroeder 全通 y[n] = x[n-delay] + gain·(y[n-delay] - x[n])（段式向量化）。"""
    n = len(x)
    y = np.empty_like(x)
    pos = 0
    while pos < n:
        seg = min(delay, n - pos)
        y[pos:pos + seg] = x_hist[:seg] + gain * (y_hist[:seg] - x[pos:pos + seg])
        y_hist = np.concatenate([y_hist[seg:], y[pos:pos + seg]])
        x_hist = np.concatenate([x_hist[seg:], x[pos:pos + seg]])
        pos += seg
    return y, x_hist, y_hist


class _FreeverbChannel:
    """单声道 Freeverb 混响链：8 梳状并联 → 4 全通串联（带流式历史）"""

    def __init__(self, fs: int, delay_offset: int):
        scale = fs / 44100.0
        self.comb_delay = [max(2, int(round(d * scale)) + delay_offset) for d in _COMB_DELAYS]
        self.ap_delay = [max(2, int(round(d * scale))) for d in _ALLPASS_DELAYS]
        # 每个梳状的输入/输出历史（阻尼用输入端一极低通近似）
        self.comb_xh = [np.zeros(d) for d in self.comb_delay]
        self.comb_yh = [np.zeros(d) for d in self.comb_delay]
        self.ap_xh = [np.zeros(d) for d in self.ap_delay]
        self.ap_yh = [np.zeros(d) for d in self.ap_delay]
        # 梳状输入阻尼一极低通（高频在反馈路径中衰减更快）
        damp_c = 0.35
        self._damp_b = [1.0 - damp_c]
        self._damp_a = [1.0, -damp_c]
        self._zi_damp = np.zeros(1)

    def process(self, x: np.ndarray) -> np.ndarray:
        # 并联梳状（输入先经阻尼低通）
        wet = np.zeros_like(x)
        damped, self._zi_damp = lfilter(self._damp_b, self._damp_a, x, zi=self._zi_damp)
        for i, d in enumerate(self.comb_delay):
            out, self.comb_xh[i], self.comb_yh[i] = _comb_process(
                damped, d, _COMB_FEEDBACK, self.comb_xh[i], self.comb_yh[i])
            wet += out
        wet *= (1.0 / len(self.comb_delay))
        # 串联全通扩散
        for i, d in enumerate(self.ap_delay):
            wet, self.ap_xh[i], self.ap_yh[i] = _allpass_process(
                wet, d, _ALLPASS_FEEDBACK, self.ap_xh[i], self.ap_yh[i])
        return wet

    def reset(self):
        self.comb_xh = [np.zeros(d) for d in self.comb_delay]
        self.comb_yh = [np.zeros(d) for d in self.comb_delay]
        self.ap_xh = [np.zeros(d) for d in self.ap_delay]
        self.ap_yh = [np.zeros(d) for d in self.ap_delay]
        self._zi_damp = np.zeros(1)


class StereoWidener:
    """声场展宽器：M/S 展宽 + 低频单声道保护 + 可选房间混响。

    enabled/width/room_mix 可运行时调整；width≈1 且混响关闭时零改动直通。
    """

    def __init__(self, fs: int, channels: int = 2,
                 width: float = 1.3,
                 hp_freq_hz: float = 250.0,
                 room_mix: float = 0.0):
        self.fs = fs
        self.channels = channels
        self.enabled = True
        self.width = float(min(max(width, 1.0), 2.0))
        self.room_mix = float(min(max(room_mix, 0.0), 1.0))
        self._hp_freq_hz = float(min(max(hp_freq_hz, 80.0), 500.0))

        self._side_zi = None
        self._reverb = None
        self._build()

    def _build(self):
        """按当前参数构建/重建内部滤波器与混响（参数变更时调用）"""
        if not _HAS_SCIPY:
            return
        nyq = self.fs / 2.0
        hp_fc = float(min(max(self._hp_freq_hz, 80.0), nyq * 0.45))
        self._b_side, self._a_side = butter(2, hp_fc / nyq, btype="high")
        self._side_zi = np.zeros(max(len(self._b_side), len(self._a_side)) - 1)
        self._reverb = ([_FreeverbChannel(self.fs, 0),
                         _FreeverbChannel(self.fs, _STEREO_SPREAD)]
                        if self.room_mix > 0.0 else None)

    # hp_freq_hz 通过属性写入，变更时自动重建
    @property
    def hp_freq_hz(self) -> float:
        return self._hp_freq_hz

    @hp_freq_hz.setter
    def hp_freq_hz(self, value: float):
        self._hp_freq_hz = float(value)
        self._build()

    def process(self, data: np.ndarray) -> np.ndarray:
        """处理音频块。data: shape (n, channels) float32，返回同形状数组。

        非立体声、未启用、或 width≈1 且混响关闭时原样返回。
        """
        if not self.enabled:
            return data
        if data.ndim < 2 or data.shape[1] != 2 or not _HAS_SCIPY:
            return data  # 单声道/多声道不处理
        if self.width <= 1.001 and self.room_mix <= 0.0:
            return data

        out = data.copy()
        mid = (out[:, 0] + out[:, 1]) * 0.5
        side = (out[:, 0] - out[:, 1]) * 0.5

        # 侧通道高通：低频保持居中
        if self.width > 1.001:
            side, self._side_zi = lfilter(self._b_side, self._a_side, side,
                                          zi=self._side_zi)
            side = side * self.width
            out[:, 0] = mid + side
            out[:, 1] = mid - side

        # 可选房间混响：干信号全保留，湿信号按 room_mix 叠加
        if self.room_mix > 0.0 and self._reverb is not None:
            wet_l = self._reverb[0].process(np.ascontiguousarray(out[:, 0]))
            wet_r = self._reverb[1].process(np.ascontiguousarray(out[:, 1]))
            out[:, 0] += wet_l * self.room_mix
            out[:, 1] += wet_r * self.room_mix

        return out

    def reset(self):
        """清空所有状态（seek 时调用）"""
        if self._side_zi is not None:
            self._side_zi[:] = 0
        if self._reverb is not None:
            for ch in self._reverb:
                ch.reset()
