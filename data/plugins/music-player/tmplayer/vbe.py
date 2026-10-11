"""虚拟低音增强 v2（Virtual Bass Enhancer）。

从小扬声器/耳机难以回放的 30-120Hz 低频中提取基频，经非线性处理生成
谐波（人耳会"脑补"出缺失的基频），限带后混回原信号。

v2 相对 v1 的改进：
  1. 谐波强度随低频包络起伏（滑动峰值 + 一极平滑），避免恒定强度造成的
     "假低音发闷"；
  2. 增加全波整流偶次谐波路径（60-240Hz 带通），低音更暖、更有弹性，
     与原有的三次谐波（硬、有攻击性）互补；
  3. cut-then-boost：混回谐波前对原信号 80Hz 处挖 -1.5dB，腾出空间防浑浊。

所有 IIR 滤波器保存内部状态，支持流式分块处理；seek 时调用 reset() 清空。
"""
from __future__ import annotations
import numpy as np

try:
    from scipy.signal import butter, lfilter
    from scipy.ndimage import maximum_filter1d
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


class VirtualBassEnhancer:
    """VBE 处理器：HPF→LPF 提取低频 → 奇/偶次谐波 → 带通限带 → 包络调制 → 混合。"""

    def __init__(self, fs: int, channels: int = 2,
                 gain_db: float = -3.0,
                 hpf_fc: float = 30.0,
                 lpf_fc: float = 120.0,
                 bpf_low: float = 125.0,
                 bpf_high: float = 275.0):
        if not _HAS_SCIPY:
            raise RuntimeError("VBE 需要 scipy")

        self.fs = fs
        self.channels = channels
        self.gain = float(10 ** (gain_db / 20.0))
        self.enabled = True

        nyq = fs / 2.0

        # 滤波器系数
        self._b_hpf, self._a_hpf = butter(2, hpf_fc / nyq, btype="high")
        self._b_lpf, self._a_lpf = butter(2, lpf_fc / nyq, btype="low")
        self._b_bpf, self._a_bpf = butter(2, [bpf_low / nyq, bpf_high / nyq],
                                          btype="band")
        # 偶次谐波带通（2 次谐波落点区间）
        self._b_ev, self._a_ev = butter(2, [60.0 / nyq, 240.0 / nyq], btype="band")
        # cut-then-boost：80Hz 处 -1.5dB 挖槽（RBJ peaking）
        self._b_dip, self._a_dip = self._peaking_eq(80.0, -1.5, 1.0, fs)

        # 包络参数：滑窗峰值提供快速攻击，一极平滑提供释放
        self._env_attack_win = max(1, int(fs * 0.020))   # 20ms 峰值窗
        self._env_release_a = float(np.exp(-1.0 / (fs * 0.150)))  # 150ms 释放
        self._peak_release_a = float(np.exp(-1.0 / (fs * 2.0)))   # 2s 慢峰值跟踪

        # 每声道独立的滤波器状态（状态长度 = 滤波器系数长度 - 1）
        def _zi_len(b, a):
            return max(len(b), len(a)) - 1

        self._zi_hpf = [np.zeros(_zi_len(self._b_hpf, self._a_hpf)) for _ in range(channels)]
        self._zi_lpf = [np.zeros(_zi_len(self._b_lpf, self._a_lpf)) for _ in range(channels)]
        self._zi_bpf = [np.zeros(_zi_len(self._b_bpf, self._a_bpf)) for _ in range(channels)]
        self._zi_ev = [np.zeros(_zi_len(self._b_ev, self._a_ev)) for _ in range(channels)]
        self._zi_dip = [np.zeros(_zi_len(self._b_dip, self._a_dip)) for _ in range(channels)]
        self._zi_env = [np.zeros(1) for _ in range(channels)]   # 包络平滑状态
        self._zi_peak = [np.zeros(1) for _ in range(channels)]  # 慢峰值跟踪状态

    @staticmethod
    def _peaking_eq(freq, gain_db, q, fs):
        """RBJ cookbook peaking EQ 系数"""
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
        return np.array([b0, b1, b2]) / a0, np.array([1.0, a1 / a0, a2 / a0])

    def process(self, data: np.ndarray) -> np.ndarray:
        """处理音频块。data: shape (n, channels) float32。返回与 data 相同形状的数组。"""
        if not self.enabled:
            return data
        if not _HAS_SCIPY:
            return data

        out = data.copy()
        n_ch = min(out.shape[1] if out.ndim > 1 else 1, self.channels)

        for ch in range(n_ch):
            x = out[:, ch] if out.ndim > 1 else out

            # ---- 低频提取：HPF 30Hz → LPF 120Hz ----
            bass, self._zi_hpf[ch] = lfilter(self._b_hpf, self._a_hpf, x, zi=self._zi_hpf[ch])
            bass, self._zi_lpf[ch] = lfilter(self._b_lpf, self._a_lpf, bass, zi=self._zi_lpf[ch])

            # ---- 包络跟随：滑动峰值（快速攻击）+ 一极平滑（150ms 释放）----
            rect = np.abs(bass)
            peak = maximum_filter1d(rect, self._env_attack_win)
            env, self._zi_env[ch] = lfilter(
                [1.0 - self._env_release_a], [1.0, -self._env_release_a],
                peak, zi=self._zi_env[ch])
            # 慢峰值跟踪：把包络归一化到近期的相对最大值（0~1）
            slow, self._zi_peak[ch] = lfilter(
                [1.0 - self._peak_release_a], [1.0, -self._peak_release_a],
                env, zi=self._zi_peak[ch])
            mod = np.clip(env / (slow + 1e-6), 0.0, 1.0)
            # 调制系数留 35% 地板：安静低音仍有基础增强
            mod_gain = 0.35 + 0.65 * mod

            # ---- 奇次谐波路径：对称软饱和（原 v1）----
            b = np.clip(bass, -1.0, 1.0)
            harm_odd = -(b ** 3) / 3.0
            harm_odd, self._zi_bpf[ch] = lfilter(self._b_bpf, self._a_bpf,
                                                 harm_odd, zi=self._zi_bpf[ch])

            # ---- 偶次谐波路径：全波整流 → 带通 60-240Hz ----
            harm_even, self._zi_ev[ch] = lfilter(self._b_ev, self._a_ev,
                                                 rect, zi=self._zi_ev[ch])

            # ---- 混合：先对原信号挖槽（cut），再加调制后的谐波（boost）----
            damped, self._zi_dip[ch] = lfilter(self._b_dip, self._a_dip, x,
                                               zi=self._zi_dip[ch])
            harmonics = (harm_odd + 0.6 * harm_even) * (self.gain * mod_gain)
            if out.ndim > 1:
                out[:, ch] = damped + harmonics
            else:
                out = damped + harmonics

        return out

    def reset(self):
        """清空所有滤波器状态（Seek 时调用）"""
        def _zi_len(b, a):
            return max(len(b), len(a)) - 1

        self._zi_hpf = [np.zeros(_zi_len(self._b_hpf, self._a_hpf)) for _ in range(self.channels)]
        self._zi_lpf = [np.zeros(_zi_len(self._b_lpf, self._a_lpf)) for _ in range(self.channels)]
        self._zi_bpf = [np.zeros(_zi_len(self._b_bpf, self._a_bpf)) for _ in range(self.channels)]
        self._zi_ev = [np.zeros(_zi_len(self._b_ev, self._a_ev)) for _ in range(self.channels)]
        self._zi_dip = [np.zeros(_zi_len(self._b_dip, self._a_dip)) for _ in range(self.channels)]
        self._zi_env = [np.zeros(1) for _ in range(self.channels)]
        self._zi_peak = [np.zeros(1) for _ in range(self.channels)]
