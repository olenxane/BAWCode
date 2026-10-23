"""music-player 插件：把终端音乐播放器移植为主窗口底部插件。

底部每帧显示歌曲/进度/歌词/频谱，用 /music 子命令代替原来的键盘操作。
播放器核心是 tmplayer 包，从 terminal-music-player 的 mp 包复制而来，仅保留
本地播放链路（解码/DSP/歌词/频谱/统计），在线音乐部分不移植。
"""

from __future__ import annotations

from bawcode_sdk import PluginContext

import os
import shutil
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent

from rich.console import Console

from .tmplayer import ui as tm_ui
from .tmplayer.config import ConfigError, load_config
from .tmplayer.lyrics import load_lyrics
from .tmplayer.metadata import read_metadata
from .tmplayer.player import Player, PlayState
from .tmplayer.playlist import Playlist
from .tmplayer.spectrum import SpectrumAnalyzer
from .tmplayer.stats import StatsTracker

_HELP = (
    "音乐播放器指令：\n"
    "  /music play [关键词|序号] · 播放（不带参数=继续/恢复当前）\n"
    "  /music pause | toggle | stop\n"
    "  /music next | prev · 切歌（含队列与历史）\n"
    "  /music list [关键词] · 列出曲库\n"
    "  /music add <关键词|序号> · 加入播放队列\n"
    "  /music queue | clearqueue · 队列查看/清空\n"
    "  /music vol [0-150] · 音量\n"
    "  /music seek <±秒|mm:ss> · 定位\n"
    "  /music mode [seq|shuffle|one|all] · 播放模式\n"
    "  /music spectrum · 频谱开关\n"
    "  /music theme [名称] · 配色主题\n"
    "  /music lyric · 当前歌词上下文\n"
    "  /music dir [list|add <目录>|rm <序号>] · 音乐目录\n"
    "  /music rescan | reload | close | open | status | help"
)

_MODE_ALIAS = {
    "seq": "sequential", "sequential": "sequential", "顺序": "sequential",
    "shuffle": "shuffle", "随机": "shuffle",
    "one": "repeat_one", "repeat_one": "repeat_one", "单曲": "repeat_one",
    "all": "repeat_all", "repeat_all": "repeat_all", "列表": "repeat_all",
}
_MODE_LABEL = {"sequential": "顺序", "shuffle": "随机", "repeat_one": "单曲", "repeat_all": "列表"}


def _fmt_sec(sec: float) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


