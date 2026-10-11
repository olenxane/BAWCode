"""配置文件加载与解析"""
from __future__ import annotations
import os
import copy
from dataclasses import dataclass, field
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")

BAND_FREQS = ["31Hz", "62Hz", "125Hz", "250Hz", "500Hz", "1kHz",
              "2kHz", "4kHz", "8kHz", "16kHz"]
BAND_CENTER_HZ = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
DEFAULT_Q_VALUES = [1.0, 1.0, 1.0, 1.0, 2.5, 1.0, 1.0, 1.0, 4.0, 1.0]

# 30 段 1/3 倍频程（ISO 中心频率，20Hz~16kHz），peaking EQ 对应 Q≈4.32
BAND_FREQS_30 = ["20Hz", "25Hz", "31.5Hz", "40Hz", "50Hz", "63Hz", "80Hz",
                 "100Hz", "125Hz", "160Hz", "200Hz", "250Hz", "315Hz", "400Hz",
                 "500Hz", "630Hz", "800Hz", "1kHz", "1.25kHz", "1.6kHz",
                 "2kHz", "2.5kHz", "3.15kHz", "4kHz", "5kHz", "6.3kHz",
                 "8kHz", "10kHz", "12.5kHz", "16kHz"]
BAND_CENTER_HZ_30 = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250,
                     315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500,
                     3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000]
DEFAULT_Q_VALUES_30 = [4.32] * 30


def band_table(band_count: int):
    """按段数返回 (频段标签, 中心频率, 默认Q) 三元组；未知段数回退 10 段"""
    if band_count == 30:
        return BAND_FREQS_30, BAND_CENTER_HZ_30, DEFAULT_Q_VALUES_30
    return BAND_FREQS, BAND_CENTER_HZ, DEFAULT_Q_VALUES


def interp_bands(vals, src_freqs, dst_freqs):
    """把频段增益按对数频率线性插值到新频段表（预设跨段数复用用）"""
    import math
    if len(vals) != len(src_freqs):
        return [0.0] * len(dst_freqs)
    out = []
    for f in dst_freqs:
        lf = math.log10(max(f, 1e-1))
        if lf <= math.log10(src_freqs[0]):
            out.append(float(vals[0]))
            continue
        if lf >= math.log10(src_freqs[-1]):
            out.append(float(vals[-1]))
            continue
        for i in range(len(src_freqs) - 1):
            f0, f1 = src_freqs[i], src_freqs[i + 1]
            l0, l1 = math.log10(f0), math.log10(f1)
            if l0 <= lf <= l1:
                t = (lf - l0) / (l1 - l0) if l1 > l0 else 0.0
                out.append(float(vals[i] + t * (vals[i + 1] - vals[i])))
                break
    return out

class ConfigError(Exception):
    pass


def _minimal_yaml_load(text: str) -> dict:
    """极简 YAML 兜底解析器（仅当 PyYAML 不可用时使用）。
    """
    raise ConfigError(
        "未安装 PyYAML，无法解析配置文件。请先运行: pip install pyyaml"
    )


