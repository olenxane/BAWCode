"""FFmpeg 子进程解码器：为所有格式提供流式 PCM 解码。
优先使用系统 PATH 中的 ffmpeg，若不存在则尝试 imageio-ffmpeg 自带的预编译二进制，
使 Windows 用户无需手动安装 ffmpeg。
"""
from __future__ import annotations
import json
import re
import shutil
import subprocess
import sys
from typing import Optional

import numpy as np

# Windows 下隐藏子进程控制台窗口
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _find_ffmpeg_binary() -> Optional[str]:
    """返回可用的 ffmpeg 可执行文件路径：优先 PATH，其次 imageio-ffmpeg"""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _find_ffprobe_binary() -> Optional[str]:
    """ffprobe 不随 imageio-ffmpeg 捆绑，仅在系统 PATH 中查找"""
    return shutil.which("ffprobe")


_FFMPEG_BIN = None
_FFPROBE_BIN = None


def _build_header_args(headers: Optional[dict]) -> list:
    """把请求头 dict 转为 ffmpeg `-headers` 输入选项参数（非 HTTP 源返回空）。"""
    if not headers:
        return []
    lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    if not lines:
        return []
    return ["-headers", lines]


def ffmpeg_available() -> bool:
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        _FFMPEG_BIN = _find_ffmpeg_binary()
    return _FFMPEG_BIN is not None


def ffprobe_available() -> bool:
    global _FFPROBE_BIN
    if _FFPROBE_BIN is None:
        _FFPROBE_BIN = _find_ffprobe_binary()
    return _FFPROBE_BIN is not None


def _get_ffmpeg_bin() -> str:
    if not ffmpeg_available():
        raise RuntimeError(
            "未找到 ffmpeg。请安装 ffmpeg 后重试，或运行 pip install imageio-ffmpeg"
        )
    return _FFMPEG_BIN


class FFmpegAudioFile:
    """通过 ffmpeg 子进程流式解码音频，模拟 soundfile.SoundFile 接口。

    seek 通过重启 ffmpeg -ss 实现。适用于 sounddevice 回调式播放。
    仅支持 float32 输出（与 Player 的回调一致）。

    headers: 可选 HTTP 请求头 dict（如 B站直链的 Referer/UA），
    以 ffmpeg -headers 输入选项传入（对本地路径无影响）。
    """

    def __init__(self, path: str, headers: Optional[dict] = None):
        if not ffmpeg_available():
            raise RuntimeError(
                "未找到 ffmpeg。请安装 ffmpeg，或运行 pip install imageio-ffmpeg"
            )
        self._path = path
        self._proc: Optional[subprocess.Popen] = None
        self._start_offset_sec = 0.0
        self._header_args = _build_header_args(headers)

        self.samplerate: int = 0
        self.channels: int = 0
        self._duration_sec: float = 0.0

        self._probe(path)
        if self.samplerate == 0 or self.channels == 0:
            raise RuntimeError(f"无法解析音频参数: {path}")
        self._start_pipe(0.0)

    # ---- 元数据探测 ----
    def _probe(self, path: str):
        """通过 ffprobe（优先）或 ffmpeg stderr（兜底）获取采样率/声道/时长"""
        if ffprobe_available():
            self._probe_with_ffprobe(path)
        if self.samplerate == 0:
            self._probe_with_ffmpeg(path)

    def _probe_with_ffprobe(self, path: str):
        cmd = [
            _FFPROBE_BIN, "-v", "quiet",
            *self._header_args,
            "-print_format", "json",
            "-show_streams", "-show_format", path,
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, encoding="utf-8", errors="replace",
                timeout=10, creationflags=_CREATE_NO_WINDOW,
            )
            info = json.loads(r.stdout)
            streams = info.get("streams", [])
            audio_stream = next(
                (s for s in streams if s.get("codec_type") == "audio"),
                streams[0] if streams else {},
            )
            self.samplerate = int(audio_stream.get("sample_rate", 0))
            self.channels = int(audio_stream.get("channels", 0))
            self._duration_sec = float(
                info.get("format", {}).get("duration", 0.0) or 0.0
            )
        except Exception:
            pass

    def _probe_with_ffmpeg(self, path: str):
        cmd = [_get_ffmpeg_bin(), *self._header_args, "-i", path, "-f", "null", "-"]
        try:
            r = subprocess.run(
                cmd, capture_output=True, encoding="utf-8", errors="replace",
                timeout=10, creationflags=_CREATE_NO_WINDOW,
            )
            stderr = r.stderr
            m = re.search(r"(\d+)\s*Hz", stderr)
            if m:
                self.samplerate = int(m.group(1))
            m = re.search(r"(\d+)\s*channels?", stderr, re.IGNORECASE)
            if m:
                self.channels = int(m.group(1))
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
            if m:
                h, mi, s = m.groups()
                self._duration_sec = int(h) * 3600 + int(mi) * 60 + float(s)
        except Exception:
            pass

    # ---- 管道管理 ----
    def _start_pipe(self, start_sec: float):
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass

        self._start_offset_sec = start_sec
        cmd = [
            _get_ffmpeg_bin(),
            *self._header_args,
            "-ss", f"{start_sec}",
            "-i", self._path,
            "-f", "f32le", "-acodec", "pcm_f32le",
            "-ac", str(self.channels),
            "-ar", str(self.samplerate),
            "-vn", "-",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )

    # ---- 兼容 soundfile.SoundFile 接口 ----
    def read(self, frames: int, dtype: str = "float32", always_2d: bool = True):
        """读取指定帧数的 float32 PCM 数据。dtype 参数仅为接口兼容，始终返回 float32。"""
        if self._proc is None or self._proc.stdout is None:
            return np.zeros((0, self.channels if always_2d else 1), dtype=np.float32)

        bytes_needed = frames * self.channels * 4
        raw = self._proc.stdout.read(bytes_needed)
        data = np.frombuffer(raw, dtype=np.float32)

        actual = len(data) // self.channels
        data = data[:actual * self.channels]
        if always_2d:
            data = data.reshape(-1, self.channels)
        elif self.channels == 1:
            data = data.reshape(-1)
        return data

    def seek(self, frame: int):
        """通过重启 ffmpeg 并指定 -ss 偏移来实现 seek"""
        self._start_pipe(frame / self.samplerate)

    def __len__(self) -> int:
        return int(self._duration_sec * self.samplerate)

    def close(self):
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
            self._proc = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
