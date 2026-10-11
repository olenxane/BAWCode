"""响度均衡：基于 ITU-R BS.1770-4 的 LUFS 测量与播放增益计算。
后台线程分析整曲，不创建缓存文件，分析结果仅存于 Player 内存。

测量要点（BS.1770-4）：
  - 各声道均方能量求和（立体声 L=R=1.0 各计一次），而非跨声道取平均；
  - 相对门限在能量域取均值再转 dB，不能对 dB 值直接平均。

measure_dual(): 一次解码同时测量干信号（原始）与湿信号（过效果链后）
的响度，供播放器实现"开关音效响度不变"的自动电平匹配。
"""
from __future__ import annotations
import math
import threading
import numpy as np
from typing import Optional, Callable

try:
    from scipy.signal import lfilter, tf2zpk, bilinear_zpk, zpk2tf
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ITU-R BS.1770-4 常量
_ABS_GATE_LUFS = -70.0
_REL_GATE_OFFSET_LU = -10.0
_BLOCK_MS = 400.0
_STEP_MS = 100.0

# ITU-R BS.1770-4 官方 48kHz K-weighting 系数（a0=1 归一形态）。
# 其他采样率通过"提取模拟原型 → 双线性变换"在运行时重推（与 libebur128/
# ffmpeg f_ebur128 一致），保证 1kHz≈0dB、5kHz 以上 +4dB 的标准曲线。
_FS48 = 48000.0
_PRE_B_48K = (1.53512485958697, -2.69169618940638, 1.19839281085285)
_PRE_A_48K = (1.0, -1.69065929318241, 0.73248077421585)
_RLB_B_48K = (1.0, -1.99004745483398, 0.99007225036621)
_RLB_A_48K = (1.0, -1.99004745483398, 0.99007225036621)

_k_weighting_cache: dict = {}


def _analog_prototype(b48, a48, fs48=_FS48):
    """把 48kHz 数字系数逆映射回模拟原型（s 平面极零点 + DC 增益对齐）"""
    z, p, _ = tf2zpk(b48, a48)
    z = np.asarray(z, complex)
    p = np.asarray(p, complex)
    z_a = 2 * fs48 * (z - 1) / (z + 1)
    p_a = 2 * fs48 * (p - 1) / (p + 1)
    dc = np.sum(b48) / np.sum(a48)  # 双线性变换保 DC：H_a(0) = H_d(z=1)
    k_a = dc * np.prod(-p_a) / np.prod(-z_a)
    return z_a, p_a, k_a


def _design_at_fs(b48, a48, fs):
    """在目标采样率重做双线性变换，返回 (b, a)"""
    z_a, p_a, k_a = _analog_prototype(b48, a48)
    z2, p2, k2 = bilinear_zpk(z_a, p_a, k_a, fs=fs)
    b, a = zpk2tf(z2, p2, k2)
    # 极零点共轭成对，多项式系数的虚部只是浮点残差（~1e-17）；
    # 显式取实部，避免 lfilter 输出复数触发 ComplexWarning 与复数运算开销
    if np.iscomplexobj(b):
        b = b.real
    if np.iscomplexobj(a):
        a = a.real
    return b, a


def _k_weighting_coeffs(fs: float):
    """K-weighting 两级滤波器系数（pre-filter + RLB），带缓存"""
    key = float(fs)
    cached = _k_weighting_cache.get(key)
    if cached is not None:
        return cached
    b_pre, a_pre = _design_at_fs(_PRE_B_48K, _PRE_A_48K, fs)
    b_rlb, a_rlb = _design_at_fs(_RLB_B_48K, _RLB_A_48K, fs)
    result = (b_pre, a_pre), (b_rlb, a_rlb)
    _k_weighting_cache[key] = result
    return result


def _integrated_lufs(block_means: list) -> float:
    """由各 400ms 块的均方能量计算带门限的综合响度（BS.1770-4 门限流程）"""
    if not block_means:
        return float("-inf")
    # 绝对门限 -70 LUFS
    gated = [ms for ms in block_means
             if ms > 0 and (-0.691 + 10 * math.log10(ms)) > _ABS_GATE_LUFS]
    if not gated:
        return float("-inf")
    # 相对门限：能量域均值转 dB 后 -10 LU
    mean_lufs = -0.691 + 10 * math.log10(sum(gated) / len(gated))
    rel_gate = mean_lufs + _REL_GATE_OFFSET_LU
    gated2 = [ms for ms in gated
              if (-0.691 + 10 * math.log10(ms)) > rel_gate]
    if not gated2:
        gated2 = gated
    return -0.691 + 10 * math.log10(sum(gated2) / len(gated2))


