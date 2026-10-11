"""使用 mutagen 读取歌曲基础信息（标题/艺人/专辑/时长）"""
from __future__ import annotations
import os
from dataclasses import dataclass

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None


@dataclass
class TrackInfo:
    path: str
    title: str
    artist: str
    album: str
    duration_sec: float
    filetype: str


def _fix_mojibake(s: str) -> str:
    """修复被按 latin-1 解码的 GBK 标签乱码（ID3v1 无编码声明、
    ID3v2 编码字节声明错误时常见，如 "ÖÚÉúÆæÔµ" → "众生奇缘"）。

    仅当满足全部条件才替换，避免误伤真正的拉丁字母文本：
    1. 可无损映射回 latin-1 字节（UTF-8 标签的正常中文含非 Latin-1
       字符，直接排除）；
    2. 存在连续两个高位字节——GB2312 汉字的 GBK 编码两字节均 ≥0x80，
       中文乱码必有相邻高位对，而 "Café"、"Mötley Crüe" 之类真实
       拉丁文本的重音字符是孤立的；
    3. 字节能按 gb18030 严格解码，且还原结果包含汉字。"""
    if not s:
        return s
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return s
    if not any(c >= "\x80" for c in s):
        return s
    has_high_pair = any(raw[i] >= 0x80 and raw[i + 1] >= 0x80
                        for i in range(len(raw) - 1))
    if not has_high_pair:
        return s
    try:
        decoded = raw.decode("gb18030")
    except UnicodeDecodeError:
        return s
    if not any("\u4e00" <= c <= "\u9fff" for c in decoded):
        return s
    return decoded


def _first(tags, keys, default=""):
    if tags is None:
        return default
    for k in keys:
        if k in tags:
            v = tags[k]
            if isinstance(v, list) and v:
                return _fix_mojibake(str(v[0]))
            if v:
                return _fix_mojibake(str(v))
    return default


def read_metadata(path: str) -> TrackInfo:
    filename = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    title, artist, album, duration = filename, "神秘艺术家", "神秘专辑", 0.0

    if MutagenFile is not None:
        try:
            audio = MutagenFile(path, easy=True)
            if audio is not None:
                tags = audio.tags
                title = _first(tags, ["title"], filename) or filename
                artist = _first(tags, ["artist"], "神秘艺术家") or "神秘艺术家"
                album = _first(tags, ["album"], "神秘专辑") or "神秘专辑"
                if audio.info is not None:
                    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
        except Exception:
            pass

    return TrackInfo(path=path, title=title, artist=artist, album=album,
                      duration_sec=duration, filetype=ext)


def write_track_tags(path: str, title: str = "", artist: str = "", album: str = "",
                     cover_bytes: bytes | None = None,
                     lyrics_text: str | None = None) -> None:
    """把标题/艺人/专辑/封面/歌词写入音频文件标签（支持 mp3/m4a/flac）。

    任一字段为空则跳过；格式不支持或 mutagen 缺失时抛异常，由调用方降级处理。
    """
    if MutagenFile is None:
        raise RuntimeError("未安装 mutagen，无法写入标签")
    audio = MutagenFile(path)
    if audio is None or not hasattr(audio, "save"):
        raise ValueError(f"mutagen 无法解析该文件: {path}")
    ext = os.path.splitext(path)[1].lower()

    if ext == ".mp3":
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, USLT
        tags = audio.tags
        if not isinstance(tags, ID3):
            tags = ID3()
        if title:
            tags.add(TIT2(encoding=3, text=[title]))
        if artist:
            tags.add(TPE1(encoding=3, text=[artist]))
        if album:
            tags.add(TALB(encoding=3, text=[album]))
        if lyrics_text:
            tags.add(USLT(encoding=3, lang="chi", desc="", text=lyrics_text))
        if cover_bytes:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                          desc="Cover", data=cover_bytes))
        audio.tags = tags
        audio.save()
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4Cover
        if title:
            audio["\xa9nam"] = [title]
        if artist:
            audio["\xa9ART"] = [artist]
        if album:
            audio["\xa9alb"] = [album]
        if lyrics_text:
            audio["\xa9lyr"] = [lyrics_text]
        if cover_bytes:
            audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    elif ext == ".flac":
        if title:
            audio["title"] = [title]
        if artist:
            audio["artist"] = [artist]
        if album:
            audio["album"] = [album]
        if lyrics_text:
            audio["LYRICS"] = [lyrics_text]
        if cover_bytes:
            from mutagen.flac import Picture
            pic = Picture()
            pic.data = cover_bytes
            pic.mime = "image/jpeg"
            pic.type = 3  # front cover
            pic.desc = "Cover"
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
    else:
        raise ValueError(f"不支持的标签格式: {ext}")
