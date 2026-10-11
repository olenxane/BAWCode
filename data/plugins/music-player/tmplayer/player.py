"""播放引擎：基于 ffmpeg 流式解码 + sounddevice 输出。

管线架构（v2）：
    ffmpeg 子进程 → 解码线程（读块 → DSP 链 → 环形缓冲）→ 音频回调（只做拷贝+音量）

DSP 链顺序由 dsp.chain 配置决定（每级可独立关闭），默认：
    EQ → VBE虚拟低音 → 激励器 → 声场展宽 → LUFS响度补偿 → 前瞻限幅
响度/电平匹配增益以块内线性斜坡过渡，避免中途音量跳变。
解码线程持有所有可能阻塞的操作（网络流读取、DSP 计算），
音频回调仅从环形缓冲拷贝数据，欠载时输出静音并计数。
"""
from __future__ import annotations
import threading
from enum import Enum
from typing import Optional, Callable

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None

from .equalizer import Equalizer
from .spectrum import SpectrumAnalyzer
from .ffmpeg_decoder import FFmpegAudioFile, ffmpeg_available

# ---- 可选 DSP 模块（缺失/不可用不影响主播放链路） ----
try:
    from .loudness import LoudnessAnalyzer
    _HAS_LOUDNESS = True
except ImportError:
    _HAS_LOUDNESS = False

try:
    from .vbe import VirtualBassEnhancer
    _HAS_VBE = True
except ImportError:
    _HAS_VBE = False

try:
    from .limiter import SoftLimiter
    _HAS_LIMITER = True
except ImportError:
    _HAS_LIMITER = False

try:
    from .exciter import Exciter
    _HAS_EXCITER = True
except ImportError:
    _HAS_EXCITER = False

try:
    from .stereo_widener import StereoWidener
    _HAS_WIDENER = True
except ImportError:
    _HAS_WIDENER = False


