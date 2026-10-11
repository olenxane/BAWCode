"""LRC 歌词解析与匹配。

匹配策略（优先级从高到低）：
 1. 歌词库精确匹配：与歌曲同名的 .lrc 文件在 lyrics_dir 中
 2. 歌词库模糊匹配：lyrics_dir 中文件名最相似的 .lrc
 3. 歌曲目录精确匹配：与歌曲同名的 .lrc 在歌曲同目录
 4. 歌曲目录模糊匹配：歌曲同目录中文件名最相似的 .lrc
"""
from __future__ import annotations
import os
import re
import difflib
from dataclasses import dataclass
from typing import Optional


LRC_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass
class LyricLine:
    time_ms: int
    text: str


@dataclass
class LyricsData:
    lines: list
    source_path: Optional[str]
    match_type: str  # "exact" / "fuzzy" / "none"
    similarity: float = 1.0

    def find_index(self, position_ms: int) -> int:
        if not self.lines:
            return -1
        lo, hi = 0, len(self.lines) - 1
        ans = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.lines[mid].time_ms <= position_ms:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans


def format_lrc_time(time_ms: float) -> str:
    """毫秒 → LRC 时间戳 [mm:ss.xx]（歌词文件导出用）"""
    if time_ms < 0:
        time_ms = 0
    total_cs = int(round(time_ms / 10.0))  # 厘秒
    cs = total_cs % 100
    m, s = divmod(total_cs // 100, 60)
    return f"{m:02d}:{s:02d}.{cs:02d}"


def _parse_lrc_text(text: str) -> list:
    lines = []
    for raw_line in text.splitlines():
        matches = list(LRC_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        content = LRC_TIME_RE.sub("", raw_line).strip()
        for m in matches:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3) or "0"
            frac = frac.ljust(3, "0")[:3]
            millis = int(frac) if len(frac) == 3 else int(frac) * (10 ** (3 - len(frac)))
            time_ms = minutes * 60_000 + seconds * 1000 + millis
            lines.append(LyricLine(time_ms=time_ms, text=content))
    lines.sort(key=lambda l: l.time_ms)
    return lines


def _read_lrc_file(path: str) -> list:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read()
            return _parse_lrc_text(text)
        except (UnicodeDecodeError, LookupError):
            continue
    return []


def _try_exact_match(directory: str, base_name: str) -> Optional[str]:
    """在指定目录中精确匹配同名 .lrc 文件"""
    exact_path = os.path.join(directory, base_name + ".lrc")
    if os.path.isfile(exact_path):
        return exact_path
    return None


def _try_fuzzy_match(directory: str, base_name: str,
                     threshold: float) -> tuple:
    """在指定目录中模糊匹配最相似的 .lrc 文件，返回 (path, ratio) 或 (None, 0)"""
    norm_base = _normalize_name(base_name)
    best_path, best_ratio = None, 0.0
    try:
        entries = os.listdir(directory)
    except OSError:
        return None, 0.0
    for fname in entries:
        if not fname.lower().endswith(".lrc"):
            continue
        candidate = os.path.splitext(fname)[0]
        ratio = difflib.SequenceMatcher(
            None, norm_base, _normalize_name(candidate)
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_path = os.path.join(directory, fname)
    if best_path and best_ratio >= threshold:
        return best_path, best_ratio
    return None, 0.0


def load_lyrics(song_path: str, lyrics_dir: str = "", fuzzy_match: bool = True,
                 fuzzy_threshold: float = 0.75) -> LyricsData:
    base_name = os.path.splitext(os.path.basename(song_path))[0]
    song_dir = os.path.dirname(os.path.abspath(song_path))

    # 候选目录列表，歌词库优先
    search_dirs = []
    if lyrics_dir:
        ld = os.path.abspath(lyrics_dir)
        if os.path.isdir(ld):
            search_dirs.append(ld)
    search_dirs.append(song_dir)

    # 逐目录执行双匹配（精确 → 模糊），第一个命中即返回
    for d in search_dirs:
        # 精确匹配
        exact_path = _try_exact_match(d, base_name)
        if exact_path:
            lines = _read_lrc_file(exact_path)
            if lines:
                return LyricsData(lines=lines, source_path=exact_path,
                                   match_type="exact", similarity=1.0)

        # 模糊匹配
        if not fuzzy_match:
            continue
        fuzzy_path, ratio = _try_fuzzy_match(d, base_name, fuzzy_threshold)
        if fuzzy_path:
            lines = _read_lrc_file(fuzzy_path)
            if lines:
                return LyricsData(lines=lines, source_path=fuzzy_path,
                                   match_type="fuzzy", similarity=ratio)

    return LyricsData(lines=[], source_path=None, match_type="none")


def lyrics_from_lines(lines: list) -> LyricsData:
    """从 [(time_ms, text), ...] 或 [LyricLine, ...] 列表构建 LyricsData"""
    parsed = []
    for item in lines:
        if isinstance(item, LyricLine):
            parsed.append(item)
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            parsed.append(LyricLine(time_ms=int(item[0]), text=str(item[1])))
    parsed.sort(key=lambda l: l.time_ms)
    return LyricsData(lines=parsed, source_path=None, match_type="online")


def _normalize_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[\(\[（【][^)\]）】]*[\)\]）】]", "", name)
    name = re.sub(r"[-_·—]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name