def load_raw_config(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if yaml is not None:
        data = yaml.safe_load(text)
    else:
        data = _minimal_yaml_load(text)
    if not isinstance(data, dict):
        raise ConfigError("配置文件格式错误：根节点必须是一个映射(dict)")
    return data


@dataclass
class ThemeColors:
    key: str
    name: str
    primary: str
    secondary: str
    accent: str
    text: str
    dim: str
    bg_bar: str
    spectrum_gradient: list
    lyric_current: str
    lyric_context: str


@dataclass
class EqualizerConfig:
    enabled: bool
    bands_db: list  # 频段增益, 单位 dB, 顺序与 band_table(band_count) 一致
    q_values: list  # 每个频段的 Q 值
    preset: str = "custom"
    band_count: int = 10  # 10 段或 30 段（1/3 倍频程）


@dataclass
class LyricsConfig:
    fuzzy_match: bool = True
    fuzzy_threshold: float = 0.55
    offset_ms: int = 0
    context_lines: int = 2
    lyrics_dir: str = ""


@dataclass
class SpectrumConfig:
    bars: int = 48
    fft_size: int = 2048
    smoothing: float = 0.6
    min_db: float = -60
    max_db: float = 0
    style: str = "blocks"
    height: int = 4  # 频谱行数，1~5
    height_scale: float = 1.0  # 高度缩放因子，<1 变矮 >1 变高
    zhanwei_char: str = "▁"  # 未填充格位的占位字符，默认 ▁
    enabled: bool = True  # 是否默认显示频谱


@dataclass
class MarqueeConfig:
    enabled: bool = True          # 标题超宽时是否开启截断滚动（关闭则静态截断加 …）
    hold_secs: float = 5.0        # 每轮滚动前截断态停留秒数
    step_interval: float = 0.3    # 每滚动一列的秒数
    gap_cols: int = 4             # 两轮滚动之间的空隙列数


@dataclass
class PlaybackConfig:
    default_volume: float = 0.8
    fade_ms: int = 150
    playlist_mode: str = "sequential"
    ui_fps: int = 20
    ui_width: int = 50
    marquee: MarqueeConfig = field(default_factory=MarqueeConfig)


@dataclass
class LoudnessConfig:
    enabled: bool = False
    target_lufs: float = -16.0


@dataclass
class VbeConfig:
    enabled: bool = False
    gain_db: float = -3.0


@dataclass
class LimiterConfig:
    enabled: bool = False
    threshold_db: float = -1.0
    release_ms: float = 50.0


@dataclass
class ExciterConfig:
    """谐波激励器：对高频段做软饱和，增加人声/高频光泽"""
    enabled: bool = True
    freq_hz: float = 3500.0   # 激励起始频率
    mix: float = 0.15         # 谐波混合比例 0~0.5


@dataclass
class WidenerConfig:
    """声场展宽器：M/S 立体声扩展，低频自动保持居中；可选房间混响"""
    enabled: bool = True
    width: float = 1.3        # 展宽系数 1.0(原始)~2.0
    hp_freq_hz: float = 250.0  # 侧通道高通截止，低于此频率保持单声道
    room_mix: float = 0.0     # 房间混响湿量 0~1，默认关闭


# DSP 链默认顺序：整形类在前，电平管理类在后
DEFAULT_DSP_CHAIN = ["eq", "vbe", "exciter", "widener", "loudness", "limiter"]
# 合法模块名集合（chain 校验用）
DSP_CHAIN_MODULES = set(DEFAULT_DSP_CHAIN)


@dataclass
class DspConfig:
    loudness: LoudnessConfig = field(default_factory=LoudnessConfig)
    vbe: VbeConfig = field(default_factory=VbeConfig)
    limiter: LimiterConfig = field(default_factory=LimiterConfig)
    exciter: ExciterConfig = field(default_factory=ExciterConfig)
    widener: WidenerConfig = field(default_factory=WidenerConfig)
    chain: list = field(default_factory=lambda: list(DEFAULT_DSP_CHAIN))
    chain_invalid: list = field(default_factory=list)  # chain 中被丢弃的非法项，供上层告警


@dataclass
class OnlineMusicConfig:
    qq_quality: int = 320          # 128 或 320
    wy_quality: str = "exhigh"     # standard/higher/exhigh/lossless/hires
    bi_quality: int = 320          # B站音频音质上限：320=尽力最高(实际192K)/192/132/64
    download_dir: str = "download_musics"  # 在线歌曲下载目录；相对路径以程序目录为基准，空 = 当前工作目录


def _norm_bind_key(value, default: str) -> str:
    """归一化按键绑定：space→空格，tab→\\t，其余取小写单字符"""
    v = str(value or "").strip().lower()
    if not v:
        return default
    if v == "space":
        return " "
    if v == "tab":
        return "\t"
    return v[0]


@dataclass
class KeyBindingsConfig:
    quit: str = "q"
    selector: str = "\t"
    online: str = "o"
    play_pause: str = " "
    next: str = "n"
    prev: str = "p"
    spectrum: str = "s"
    theme: str = "t"
    mode: str = "m"
    reload: str = "r"
    download: str = "d"
    dir_setup: str = "u"
    exciter: str = "e"   # 切换谐波激励器
    widener: str = "w"   # 切换声场展宽器


def _load_keybindings(raw: dict) -> KeyBindingsConfig:
    kb_raw = raw.get("keybindings", {}) or {}
    defaults = KeyBindingsConfig()
    kb = KeyBindingsConfig()
    for field_name in vars(defaults):
        setattr(kb, field_name,
                _norm_bind_key(kb_raw.get(field_name),
                               getattr(defaults, field_name)))
    return kb


@dataclass
class AppConfig:
    music_dirs: list
    equalizer: EqualizerConfig
    lyrics: LyricsConfig
    spectrum: SpectrumConfig
    playback: PlaybackConfig
    dsp: DspConfig
    theme: ThemeColors
    all_themes: dict
    raw: dict
    path: str
    online_music: OnlineMusicConfig = field(default_factory=OnlineMusicConfig)
    keybindings: KeyBindingsConfig = field(default_factory=KeyBindingsConfig)

    def theme_names(self):
        return list(self.all_themes.keys())


def _build_theme(key: str, raw: dict) -> ThemeColors:
    return ThemeColors(
        key=key,
        name=raw.get("name", key),
        primary=raw.get("primary", "#ffffff"),
        secondary=raw.get("secondary", "#cccccc"),
        accent=raw.get("accent", "#ffcc00"),
        text=raw.get("text", "#e0e0e0"),
        dim=raw.get("dim", "#666666"),
        bg_bar=raw.get("bg_bar", "#202020"),
        spectrum_gradient=raw.get("spectrum_gradient", ["#666666", "#aaaaaa", "#ffffff"]),
        lyric_current=raw.get("lyric_current", "bold #ffffff"),
        lyric_context=raw.get("lyric_context", "#888888"),
    )


def _resolve_eq_bands(eq_raw: dict) -> list:
    band_count = int(eq_raw.get("band_count", 10) or 10)
    freqs, _, _ = band_table(band_count)
    preset = eq_raw.get("preset", "custom")
    presets = eq_raw.get("presets", {})
    if preset != "custom" and preset in presets:
        vals = presets[preset]
        # 预设段数与目标段数不一致时，按对数频率插值迁移
        if len(vals) != band_count:
            src_freqs = BAND_CENTER_HZ if len(vals) == 10 else BAND_CENTER_HZ_30
            vals = interp_bands(vals, src_freqs, freqs)
    else:
        bands_map = eq_raw.get("bands", {})
        vals = [float(bands_map.get(f, 0)) for f in freqs]
    vals = [float(v) for v in vals]
    if len(vals) != band_count:
        raise ConfigError(f"均衡器必须恰好包含 {band_count} 个频段的数值")
    return vals


def _resolve_eq_q_values(eq_raw: dict) -> list:
    band_count = int(eq_raw.get("band_count", 10) or 10)
    freq_labels, _, default_q = band_table(band_count)
    q_map = eq_raw.get("q_values", {})
    vals = [float(q_map.get(f, dq)) for f, dq in zip(freq_labels, default_q)]
    return vals


def _resolve_music_dirs(raw: dict) -> list:
    """兼容新旧两种写法：
    - 新版: music_dirs: [路径1, 路径2, ...]
    - 旧版: music_dir: "单个路径"（字符串）
    返回去重后的路径列表（原始顺序保留，不在此处校验是否存在）"""
    dirs = []
    multi = raw.get("music_dirs")
    if isinstance(multi, list):
        dirs.extend(str(d).strip() for d in multi if str(d).strip())
    single = raw.get("music_dir")
    if isinstance(single, str) and single.strip():
        dirs.append(single.strip())
    # 去重，保留顺序
    seen = set()
    result = []
    for d in dirs:
        key = os.path.abspath(os.path.expanduser(d))
        if key not in seen:
            seen.add(key)
            result.append(d)
    return result


def save_music_dirs(path: str, music_dirs: list) -> None:
    """把音频目录列表写回配置文件，仅更新 music_dirs 字段（保留其余配置和注释无法完全保留，
    因为 yaml.safe_load 不保留注释；此函数用最小侵入方式重写整份配置）。
    """
    if yaml is None:
        raise ConfigError("未安装 PyYAML，无法写入配置文件。请先运行: pip install pyyaml")
    raw = load_raw_config(path) if os.path.exists(path) else {}
    raw["music_dirs"] = list(music_dirs)
    raw.pop("music_dir", None)  # 迁移到新字段，避免新旧同时存在造成混淆
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    raw = load_raw_config(path)

    eq_raw = raw.get("equalizer", {}) or {}
    equalizer = EqualizerConfig(
        enabled=bool(eq_raw.get("enabled", True)),
        bands_db=_resolve_eq_bands(eq_raw),
        q_values=_resolve_eq_q_values(eq_raw),
        preset=eq_raw.get("preset", "custom"),
        band_count=int(eq_raw.get("band_count", 10) or 10),
    )

    lyr_raw = raw.get("lyrics", {}) or {}
    lyrics = LyricsConfig(
        fuzzy_match=bool(lyr_raw.get("fuzzy_match", True)),
        fuzzy_threshold=float(lyr_raw.get("fuzzy_threshold", 0.55)),
        offset_ms=int(lyr_raw.get("offset_ms", 0)),
        context_lines=int(lyr_raw.get("context_lines", 2)),
        lyrics_dir=raw.get("lyrics_dir", "") or "",
    )

    spec_raw = raw.get("spectrum", {}) or {}
    spectrum = SpectrumConfig(
        bars=int(spec_raw.get("bars", 48)),
        fft_size=int(spec_raw.get("fft_size", 2048)),
        smoothing=float(spec_raw.get("smoothing", 0.6)),
        min_db=float(spec_raw.get("min_db", -60)),
        max_db=float(spec_raw.get("max_db", 0)),
        style=spec_raw.get("style", "blocks"),
        height=max(1, min(5, int(spec_raw.get("height", 4)))),
        height_scale=max(0.1, float(spec_raw.get("height_scale", 1.0))),
        zhanwei_char=spec_raw.get("zhanwei_char", "▁") or "▁",
        enabled=bool(spec_raw.get("enabled", True)),
    )

    pb_raw = raw.get("playback", {}) or {}
    mq_raw = pb_raw.get("marquee", {}) or {}
    playback = PlaybackConfig(
        default_volume=float(pb_raw.get("default_volume", 0.8)),
        fade_ms=int(pb_raw.get("fade_ms", 150)),
        playlist_mode=pb_raw.get("playlist_mode", "sequential"),
        ui_fps=int(pb_raw.get("ui_fps", 20)),
        ui_width=max(30, int(pb_raw.get("ui_width", 50))),
        marquee=MarqueeConfig(
            enabled=bool(mq_raw.get("enabled", True)),
            hold_secs=max(0.0, float(mq_raw.get("hold_secs", 5.0))),
            step_interval=max(0.05, float(mq_raw.get("step_interval", 0.3))),
            gap_cols=max(0, int(mq_raw.get("gap_cols", 4))),
        ),
    )

    dsp_raw = raw.get("dsp", {}) or {}
    loud_raw = dsp_raw.get("loudness", {}) or {}
    vbe_raw = dsp_raw.get("vbe", {}) or {}
    lim_raw = dsp_raw.get("limiter", {}) or {}
    exc_raw = dsp_raw.get("exciter", {}) or {}
    wid_raw = dsp_raw.get("widener", {}) or {}

    # chain：有序模块列表；非法/重复项丢弃并记录，空缺用默认顺序
    chain = []
    chain_invalid = []
    for name in (dsp_raw.get("chain") or DEFAULT_DSP_CHAIN):
        name = str(name).strip().lower()
        if name not in DSP_CHAIN_MODULES:
            chain_invalid.append(name)
            continue
        if name not in chain:
            chain.append(name)

    exc_mix = float(exc_raw.get("mix", 0.15))
    wid_width = float(wid_raw.get("width", 1.3))
    dsp = DspConfig(
        loudness=LoudnessConfig(
            enabled=bool(loud_raw.get("enabled", False)),
            target_lufs=float(loud_raw.get("target_lufs", -16.0)),
        ),
        vbe=VbeConfig(
            enabled=bool(vbe_raw.get("enabled", False)),
            gain_db=float(vbe_raw.get("gain_db", -3.0)),
        ),
        limiter=LimiterConfig(
            enabled=bool(lim_raw.get("enabled", False)),
            threshold_db=float(lim_raw.get("threshold_db", -1.0)),
            release_ms=float(lim_raw.get("release_ms", 50.0)),
        ),
        exciter=ExciterConfig(
            enabled=bool(exc_raw.get("enabled", True)),
            freq_hz=float(min(max(exc_raw.get("freq_hz", 3500.0), 1000.0), 8000.0)),
            mix=float(min(max(exc_mix, 0.0), 0.5)),
        ),
        widener=WidenerConfig(
            enabled=bool(wid_raw.get("enabled", True)),
            width=float(min(max(wid_width, 1.0), 2.0)),
            hp_freq_hz=float(min(max(wid_raw.get("hp_freq_hz", 250.0), 80.0), 500.0)),
            room_mix=float(min(max(wid_raw.get("room_mix", 0.0), 0.0), 1.0)),
        ),
        chain=chain or list(DEFAULT_DSP_CHAIN),
        chain_invalid=chain_invalid,
    )

    themes_raw = raw.get("themes", {}) or {}
    all_themes = {k: _build_theme(k, v) for k, v in themes_raw.items()}
    theme_key = raw.get("theme", "neon")
    if theme_key not in all_themes:
        if all_themes:
            theme_key = list(all_themes.keys())[0]
        else:
            raise ConfigError("配置文件中未定义任何主题(themes)")

    music_dirs = _resolve_music_dirs(raw)

    om_raw = raw.get("online_music", {}) or {}
    online_music = OnlineMusicConfig(
        qq_quality=int(om_raw.get("qq_quality", 320)),
        wy_quality=om_raw.get("wy_quality", "exhigh"),
        bi_quality=int(om_raw.get("bi_quality", 320)),
        download_dir=str(om_raw.get("download_dir", "download_musics") or ""),
    )
    keybindings = _load_keybindings(raw)

    return AppConfig(
        music_dirs=music_dirs,
        equalizer=equalizer,
        lyrics=lyrics,
        spectrum=spectrum,
        playback=playback,
        dsp=dsp,
        theme=all_themes[theme_key],
        all_themes=all_themes,
        raw=raw,
        path=path,
        online_music=online_music,
        keybindings=keybindings,
    )


def reload_config(cfg: AppConfig) -> AppConfig:
    """重新从磁盘加载配置（用于运行时热重载均衡器/主题）"""
    return load_config(cfg.path)