class PlayState(Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class _RingBuffer:
    """预分配环形缓冲（float32），解码线程写、音频回调读，Lock 保护。

    write() 由解码线程在确认空间足够后调用（阻塞策略在线程侧实现），
    read() 只取现有数据、绝不等待，保证回调不阻塞。
    """

    def __init__(self, capacity: int, channels: int):
        self._buf = np.zeros((capacity, channels), dtype=np.float32)
        self._cap = capacity
        self._w = 0
        self._r = 0
        self.count = 0

    @property
    def available(self) -> int:
        return self.count

    @property
    def free(self) -> int:
        return self._cap - self.count

    def write(self, block: np.ndarray) -> None:
        n = min(block.shape[0], self.free)  # 越界保护：绝不覆盖未读数据
        if n <= 0:
            return
        end = self._w + n
        if end <= self._cap:
            self._buf[self._w:end] = block
        else:
            first = self._cap - self._w
            self._buf[self._w:] = block[:first]
            self._buf[:n - first] = block[first:]
        self._w = (self._w + n) % self._cap
        self.count += n

    def read(self, frames: int) -> np.ndarray:
        """读取至多 frames 帧（跨环绕段自动拼接），不足时返回全部剩余"""
        n = min(frames, self.count)
        if n <= 0:
            return np.zeros((0, self._buf.shape[1]), dtype=np.float32)
        out = np.empty((n, self._buf.shape[1]), dtype=np.float32)
        end = self._r + n
        if end <= self._cap:
            out[:] = self._buf[self._r:end]
        else:
            first = self._cap - self._r
            out[:first] = self._buf[self._r:]
            out[first:] = self._buf[:n - first]
        self._r = (self._r + n) % self._cap
        self.count -= n
        return out

    def clear(self) -> None:
        self._r = 0
        self._w = 0
        self.count = 0


class Player:
    # 解码线程每次读取的帧数（约 93ms @44.1kHz，兼顾 DSP 向量化效率与延迟）
    _DECODE_CHUNK = 4096
    # 环形缓冲时长（秒）：1 秒的余量足以吸收 UI/网络造成的短暂 GIL 争抢
    _RING_SECONDS = 1.0

    def __init__(self, spectrum: SpectrumAnalyzer, block_size: int = 1024,
                 volume: float = 0.8, on_track_end: Optional[Callable] = None,
                 dsp_config=None):
        if sd is None:
            raise RuntimeError(
                "缺少音频依赖，请先安装: pip install sounddevice"
            )
        if not ffmpeg_available():
            raise RuntimeError(
                "未找到 ffmpeg，请安装 ffmpeg 或运行 pip install imageio-ffmpeg"
            )
        self.spectrum = spectrum
        self.block_size = block_size
        self.volume = float(np.clip(volume, 0.0, 1.5))
        self.on_track_end = on_track_end

        # DSP 配置（各模块可独立开关，顺序由 chain 决定）
        self.dsp_config = dsp_config
        self.loudness_enabled = bool(dsp_config and dsp_config.loudness.enabled)
        self.vbe_enabled = bool(dsp_config and dsp_config.vbe.enabled)
        self.limiter_enabled = bool(dsp_config and dsp_config.limiter.enabled)
        self.exciter_enabled = bool(dsp_config and dsp_config.exciter.enabled)
        self.widener_enabled = bool(dsp_config and dsp_config.widener.enabled)
        self.loudness_target_lufs = -16.0
        if dsp_config:
            self.loudness_target_lufs = dsp_config.loudness.target_lufs

        self.state = PlayState.STOPPED
        self.equalizer: Optional[Equalizer] = None
        self.vbe = None
        self.limiter = None
        self.exciter = None
        self.widener = None
        self._file: Optional[FFmpegAudioFile] = None
        self._stream: Optional[sd.OutputStream] = None
        self._lock = threading.Lock()
        self._frames_played = 0
        self._frames_listened = 0
        self._samplerate = 44100
        self._channels = 2
        self._duration_frames = 0
        self._path = ""
        self._headers = None
        self._eq_bands = None
        self._eq_q = None
        self._eof_notified = False
        self._seek_target: Optional[int] = None

        # 解码线程 / 环形缓冲
        self._ring: Optional[_RingBuffer] = None
        self._decode_thread: Optional[threading.Thread] = None
        self._decode_running = False
        self._decode_session = 0  # 会话号：旧线程发现过期后自行退出，避免双写
        self._decode_wake = threading.Event()
        self._decode_done = False
        self.underruns = 0

        # 响度/电平匹配增益（线性，分析线程写、解码线程读；GIL 下原子）
        self.loudness_gain_db = 0.0
        self._loudness_gain_linear = 1.0
        self._current_gain = 1.0
        self._loudness_generation = 0
        self._loudness_task: Optional[threading.Thread] = None
        self._loudness_cancel: Optional[threading.Event] = None

    # ---------- 属性 ----------
    # 这些属性仅读单值且只在回调/加载路径写入（GIL 下原子），不加锁。
    @property
    def position_sec(self) -> float:
        return self._frames_played / self._samplerate if self._samplerate else 0.0

    @property
    def listened_sec(self) -> float:
        """实际已听秒数：仅正常播放时递增，不含 seek 跳变（统计30秒判定用）"""
        return self._frames_listened / self._samplerate if self._samplerate else 0.0

    @property
    def duration_sec(self) -> float:
        return self._duration_frames / self._samplerate if self._samplerate else 0.0

    @property
    def path(self) -> str:
        return self._path

    # ---------- 加载/控制 ----------
    def load(self, path: str, eq_bands=None, q_values=None, headers=None):
        self.stop()
        self._file = FFmpegAudioFile(path, headers=headers)
        self._samplerate = self._file.samplerate
        self._channels = self._file.channels
        self._duration_frames = len(self._file)
        self._frames_played = 0
        self._frames_listened = 0
        self._path = path
        self._headers = headers
        self._eq_bands = list(eq_bands) if eq_bands else [0.0] * 10
        self._eq_q = list(q_values) if q_values else None
        self._eof_notified = False
        self.underruns = 0
        self.equalizer = Equalizer(fs=self._samplerate,
                                    bands_db=self._eq_bands,
                                    q_values=self._eq_q,
                                    channels=self._channels)
        self.spectrum.fs = self._samplerate
        self._ring = _RingBuffer(int(self._samplerate * self._RING_SECONDS),
                                 self._channels)

        # 初始化可选 DSP 模块
        self._init_dsp_modules()

        # 后台启动响度/电平匹配分析（不阻塞播放）
        self._start_loudness_analysis(path, headers=headers)

    def _init_dsp_modules(self):
        """按配置初始化各可选 DSP 模块（模块缺失或关闭则置 None）"""
        self.vbe = None
        self.limiter = None
        self.exciter = None
        self.widener = None
        cfg = self.dsp_config
        if cfg is None:
            return
        if self.vbe_enabled and _HAS_VBE:
            try:
                self.vbe = VirtualBassEnhancer(
                    fs=self._samplerate, channels=self._channels,
                    gain_db=cfg.vbe.gain_db,
                )
            except Exception:
                self.vbe = None
        if self.limiter_enabled and _HAS_LIMITER:
            try:
                self.limiter = SoftLimiter(
                    fs=self._samplerate,
                    threshold_db=cfg.limiter.threshold_db,
                    release_ms=cfg.limiter.release_ms,
                )
            except Exception:
                self.limiter = None
        if self.exciter_enabled and _HAS_EXCITER:
            try:
                self.exciter = Exciter(
                    fs=self._samplerate, channels=self._channels,
                    freq_hz=cfg.exciter.freq_hz, mix=cfg.exciter.mix,
                )
            except Exception:
                self.exciter = None
        if self.widener_enabled and _HAS_WIDENER:
            try:
                self.widener = StereoWidener(
                    fs=self._samplerate, channels=self._channels,
                    width=cfg.widener.width,
                    hp_freq_hz=cfg.widener.hp_freq_hz,
                    room_mix=cfg.widener.room_mix,
                )
            except Exception:
                self.widener = None

    def apply_dsp_config(self, dsp_config, eq_bands=None, q_values=None):
        """热重载入口：替换 DSP 配置、重建模块、按新 chain 生效并重新分析电平"""
        self.dsp_config = dsp_config
        self.loudness_enabled = bool(dsp_config.loudness.enabled)
        self.vbe_enabled = bool(dsp_config.vbe.enabled)
        self.limiter_enabled = bool(dsp_config.limiter.enabled)
        self.exciter_enabled = bool(dsp_config.exciter.enabled)
        self.widener_enabled = bool(dsp_config.widener.enabled)
        self.loudness_target_lufs = dsp_config.loudness.target_lufs
        if eq_bands is not None and self.equalizer is not None:
            self.equalizer.set_bands(eq_bands, q_values=q_values)
            self._eq_bands = list(eq_bands)
            self._eq_q = list(q_values) if q_values else self._eq_q
        self._init_dsp_modules()
        # 电平匹配依赖效果链构成，重新分析当前曲目
        self._start_loudness_analysis(self._path, headers=self._headers)

    # ---------- 响度/电平匹配分析 ----------
    def _start_loudness_analysis(self, path: str, headers=None):
        """后台线程分析整曲响度；generation 防止旧结果覆盖新歌/新配置"""
        # 响度补偿关闭且无任何效果模块时，无需测量
        if not (self.loudness_enabled and _HAS_LOUDNESS):
            if not self._any_fx_enabled():
                self._loudness_gain_linear = 1.0
                self.loudness_gain_db = 0.0
                return
            if not _HAS_LOUDNESS:
                return
        self._loudness_gain_linear = 1.0
        self.loudness_gain_db = 0.0
        self._current_gain = 1.0
        self._loudness_generation += 1
        # 取消仍在运行的旧分析，避免快速切歌时堆积分析线程与 ffmpeg 进程
        if self._loudness_cancel is not None:
            self._loudness_cancel.set()
        cancel = threading.Event()
        self._loudness_cancel = cancel
        gen = self._loudness_generation
        self._loudness_task = threading.Thread(
            target=self._analyze_loudness,
            args=(path, self._samplerate, self._channels, gen, cancel),
            kwargs={"headers": headers},
            daemon=True,
        )
        self._loudness_task.start()

    def _any_fx_enabled(self) -> bool:
        """chain 中除 loudness 外是否有启用的效果模块（决定电平匹配分析必要性）"""
        chain = self.dsp_config.chain if self.dsp_config else []
        for name in chain:
            if name == "eq" and self.equalizer and self.equalizer.enabled:
                return True
            if name == "vbe" and self.vbe and self.vbe.enabled:
                return True
            if name == "exciter" and self.exciter and self.exciter.enabled:
                return True
            if name == "widener" and self.widener and self.widener.enabled:
                return True
            if name == "limiter" and self.limiter and self.limiter.enabled:
                return True
        return False

    def _make_offline_fx(self):
        """构建效果链离线副本（供响度分析的湿路测量，不含响度补偿本身）"""
        chain = self.dsp_config.chain if self.dsp_config else []
        mods = []
        for name in chain:
            if name == "eq" and self.equalizer and self.equalizer.enabled:
                mods.append(Equalizer(fs=self._samplerate,
                                      bands_db=list(self._eq_bands or [0.0] * 10),
                                      q_values=self._eq_q,
                                      channels=self._channels))
            elif name == "vbe" and self.vbe and self.vbe.enabled:
                mods.append(VirtualBassEnhancer(
                    fs=self._samplerate, channels=self._channels,
                    gain_db=self.dsp_config.vbe.gain_db))
            elif name == "exciter" and self.exciter and self.exciter.enabled:
                mods.append(Exciter(
                    fs=self._samplerate, channels=self._channels,
                    freq_hz=self.dsp_config.exciter.freq_hz,
                    mix=self.dsp_config.exciter.mix))
            elif name == "widener" and self.widener and self.widener.enabled:
                mods.append(StereoWidener(
                    fs=self._samplerate, channels=self._channels,
                    width=self.dsp_config.widener.width,
                    hp_freq_hz=self.dsp_config.widener.hp_freq_hz,
                    room_mix=self.dsp_config.widener.room_mix))
            elif name == "limiter" and self.limiter and self.limiter.enabled:
                mods.append(SoftLimiter(
                    fs=self._samplerate,
                    threshold_db=self.dsp_config.limiter.threshold_db,
                    release_ms=self.dsp_config.limiter.release_ms))

        if not mods:
            return None

        def fx_process(block):
            for m in mods:
                block = m.process(block)
            return block

        return fx_process

    def _analyze_loudness(self, path: str, fs: int, channels: int, gen: int,
                          cancel_event=None, headers=None):
        """分析完成后仅在 generation 匹配时应用增益。

        - 响度补偿开启：gain = target - 湿响度（对效果链后的电平做补偿）
        - 响度补偿关闭但有效果：gain = 干响度 - 湿响度（开关音效响度不变）
        """
        try:
            analyzer = LoudnessAnalyzer(target_lufs=self.loudness_target_lufs)
            fx_process = self._make_offline_fx() if self._any_fx_enabled() else None
            dry_lufs, wet_lufs = analyzer.measure_dual(
                path, fs, channels, headers=headers,
                cancel_event=cancel_event, fx_process=fx_process)
            if gen != self._loudness_generation:
                return
            neg_inf = float("-inf")
            if self.loudness_enabled:
                base = wet_lufs if wet_lufs != neg_inf else dry_lufs
                if base != neg_inf:
                    gain_db = float(np.clip(
                        self.loudness_target_lufs - base,
                        analyzer.max_cut, analyzer.max_boost))
                    self.loudness_gain_db = gain_db
                    self._loudness_gain_linear = float(10 ** (gain_db / 20.0))
            elif fx_process is not None and dry_lufs != neg_inf and wet_lufs != neg_inf:
                # 电平匹配：效果链导致的响度变化用增益抵消
                gain_db = float(np.clip(dry_lufs - wet_lufs, -6.0, 6.0))
                self.loudness_gain_db = gain_db
                self._loudness_gain_linear = float(10 ** (gain_db / 20.0))
        except Exception:
            pass  # 分析失败保持 0dB，正常播放

    # ---------- 播放控制 ----------
    def play(self):
        if self._file is None:
            return
        if self._stream is not None and self.state == PlayState.PAUSED:
            self.state = PlayState.PLAYING
            self._decode_wake.set()
            return
        self._open_stream()
        self.state = PlayState.PLAYING
        self._decode_wake.set()

    def _open_stream(self):
        if self._stream is not None:
            self._stream.close()

        # 重置播放会话状态
        with self._lock:
            if self._ring is not None:
                self._ring.clear()
            self._decode_done = False
            self._eof_notified = False
            self._seek_target = None
            self._current_gain = self._loudness_gain_linear

        def callback(outdata, frames, time_info, status):
            if self.state != PlayState.PLAYING:
                outdata[:] = 0
                return
            with self._lock:
                if self._ring is not None:
                    data = self._ring.read(frames)
                else:
                    data = np.zeros((0, self._channels), dtype=np.float32)
                n_read = data.shape[0]
                if n_read < frames:
                    pad = np.zeros((frames - n_read, self._channels),
                                   dtype=np.float32)
                    data = np.vstack([data, pad]) if n_read > 0 else pad
                    # 真正的曲尾：解码已完成且缓冲排空
                    if (self._decode_done and self._ring is not None
                            and self._ring.available == 0
                            and not self._eof_notified):
                        self._eof_notified = True
                        self.state = PlayState.STOPPED
                        end_pending = True
                    else:
                        end_pending = False
                        if n_read == 0:
                            self.underruns += 1
                else:
                    end_pending = False
                self._frames_played += n_read
                self._frames_listened += n_read

            # 音量与最终削波保护（固定在链路末端，不属于可配置 chain）
            out = data * self.volume
            np.clip(out, -1.0, 1.0, out=out)
            outdata[:] = out

            mono = out.mean(axis=1) if out.ndim > 1 else out
            self.spectrum.feed(mono.astype(np.float32))

            if end_pending:
                if self.on_track_end:
                    threading.Thread(target=self.on_track_end,
                                     daemon=True).start()
                raise sd.CallbackStop()
            if status is not None and getattr(status, "output_underflow", False):
                self.underruns += 1

        self._stream = sd.OutputStream(
            samplerate=self._samplerate,
            channels=self._channels,
            blocksize=self.block_size,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()
        self._start_decode_thread()

    def _start_decode_thread(self):
        if self._decode_thread is not None and self._decode_thread.is_alive():
            # 上一次的线程残留（join 超时）时会话号已递增，旧线程会自行退出，
            # 这里直接另起新线程接管本会话
            pass
        self._decode_session += 1
        session = self._decode_session
        self._decode_running = True
        self._decode_thread = threading.Thread(
            target=self._decode_loop, args=(session,), daemon=True,
            name="dsp-decode")
        self._decode_thread.start()

    def _decode_loop(self, session: int):
        """解码线程：读 ffmpeg → DSP 链 → 环形缓冲。

        所有阻塞操作（含在线流的网络读取）都发生在这里，远离音频回调。
        会话号不匹配说明本线程已被新会话取代，立即退出避免双写环形缓冲。
        """
        while self._decode_running and session == self._decode_session:
            # seek 优先处理（暂停态也响应）
            target = None
            with self._lock:
                if self._seek_target is not None:
                    target = self._seek_target
                    self._seek_target = None
            if target is not None:
                try:
                    self._file.seek(target)
                except Exception:
                    continue
                with self._lock:
                    self._frames_played = target
                    if self._ring is not None:
                        self._ring.clear()
                    self._decode_done = False
                    self._eof_notified = False
                self._reset_fx_states()
                continue

            if self.state != PlayState.PLAYING:
                self._decode_wake.wait(timeout=0.05)
                self._decode_wake.clear()
                continue

            with self._lock:
                free = self._ring.free if self._ring else 0
            if free < self._DECODE_CHUNK:
                # 缓冲将满，等待回调消费
                self._decode_wake.wait(timeout=0.02)
                self._decode_wake.clear()
                continue

            try:
                data = self._file.read(self._DECODE_CHUNK, dtype="float32",
                                       always_2d=True)
            except Exception:
                with self._lock:
                    self._decode_done = True
                continue
            if data.shape[0] == 0:
                with self._lock:
                    self._decode_done = True
                self._decode_wake.wait(timeout=0.05)
                self._decode_wake.clear()
                continue

            # DSP 链（顺序由 dsp.chain 决定，每级独立开关）
            data = self._process_chain(data)
            with self._lock:
                if self._ring is not None:
                    self._ring.write(data)

    def _process_chain(self, data: np.ndarray) -> np.ndarray:
        """按 dsp.chain 顺序执行启用的 DSP 模块"""
        chain = self.dsp_config.chain if self.dsp_config else []
        for name in chain:
            if name == "eq":
                if self.equalizer and self.equalizer.enabled:
                    data = self.equalizer.process(data)
            elif name == "vbe":
                if self.vbe and self.vbe.enabled:
                    data = self.vbe.process(data)
            elif name == "exciter":
                if self.exciter and self.exciter.enabled:
                    data = self.exciter.process(data)
            elif name == "widener":
                if self.widener and self.widener.enabled:
                    data = self.widener.process(data)
            elif name == "loudness":
                data = self._apply_loudness_gain(data)
            elif name == "limiter":
                if self.limiter and self.limiter.enabled:
                    data = self.limiter.process(data)
        return data

    def _apply_loudness_gain(self, data: np.ndarray) -> np.ndarray:
        """响度/电平匹配增益，块内线性斜坡过渡，避免音量跳变爆音"""
        target = self._loudness_gain_linear
        current = self._current_gain
        if target == current:
            if target != 1.0:
                data = data * target
            return data
        # 块内从当前增益线性过渡到目标增益（一个解码块 ≈ 93ms）
        n = data.shape[0]
        ramp = np.linspace(current, target, n, dtype=np.float32)
        data = data * ramp[:, np.newaxis]
        self._current_gain = target
        return data

    def _reset_fx_states(self):
        """seek 后清空所有 DSP 模块的流式状态，并复位增益斜坡"""
        if self.equalizer:
            self.equalizer.reset()
        if self.vbe:
            self.vbe.reset()
        if self.exciter:
            self.exciter.reset()
        if self.widener:
            self.widener.reset()
        if self.limiter:
            self.limiter.reset()
        self._current_gain = self._loudness_gain_linear

    def pause(self):
        if self.state == PlayState.PLAYING:
            self.state = PlayState.PAUSED

    def resume(self):
        if self.state == PlayState.PAUSED:
            self.state = PlayState.PLAYING
            self._decode_wake.set()

    def toggle_pause(self):
        if self.state == PlayState.PLAYING:
            self.pause()
        elif self.state == PlayState.PAUSED:
            self.resume()

    def stop(self):
        self.state = PlayState.STOPPED
        # 先停解码线程，再关闭文件（避免线程读到已关闭的管道）
        self._decode_running = False
        self._decode_wake.set()
        if self._decode_thread is not None:
            self._decode_thread.join(timeout=2.0)
            self._decode_thread = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        # 复位进度，避免加载失败后残留上一首的 position 被统计误读
        self._frames_played = 0
        self._frames_listened = 0
        self._seek_target = None
        with self._lock:
            if self._ring is not None:
                self._ring.clear()
        # 取消仍在后台运行的响度分析（切歌/退出时不残留 ffmpeg 子进程）
        if self._loudness_cancel is not None:
            self._loudness_cancel.set()

    def seek(self, seconds: float):
        if self._file is None:
            return
        target_frame = int(max(0, min(seconds, self.duration_sec)) * self._samplerate)
        # seek 由解码线程执行（网络流 seek 的阻塞不再影响音频回调）
        with self._lock:
            self._seek_target = target_frame
        self._decode_wake.set()

    def seek_relative(self, delta_seconds: float):
        self.seek(self.position_sec + delta_seconds)

    def set_volume(self, vol: float):
        self.volume = float(np.clip(vol, 0.0, 1.5))

    def volume_relative(self, delta: float):
        self.set_volume(self.volume + delta)

    # ---------- DSP 控制接口 ----------
    def set_eq_bands(self, bands_db, q_values=None):
        if self.equalizer:
            self.equalizer.set_bands(bands_db, q_values=q_values)

    def toggle_eq(self):
        if self.equalizer:
            self.equalizer.enabled = not self.equalizer.enabled
            self._start_loudness_analysis(self._path, headers=self._headers)
            return self.equalizer.enabled
        return False

    def toggle_loudness(self):
        self.loudness_enabled = not self.loudness_enabled
        self._start_loudness_analysis(self._path, headers=self._headers)
        return self.loudness_enabled

    def set_loudness_target(self, lufs: float):
        self.loudness_target_lufs = float(lufs)
        self._start_loudness_analysis(self._path, headers=self._headers)

    def toggle_vbe(self):
        self.vbe_enabled = not self.vbe_enabled
        if self.vbe_enabled and self.vbe is None and _HAS_VBE:
            self._init_dsp_modules()
        if self.vbe is not None:
            self.vbe.enabled = self.vbe_enabled
        self._start_loudness_analysis(self._path, headers=self._headers)
        return self.vbe_enabled

    def set_vbe_gain(self, db: float):
        if self.vbe:
            self.vbe.gain = float(10 ** (db / 20.0))

    def toggle_exciter(self):
        self.exciter_enabled = not self.exciter_enabled
        if self.exciter_enabled and self.exciter is None and _HAS_EXCITER:
            self._init_dsp_modules()
        if self.exciter is not None:
            self.exciter.enabled = self.exciter_enabled
        self._start_loudness_analysis(self._path, headers=self._headers)
        return self.exciter_enabled

    def toggle_widener(self):
        self.widener_enabled = not self.widener_enabled
        if self.widener_enabled and self.widener is None and _HAS_WIDENER:
            self._init_dsp_modules()
        if self.widener is not None:
            self.widener.enabled = self.widener_enabled
        self._start_loudness_analysis(self._path, headers=self._headers)
        return self.widener_enabled

    def toggle_limiter(self):
        self.limiter_enabled = not self.limiter_enabled
        if self.limiter_enabled and self.limiter is None and _HAS_LIMITER:
            self._init_dsp_modules()
        if self.limiter is not None:
            self.limiter.enabled = self.limiter_enabled
        self._start_loudness_analysis(self._path, headers=self._headers)
        return self.limiter_enabled
