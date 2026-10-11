"""播放统计：静默记录每日听歌时长与播放明细"""
from __future__ import annotations
import json
import os
from datetime import date, datetime


class StatsTracker:
    """静默运行的统计数据收集器。

    数据按天粒度存储，结构（_version: 2）：
        {"_version": 2, "days": {"2026-09-05": {
            "seconds": 3600.0,
            "songs": [{"t": 歌名, "a": 歌手, "ts": "HH:MM", "p": "local|qq|wy|bili"}]
        }}}
    songs 每次播放追加一条，不去重；seconds 仍按整曲时长累计。
    v1（纯歌名列表）加载时自动迁移：a 填"神秘艺术家"，ts 置 None，p 填 "local"。
    """

    FLUSH_INTERVAL = 600.0  # 秒，定时刷写间隔

    def __init__(self, data_path: str):
        self._data_path = data_path
        self._data: dict = {}
        self._dirty = False
        self._load()

    # ---------- 内部 ----------
    def _load(self):
        if os.path.exists(self._data_path):
            try:
                with open(self._data_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                if "days" not in self._data:
                    self._data = {}
            except (json.JSONDecodeError, OSError):
                self._data = {}
        if "days" not in self._data:
            self._data = {"_version": 2, "days": {}}
        elif self._data.get("_version", 1) != 2:
            self._migrate_v1()

    def _migrate_v1(self):
        """v1 → v2：纯歌名包装为明细对象，未知字段填默认值"""
        for day in self._data.get("days", {}).values():
            day["songs"] = [
                s if isinstance(s, dict) else
                {"t": s, "a": "神秘艺术家", "ts": None, "p": "local"}
                for s in day.get("songs", [])
            ]
        self._data["_version"] = 2
        self._save()

    def _save(self):
        try:
            tmp = self._data_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._data_path)
        except OSError:
            pass

    def _get_today(self) -> dict:
        key = date.today().isoformat()
        days = self._data.setdefault("days", {})
        if key not in days:
            days[key] = {"seconds": 0, "songs": []}
        return days[key]

    # ---------- 对外接口 ----------
    def record_song(self, title: str, artist: str | None = None,
                    platform: str | None = None):
        """记录一条播放明细（追加，不去重），仅标脏，延迟至定时/退出时批量写盘"""
        today = self._get_today()
        today["songs"].append({
            "t": title,
            "a": artist,
            "ts": datetime.now().strftime("%H:%M"),
            "p": platform,
        })
        self._dirty = True

    def add_seconds(self, seconds: float):
        """累加当天听歌秒数（仅内存，不写盘）"""
        if seconds <= 0:
            return
        today = self._get_today()
        today["seconds"] += seconds
        self._dirty = True

    def flush(self):
        """将内存数据写入磁盘"""
        if self._dirty:
            self._save()
            self._dirty = False

    def get_today(self) -> dict:
        return self._get_today()