class MusicController:
    def __init__(self, ctx):
        self.ctx = ctx
        self.log = ctx.log
        self.data_dir = Path(ctx.storage_dir())
        self.cfg_path = self.data_dir / "config.yaml"
        if not self.cfg_path.exists():
            shutil.copyfile(_HERE / "config.yaml", self.cfg_path)

        self._lock = threading.RLock()
        self._visible = True
        self._status = ""
        self._status_until = 0.0
        self._console = None
        self._console_width = 0
        self._search_cache: list = []

        self.cfg = load_config(str(self.cfg_path))
        self._theme_keys = self.cfg.theme_names()
        self._spectrum_on = bool(self.cfg.spectrum.enabled)

        self.playlist = Playlist(mode=self.cfg.playback.playlist_mode)
        self.playlist.load_dirs(self.cfg.music_dirs)

        spec = self.cfg.spectrum
        self.spectrum = SpectrumAnalyzer(
            fs=44100, bars=spec.bars, fft_size=spec.fft_size,
            smoothing=spec.smoothing, min_db=spec.min_db, max_db=spec.max_db,
        )
        self.player = Player(
            spectrum=self.spectrum,
            volume=self.cfg.playback.default_volume,
            on_track_end=self._on_track_end,
            dsp_config=self.cfg.dsp,
        )
        self.current_track = None
        self.lyrics_data = None
        self.stats = StatsTracker(str(self.data_dir / "stats.json"))
        self._stats_flush = 0.0

        if self.playlist.tracks:
            self._load_current_track()
        self.log.info("音乐播放器就绪：曲库 %d 首", len(self.playlist.tracks))

    # ---------------- 播放核心 ----------------
    def _current_index(self):
        if self.playlist.tracks and 0 <= self.playlist.index < len(self.playlist.tracks):
            return self.playlist.index
        return None

    def _record_stats_if_played(self):
        if self.current_track is None or self.player.listened_sec < 30:
            return
        self.stats.record_song(self.current_track.title,
                               artist=self.current_track.artist, platform="local")
        if self.current_track.duration_sec > 0:
            self.stats.add_seconds(self.current_track.duration_sec)

    def _load_current_track(self):
        self._record_stats_if_played()
        path = self.playlist.current
        if not path:
            return
        self.current_track = read_metadata(path)
        self.lyrics_data = load_lyrics(
            path,
            lyrics_dir=self.cfg.lyrics.lyrics_dir,
            fuzzy_match=self.cfg.lyrics.fuzzy_match,
            fuzzy_threshold=self.cfg.lyrics.fuzzy_threshold,
        )
        self.player.load(path, eq_bands=self.cfg.equalizer.bands_db,
                         q_values=self.cfg.equalizer.q_values)

    def _leave_current_to_history(self):
        idx = self._current_index()
        if idx is not None:
            self.playlist.push_buffer(idx)

    def _on_track_end(self):
        # 音频线程回调：队列优先，其次按模式顺延
        with self._lock:
            if self.playlist.has_queue():
                self._leave_current_to_history()
                self.playlist.next_from_queue()
                self._load_current_track()
                self.player.play()
                return
            if not self.playlist.tracks:
                return
            if self.playlist.mode == "repeat_one":
                self._load_current_track()
                self.player.play()
                return
            if (self.playlist.mode == "sequential"
                    and self.playlist.index == len(self.playlist.tracks) - 1):
                return
            self.playlist.next()
            self._load_current_track()
            self.player.play()

    def _jump_and_play(self, index: int):
        self.playlist.jump_to(index)
        self._load_current_track()
        self.player.play()

    def _match_tracks(self, query: str):
        """按关键词匹配曲目（空格分隔，全部命中，顺序无关），返回索引列表"""
        keys = [k for k in query.lower().split() if k]
        out = []
        for i, path in enumerate(self.playlist.tracks):
            name = os.path.basename(path).lower()
            if all(k in name for k in keys):
                out.append(i)
        return out

    def _resolve_target(self, token: str):
        """把「序号」或「关键词」解析为曲目索引；找不到返回 None"""
        token = token.strip()
        if not token:
            return None
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(self.playlist.tracks):
                return idx
            return None
        matches = self._match_tracks(token)
        return matches[0] if matches else None

    # ---------------- 指令处理 ----------------
    def handle(self, args: str) -> str:
        parts = (args or "").strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        with self._lock:
            return self._handle(sub, rest)

    def _handle(self, sub: str, rest: str) -> str:
        if sub in ("help", "?", "-h"):
            return _HELP
        if sub == "play":
            return self._cmd_play(rest)
        if sub in ("pause", "p"):
            self.player.pause()
            return self._say("已暂停")
        if sub in ("toggle", "resume"):
            self._cmd_play("")
            return ""
        if sub == "next":
            return self._cmd_next()
        if sub == "prev":
            return self._cmd_prev()
        if sub == "stop":
            self.player.stop()
            self.current_track = None
            self.lyrics_data = None
            return self._say("已停止")
        if sub == "status":
            return self._status_text()
        if sub == "list":
            return self._cmd_list(rest)
        if sub == "search":
            return self._cmd_list(rest)
        if sub == "add":
            return self._cmd_add(rest)
        if sub in ("queue", "q"):
            return self._cmd_queue()
        if sub in ("clearqueue", "clear"):
            self.playlist.clear_queue()
            return self._say("已清空播放队列")
        if sub in ("vol", "volume"):
            return self._cmd_vol(rest)
        if sub == "seek":
            return self._cmd_seek(rest)
        if sub == "mode":
            return self._cmd_mode(rest)
        if sub in ("spectrum", "spec"):
            self._spectrum_on = not self._spectrum_on
            self._persist()
            return self._say(f"频谱已{'开启' if self._spectrum_on else '关闭'}")
        if sub == "theme":
            return self._cmd_theme(rest)
        if sub in ("lyric", "lyrics"):
            return self._cmd_lyric()
        if sub in ("dir", "dirs"):
            return self._cmd_dir(rest)
        if sub == "rescan":
            self.playlist.load_dirs(self.cfg.music_dirs)
            self._search_cache = []
            if self.playlist.tracks:
                self._load_current_track()
            else:
                self.player.stop()
                self.current_track = None
                self.lyrics_data = None
            return self._say(f"已重扫：曲库 {len(self.playlist.tracks)} 首")
        if sub == "reload":
            return self._cmd_reload()
        if sub in ("close", "hide"):
            self.player.stop()
            self._visible = False
            return self._say("已关闭底部显示（/music open 恢复）")
        if sub in ("open", "show"):
            self._visible = True
            return self._say("已恢复底部显示")
        return f"未知子命令：{sub}\n{_HELP}"

    def _cmd_play(self, rest: str) -> str:
        if rest:
            idx = self._resolve_target(rest)
            if idx is None:
                return f"未找到匹配曲目：{rest}"
            self._jump_and_play(idx)
            return self._say(f"播放：{os.path.basename(self.playlist.tracks[idx])}")
        if not self.playlist.tracks:
            return "曲库为空：用 /music dir add <目录> 添加后 /music rescan"
        if self.player.state == PlayState.PLAYING:
            return self._say("正在播放")
        if self.current_track is None:
            self._load_current_track()
        self.player.play()
        return self._say("继续播放")

    def _cmd_next(self) -> str:
        if not self.playlist.tracks:
            return "曲库为空"
        if self.playlist.has_queue():
            self._leave_current_to_history()
            self.playlist.next_from_queue()
        else:
            self._leave_current_to_history()
            self.playlist.next()
        self._load_current_track()
        self.player.play()
        return self._say(f"下一首：{os.path.basename(self.playlist.current or '')}")

    def _cmd_prev(self) -> str:
        if not self.playlist.tracks:
            return "曲库为空"
        if self.playlist.has_buffer():
            idx = self._current_index()
            if idx is not None:
                self.playlist.push_queue_front(idx)
            self.playlist.prev_from_buffer()
        else:
            self.playlist.prev()
        self._load_current_track()
        self.player.play()
        return self._say(f"上一首：{os.path.basename(self.playlist.current or '')}")

    def _cmd_list(self, rest: str) -> str:
        if not self.playlist.tracks:
            return "曲库为空：用 /music dir add <目录> 添加后 /music rescan"
        if rest:
            self._search_cache = self._match_tracks(rest)
            idxs = self._search_cache
        else:
            idxs = list(range(len(self.playlist.tracks)))
        if not idxs:
            return f"无匹配曲目：{rest}"
        lines = [f"曲库 {len(self.playlist.tracks)} 首"
                 + (f" · 匹配 {len(idxs)} 首：{rest}" if rest else "")]
        cur = self.playlist.index
        for n, i in enumerate(idxs[:30], 1):
            mark = "▶ " if i == cur and self.playlist.tracks else "  "
            tags = ""
            if self.playlist.is_in_queue(i):
                tags += " [队列]"
            lines.append(f"{mark}{n:>3}. {os.path.basename(self.playlist.tracks[i])}{tags}")
        if len(idxs) > 30:
            lines.append(f"  … 还有 {len(idxs) - 30} 首")
        return "\n".join(lines)

    def _cmd_add(self, rest: str) -> str:
        if not rest:
            return "用法：/music add <关键词|序号>"
        idx = self._resolve_target(rest)
        if idx is None:
            return f"未找到匹配曲目：{rest}"
        self.playlist.add_to_queue(idx)
        return self._say(f"已加入队列（{self.playlist.queue_len()}）：{os.path.basename(self.playlist.tracks[idx])}")

    def _cmd_queue(self) -> str:
        n = self.playlist.queue_len()
        if n == 0:
            return "播放队列为空（/music add <关键词|序号> 添加）"
        lines = [f"播放队列 {n} 首："]
        for k in range(n):
            item = self.playlist._queue[k]
            if isinstance(item, int) and 0 <= item < len(self.playlist.tracks):
                lines.append(f"  {k + 1}. {os.path.basename(self.playlist.tracks[item])}")
        return "\n".join(lines)

    def _cmd_vol(self, rest: str) -> str:
        if not rest:
            return f"当前音量 {int(round(self.player.volume * 100))}%"
        raw = rest.rstrip("%").strip()
        if not raw.isdigit():
            return "用法：/music vol <0-150>"
        self.player.set_volume(int(raw) / 100.0)
        self._persist()
        return self._say(f"音量 {int(round(self.player.volume * 100))}%")

    def _cmd_seek(self, rest: str) -> str:
        if not rest:
            return "用法：/music seek <±秒|mm:ss>"
        try:
            if rest.startswith(("+", "-")):
                self.player.seek_relative(float(rest))
            elif ":" in rest:
                m, s = rest.split(":", 1)
                self.player.seek(int(m) * 60 + float(s))
            else:
                self.player.seek(float(rest))
        except ValueError:
            return "无法解析定位位置，用 ±秒 或 mm:ss"
        return self._say(f"已定位到 {_fmt_sec(self.player.position_sec)}")

    def _cmd_mode(self, rest: str) -> str:
        if not rest:
            return f"当前模式：{_MODE_LABEL.get(self.playlist.mode, self.playlist.mode)}"
        key = _MODE_ALIAS.get(rest.strip().lower())
        if key is None:
            return "用法：/music mode [seq|shuffle|one|all]"
        self.playlist.set_mode(key)
        self._persist()
        return self._say(f"播放模式：{_MODE_LABEL.get(key, key)}")

    def _cmd_theme(self, rest: str) -> str:
        if not self._theme_keys:
            return "配置未定义主题"
        if rest:
            name = rest.strip()
            if name.isdigit():
                i = int(name) - 1
                if not 0 <= i < len(self._theme_keys):
                    return f"主题序号超范围（1-{len(self._theme_keys)}）"
                key = self._theme_keys[i]
            elif name in self.cfg.all_themes:
                key = name
            else:
                return f"未知主题：{name}（可用：{', '.join(self._theme_keys)}）"
        else:
            cur = self.cfg.theme.key
            i = self._theme_keys.index(cur) if cur in self._theme_keys else -1
            key = self._theme_keys[(i + 1) % len(self._theme_keys)]
        self.cfg.theme = self.cfg.all_themes[key]
        self._persist()
        return self._say(f"主题：{self.cfg.theme.name}（{key}）")

    def _cmd_lyric(self) -> str:
        if self.current_track is None:
            return "当前无曲目"
        if not self.lyrics_data or not self.lyrics_data.lines:
            return "当前曲目无歌词"
        pos_ms = int(self.player.position_sec * 1000) + self.cfg.lyrics.offset_ms
        idx = self.lyrics_data.find_index(pos_ms)
        if idx < 0:
            return "歌词尚未开始"
        ctx_n = max(0, int(self.cfg.lyrics.context_lines))
        lo = max(0, idx - ctx_n)
        hi = min(len(self.lyrics_data.lines), idx + ctx_n + 1)
        out = []
        for i in range(lo, hi):
            line = self.lyrics_data.lines[i]
            mark = "▶ " if i == idx else "  "
            out.append(f"{mark}{_fmt_sec(line.time_ms / 1000)} {line.text}")
        return "\n".join(out)

    def _cmd_dir(self, rest: str) -> str:
        raw = self.cfg.raw
        dirs = list(raw.get("music_dirs") or [])
        parts = rest.split(maxsplit=1)
        action = parts[0].lower() if parts else "list"
        value = parts[1].strip() if len(parts) > 1 else ""
        if action == "list":
            if not dirs:
                return "未配置音乐目录"
            return "音乐目录：\n" + "\n".join(f"  {i + 1}. {d}" for i, d in enumerate(dirs))
        if action == "add":
            if not value:
                return "用法：/music dir add <目录>"
            path = os.path.expanduser(value)
            if not os.path.exists(path):
                return f"目录不存在：{path}"
            if path in dirs:
                return "该目录已在列表中"
            dirs.append(path)
            raw["music_dirs"] = dirs
            self.cfg.music_dirs = dirs
            self._persist()
            self.playlist.load_dirs(dirs)
            self._search_cache = []
            return self._say(f"已添加：{path}（曲库 {len(self.playlist.tracks)} 首）")
        if action in ("rm", "remove", "del"):
            if not value.isdigit():
                return "用法：/music dir rm <序号>"
            i = int(value) - 1
            if not 0 <= i < len(dirs):
                return f"序号超范围（1-{len(dirs)}）"
            removed = dirs.pop(i)
            raw["music_dirs"] = dirs
            self.cfg.music_dirs = dirs
            self._persist()
            self.playlist.load_dirs(dirs)
            self._search_cache = []
            return self._say(f"已移除：{removed}（曲库 {len(self.playlist.tracks)} 首）")
        return "用法：/music dir [list|add <目录>|rm <序号>]"

    def _cmd_reload(self) -> str:
        try:
            new_cfg = load_config(str(self.cfg_path))
        except ConfigError as e:
            return f"配置加载失败：{e}"
        keep_theme = self.cfg.theme.key
        self.cfg = new_cfg
        if keep_theme in self.cfg.all_themes:
            self.cfg.theme = self.cfg.all_themes[keep_theme]
        self._theme_keys = self.cfg.theme_names()
        self.spectrum.smoothing = self.cfg.spectrum.smoothing
        self.spectrum.min_db = self.cfg.spectrum.min_db
        self.spectrum.max_db = self.cfg.spectrum.max_db
        self._spectrum_on = bool(self.cfg.spectrum.enabled)
        if self.player.equalizer:
            self.player.set_eq_bands(self.cfg.equalizer.bands_db,
                                     q_values=self.cfg.equalizer.q_values)
        self.player.apply_dsp_config(
            self.cfg.dsp,
            eq_bands=self.cfg.equalizer.bands_db,
            q_values=self.cfg.equalizer.q_values,
        )
        return self._say("已重载 config.yaml")

    def _status_text(self) -> str:
        if not self.playlist.tracks:
            return "曲库为空：用 /music dir add <目录> 添加后 /music rescan"
        state = {"playing": "播放中", "paused": "已暂停", "stopped": "已停止"}.get(
            self.player.state.value, self.player.state.value)
        name = os.path.basename(self.playlist.current or "")
        track = self.current_track.title if self.current_track else name
        return (f"{state} · {track} · {_fmt_sec(self.player.position_sec)}/"
                f"{_fmt_sec(max(self.player.duration_sec, (self.current_track.duration_sec if self.current_track else 0)))}"
                f" · 音量 {int(round(self.player.volume * 100))}%"
                f" · 模式 {_MODE_LABEL.get(self.playlist.mode, self.playlist.mode)}"
                f" · 队列 {self.playlist.queue_len()}")

    def _say(self, text: str) -> str:
        self._status = text
        self._status_until = time.monotonic() + 4.0
        return text

    # ---------------- 持久化 ----------------
    def _persist(self):
        try:
            import yaml
        except ImportError:
            return
        raw = self.cfg.raw
        pb = raw.setdefault("playback", {})
        pb["default_volume"] = round(float(self.player.volume), 3)
        pb["playlist_mode"] = self.playlist.mode
        raw["theme"] = self.cfg.theme.key
        raw.setdefault("spectrum", {})["enabled"] = bool(self._spectrum_on)
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False,
                               default_flow_style=False)
        except OSError as e:
            self.log.warn("写入配置失败: %s", e)

    # ---------------- 底部渲染 ----------------
    def render_rows(self, payload=None):
        if not self._visible:
            return []
        width = int((payload or {}).get("width") or 80)
        width = max(30, width)
        with self._lock:
            try:
                return self._render_locked(width)
            except Exception as e:
                self.log.warn("底部渲染失败: %s", e)
                return []

    def _render_locked(self, width: int):
        if not self.playlist.tracks:
            return [" 音乐播放器 · 未发现曲目。用 /music dir add <目录> 添加，/music rescan 重扫"]
        if self.player.state == PlayState.PLAYING:
            levels = self.spectrum.compute()
        else:
            levels = self.spectrum.silence_decay()
        status = self._status if time.monotonic() < self._status_until else ""
        group = tm_ui.build_compact_ui(
            self.cfg, self.current_track, self.player, levels, self.lyrics_data,
            self.playlist, self._spectrum_on, width=width, status=status,
        )
        console = self._console
        if console is None or self._console_width != width:
            # 渲染宽度留余量：行宽按 cell_len 恰好等于终端宽，而 rich 对 emoji
            # 的宽度度量可能略有出入，紧贴宽度会在临界处被判定换行而拆成两行
            console = Console(width=width + 16, force_terminal=True,
                              color_system="truecolor", highlight=False)
            self._console = console
            self._console_width = width
        with console.capture() as cap:
            console.print(group, no_wrap=True)
        rows = cap.get().split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        return rows

    # ---------------- 指令补全 ----------------
    def completer(self, config, arg: str):
        subs = [
            ("play", "播放 / 按关键词或序号点播"),
            ("pause", "暂停"), ("toggle", "播放/暂停"), ("stop", "停止"),
            ("next", "下一首"), ("prev", "上一首"),
            ("list", "列出曲库"), ("add", "加入队列"), ("queue", "查看队列"),
            ("clearqueue", "清空队列"),
            ("vol", "音量 0-150"), ("seek", "定位 ±秒|mm:ss"),
            ("mode", "播放模式 seq|shuffle|one|all"),
            ("spectrum", "频谱开关"), ("theme", "配色主题"), ("lyric", "歌词上下文"),
            ("dir", "音乐目录 list|add|rm"),
            ("rescan", "重扫目录"), ("reload", "重载配置"),
            ("close", "关闭底部显示"), ("open", "恢复底部显示"),
            ("status", "播放状态"), ("help", "帮助"),
        ]
        token = (arg or "").split()[-1].lower() if (arg or "").strip() else ""
        items = [
            {"name": f"/music {s}", "hint": h, "source": "arg", "callable": True}
            for s, h in subs if not token or s.startswith(token)
        ]
        return items

    # ---------------- 生命周期 ----------------
    def shutdown(self):
        try:
            with self._lock:
                self._record_stats_if_played()
                self.stats.flush()
        except Exception:
            pass
        try:
            self.player.stop()
        except Exception:
            pass
        self.log.info("音乐播放器已停止")


def setup(ctx: PluginContext):
    try:
        ctl = MusicController(ctx)
    except Exception as e:
        ctx.log.warn("music-player 初始化失败: %s", e)
        return

    def _cmd(cctx, args):
        try:
            msg = ctl.handle(args)
        except Exception as e:
            ctl.log.warn("命令执行失败: %s", e)
            msg = f"音乐播放器出错：{e}"
        if msg:
            ctx.notify(msg)
        return None

    ctx.register_command(
        "/music",
        hint="音乐播放器 · 播放/暂停/切歌/音量/频谱",
        usage="/music [play|pause|next|prev|list|add|vol|seek|mode|theme|dir|help]",
        handler=_cmd,
        completer=ctl.completer,
    )
    ctx.register_ui_bottom(ctl.render_rows)
    ctx.register_teardown(ctl.shutdown)
    ctx.log.info("music-player 已装载")