class LoudnessAnalyzer:
    """ITU-R BS.1770-4 响度分析器。
    在后台线程调用，一次解码可同时测量干/湿响度。
    不创建缓存文件，不修改原始音频。"""

    def __init__(self, target_lufs: float = -16.0,
                 max_boost: float = 6.0, max_cut: float = -12.0):
        self.target_lufs = target_lufs
        self.max_boost = max_boost
        self.max_cut = max_cut

    def _k_weighting_coeffs(self, fs: float):
        """K-weighting 两级滤波器系数（pre-filter + RLB），见模块级同名函数"""
        return _k_weighting_coeffs(fs)

    def measure(self, audio_path: str, fs: int, channels: int,
                headers: dict | None = None,
                cancel_event: Optional[threading.Event] = None) -> tuple:
        """分析整曲干信号响度，返回 (measured_lufs, gain_db)。
        失败时返回 (-inf, 0.0)。headers 透传给 FFmpegAudioFile（在线直链需要）。"""
        dry, _wet = self._measure_stream(audio_path, fs, channels,
                                         headers, cancel_event, None)
        gain_db = 0.0
        if dry != float("-inf"):
            gain_db = float(np.clip(self.target_lufs - dry,
                                    self.max_cut, self.max_boost))
        return dry, gain_db

    def measure_dual(self, audio_path: str, fs: int, channels: int,
                     headers: dict | None = None,
                     cancel_event: Optional[threading.Event] = None,
                     fx_process: Optional[Callable] = None) -> tuple:
        """一次解码同时测量干信号与过效果链后的湿信号响度。
        fx_process(block)->block 为效果链离线回调（不含响度补偿与限幅后的 makeup），
        返回 (dry_lufs, wet_lufs)；效果链失败时 wet 为 -inf。"""
        return self._measure_stream(audio_path, fs, channels,
                                    headers, cancel_event, fx_process)

    def _measure_stream(self, audio_path: str, fs: int, channels: int,
                        headers: dict | None,
                        cancel_event: Optional[threading.Event],
                        fx_process: Optional[Callable]) -> tuple:
        if not _HAS_SCIPY:
            return float("-inf"), float("-inf")

        from .ffmpeg_decoder import FFmpegAudioFile

        (b_pre, a_pre), (b_rlb, a_rlb) = self._k_weighting_coeffs(fs)

        block_size = int(fs * _BLOCK_MS / 1000)
        step_size = int(fs * _STEP_MS / 1000)

        # K-weighting 滤波器状态（干/湿各一套，每声道独立）
        zi_pre = [np.zeros(2) for _ in range(channels)]
        zi_rlb = [np.zeros(2) for _ in range(channels)]
        zi_pre_w = [np.zeros(2) for _ in range(channels)]
        zi_rlb_w = [np.zeros(2) for _ in range(channels)]

        block_means = []      # 干信号各块均方能量
        block_means_w = []    # 湿信号各块均方能量
        fx_failed = fx_process is None

        # 环形缓冲区，跨 block 重叠（干/湿可共用步进结构，各自缓存）
        overlap_buf = np.zeros((0, channels), dtype=np.float32)
        overlap_buf_w = np.zeros((0, channels), dtype=np.float32)

        try:
            f = FFmpegAudioFile(audio_path, headers=headers)
        except Exception:
            return float("-inf"), float("-inf")

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return float("-inf"), float("-inf")
                chunk = f.read(block_size, dtype="float32", always_2d=True)
                if chunk.shape[0] == 0:
                    break

                # 湿信号：先过效果链（失败则放弃湿路测量，不影响干路）
                chunk_w = chunk
                if not fx_failed:
                    try:
                        processed = fx_process(chunk)
                        if processed is not None and processed.shape == chunk.shape:
                            chunk_w = processed
                    except Exception:
                        fx_failed = True
                        chunk_w = chunk

                # K-weighting：干/湿各自独立滤波
                weighted = np.empty_like(chunk)
                weighted_w = np.empty_like(chunk_w)
                for ch in range(min(channels, chunk.shape[1])):
                    x = chunk[:, ch]
                    x, zi_pre[ch] = lfilter(b_pre, a_pre, x, zi=zi_pre[ch])
                    x, zi_rlb[ch] = lfilter(b_rlb, a_rlb, x, zi=zi_rlb[ch])
                    weighted[:, ch] = x
                    xw = chunk_w[:, ch]
                    xw, zi_pre_w[ch] = lfilter(b_pre, a_pre, xw, zi=zi_pre_w[ch])
                    xw, zi_rlb_w[ch] = lfilter(b_rlb, a_rlb, xw, zi=zi_rlb_w[ch])
                    weighted_w[:, ch] = xw

                block_means.extend(_extract_block_means(
                    weighted, overlap_buf, block_size, step_size))
                overlap_buf = _update_overlap(
                    weighted, overlap_buf, block_size, step_size)
                block_means_w.extend(_extract_block_means(
                    weighted_w, overlap_buf_w, block_size, step_size))
                overlap_buf_w = _update_overlap(
                    weighted_w, overlap_buf_w, block_size, step_size)

                if chunk.shape[0] < block_size:
                    break
        except Exception:
            return float("-inf"), float("-inf")
        finally:
            f.close()

        dry_lufs = _integrated_lufs(block_means)
        if fx_process is None or fx_failed:
            return dry_lufs, float("-inf")
        wet_lufs = _integrated_lufs(block_means_w)
        return dry_lufs, wet_lufs


def _extract_block_means(weighted: np.ndarray, overlap_buf: np.ndarray,
                         block_size: int, step_size: int) -> list:
    """从拼接缓冲中提取所有完整 400ms 块的均方能量（各声道求和）"""
    combined = np.vstack([overlap_buf, weighted]) if overlap_buf.shape[0] > 0 else weighted
    n_blocks = (combined.shape[0] - block_size) // step_size + 1
    means = []
    for i in range(n_blocks):
        block = combined[i * step_size:i * step_size + block_size]
        # BS.1770：各声道均方能量求和（非跨声道平均）
        ms = np.mean(block ** 2, axis=0).sum()
        means.append(float(ms))
    return means


def _update_overlap(weighted: np.ndarray, overlap_buf: np.ndarray,
                    block_size: int, step_size: int) -> np.ndarray:
    """保留未被完整块消费的尾部样本，供下一块拼接"""
    combined = np.vstack([overlap_buf, weighted]) if overlap_buf.shape[0] > 0 else weighted
    n_blocks = (combined.shape[0] - block_size) // step_size + 1
    used = n_blocks * step_size if n_blocks > 0 else 0
    return combined[used:] if used < combined.shape[0] else np.zeros(
        (0, combined.shape[1]), dtype=np.float32)
