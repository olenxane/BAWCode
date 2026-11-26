"""轻量、静默的本地使用统计计数器"""
import json
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional


class UsageStatistics:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else Path(__file__).resolve().parent.parent / "data" / "stat.json"
        self._lock = threading.RLock()
        self._data = self._load()
        self._tool_events_since_save = 0

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == 1:
                data.setdefault("daily_starts", {})
                data.setdefault("dialogs", {})
                data.setdefault("models", {})
                data.setdefault("total_tokens", 0)
                tools = data.setdefault("tools", {})
                if not isinstance(tools, dict):
                    tools = data["tools"] = {}
                tools.setdefault("calls", 0)
                tools.setdefault("failures", 0)
                tools.setdefault("duration_seconds", 0.0)
                tools.setdefault("outcomes", {})
                tools.setdefault("by_name", {})
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {
            "version": 1, "total_tokens": 0, "daily_starts": {}, "dialogs": {}, "models": {},
            "tools": {"calls": 0, "failures": 0, "duration_seconds": 0.0, "outcomes": {}, "by_name": {}},
        }

    def _save(self) -> None:
        temp: Optional[Path] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            temp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            # 统计不得影响主程序；临时文件可能未创建，只清残留的
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    def record_start(self, day: Optional[str] = None) -> None:
        with self._lock:
            key = day or date.today().isoformat()
            starts = self._data["daily_starts"]
            starts[key] = int(starts.get(key, 0)) + 1
            self._save()

    def flush(self) -> None:
        """立即写出累计数据；record_tool 每累计 32 次也会自动落盘一次"""
        with self._lock:
            self._save()
            self._tool_events_since_save = 0

    def record_tool(self, name: str, elapsed: float = 0.0, outcome: str = "ok") -> None:
        """累计工具执行结果；outcome 为 ok 之外的值时计为失败"""
        name = str(name or "unknown")
        outcome = str(outcome or "ok")
        try:
            seconds = max(0.0, float(elapsed or 0.0))
        except (TypeError, ValueError):
            seconds = 0.0
        with self._lock:
            tools = self._data.setdefault("tools", {})
            for key, default in (
                ("calls", 0), ("failures", 0), ("duration_seconds", 0.0),
                ("outcomes", {}), ("by_name", {}),
            ):
                if key not in tools or not isinstance(tools.get(key), type(default)):
                    tools[key] = default
            by_name = tools["by_name"]
            entry = by_name.setdefault(name, {
                "calls": 0, "failures": 0, "duration_seconds": 0.0, "outcomes": {},
            })
            for key, default in (
                ("calls", 0), ("failures", 0), ("duration_seconds", 0.0), ("outcomes", {}),
            ):
                if key not in entry or not isinstance(entry.get(key), type(default)):
                    entry[key] = default
            for item in (tools, entry):
                item["calls"] = int(item.get("calls", 0)) + 1
                item["failures"] = int(item.get("failures", 0)) + int(outcome != "ok")
                item["duration_seconds"] = round(float(item.get("duration_seconds", 0.0)) + seconds, 3)
                outcomes = item["outcomes"]
                outcomes[outcome] = int(outcomes.get(outcome, 0)) + 1
            self._tool_events_since_save += 1
            if self._tool_events_since_save >= 32:
                self._save()
                self._tool_events_since_save = 0

    def record_tool_event(self, payload: dict) -> None:
        """接收 after_tool 观察事件，只记录执行链明确提供的结果类型"""
        self.record_tool(str(payload.get("name") or ""), payload.get("elapsed", 0.0), payload.get("outcome", "ok"))

    def begin_turn(self, session_id: str, model: str, now: Optional[datetime] = None) -> tuple:
        now = now or datetime.now()
        key = str(session_id or "unknown")
        with self._lock:
            dialogs = self._data["dialogs"]
            dialog = dialogs.setdefault(key, {
                "started_at": now.isoformat(timespec="seconds"),
                "last_active_at": "",
                "duration_seconds": 0.0,
                "work_seconds": 0.0,
                "peak_context_tokens": 0,
                "total_tokens": 0,
                "models": [],
            })
            model = str(model or "")
            if model and model not in dialog["models"]:
                dialog["models"].append(model)
            return (key, now.timestamp())

    def end_turn(self, handle: tuple, model: str, token_delta: int, context_tokens: int,
                 now: Optional[datetime] = None) -> None:
        if not handle:
            return
        now = now or datetime.now()
        key, started = handle
        elapsed = max(0.0, now.timestamp() - started)
        tokens = max(0, int(token_delta or 0))
        model = str(model or "")
        with self._lock:
            dialog = self._data["dialogs"].get(key)
            if dialog is None:
                return
            dialog["work_seconds"] = round(float(dialog.get("work_seconds", 0)) + elapsed, 2)
            started_at = datetime.fromisoformat(dialog["started_at"])
            dialog["duration_seconds"] = round(max(0.0, (now - started_at).total_seconds()), 2)
            dialog["last_active_at"] = now.isoformat(timespec="seconds")
            dialog["total_tokens"] = int(dialog.get("total_tokens", 0)) + tokens
            dialog["peak_context_tokens"] = max(int(dialog.get("peak_context_tokens", 0)), max(0, int(context_tokens or 0)))
            if model:
                if model not in dialog["models"]:
                    dialog["models"].append(model)
                entry = self._data["models"].setdefault(model, {"tokens": 0, "turns": 0})
                entry["tokens"] = int(entry.get("tokens", 0)) + tokens
                entry["turns"] = int(entry.get("turns", 0)) + 1
            self._data["total_tokens"] = int(self._data.get("total_tokens", 0)) + tokens
            self._save()


def initialize(path: Optional[Path] = None) -> UsageStatistics:
    stats = UsageStatistics(path)
    stats.record_start()
    return stats
