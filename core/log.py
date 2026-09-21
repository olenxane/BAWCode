#分级日志模块：debug/info/warn/error 四级，写入 data/log/ 按天分文件
#粒度由 config.json 的 "log" 段控制：level 全局阈值，modules 按模块覆盖
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _ROOT / "data" / "config.json"
_DEFAULT_LOG_DIR = _ROOT / "data" / "log"
_FILE_PREFIX = "bawcode"

LEVEL_NAMES = ("debug", "info", "warn", "error", "off")
_LEVEL_VALUE = {"debug": 10, "info": 20, "warn": 30, "error": 40, "off": 100}

# 单行最大长度：超长截断，避免整包上下文打爆日志
_MAX_LINE = 4000

# 全局状态：init() 前为默认值（Config 加载后会以配置文件覆盖）
_level_value = _LEVEL_VALUE["info"]
_module_levels: dict = {}
_log_dir = _DEFAULT_LOG_DIR
_console = False
_days_to_keep = 14

_lock = threading.Lock()
_fh = None
_fh_day = ""

_loggers: dict = {}


def _normalize_level(value) -> Optional[int]:
    """'debug'/'info'/'warn'/'error'/'off' → 数值；非法返回 None（保持原值）"""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _LEVEL_VALUE:
            return _LEVEL_VALUE[text]
    return None


def _clip(message: str) -> str:
    """压平换行并截断超长内容，保持日志单行可 grep"""
    text = str(message).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    if len(text) > _MAX_LINE:
        text = text[:_MAX_LINE] + f"…(截断，共{len(text)}字符)"
    return text


def init(cfg: Optional[dict], log_dir: Optional[Path] = None) -> None:
    """应用日志配置（config.json 的 log 段）；可多次调用，仅更新给出的键

    cfg 结构：
      level: 全局阈值 debug/info/warn/error/off（默认 info）
      modules: 按模块覆盖，如 {"llm": "debug", "ui": "off"}
      console: 是否同步输出到 stderr（默认 false）
      days_to_keep: 日志保留天数，0 为永久（默认 14）
    """
    global _level_value, _module_levels, _log_dir, _console, _days_to_keep
    cfg = cfg or {}
    value = _normalize_level(cfg.get("level"))
    if value is not None:
        _level_value = value
    modules = cfg.get("modules")
    if isinstance(modules, dict):
        cleaned = {}
        for name, lv in modules.items():
            target = _normalize_level(lv)
            if target is not None and str(name).strip():
                cleaned[str(name).strip().lower()] = target
        _module_levels = cleaned
    if log_dir is not None:
        _log_dir = Path(log_dir)
    if "console" in cfg:
        _console = bool(cfg.get("console"))
    try:
        _days_to_keep = int(cfg.get("days_to_keep", _days_to_keep))
    except (TypeError, ValueError):
        pass
    _cleanup()


def _cleanup() -> None:
    """删除超过保留天数的旧日志文件（按修改时间）"""
    if _days_to_keep <= 0:
        return
    try:
        cutoff = time.time() - _days_to_keep * 86400
        for path in _log_dir.glob(f"{_FILE_PREFIX}-*.log"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
    except OSError:
        pass


def _write(level_name: str, module: str, message: str) -> None:
    global _fh, _fh_day
    now = datetime.now()
    stamp = f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d}"
    line = f"{stamp} [{level_name.upper():<5}] [{module}] {message}\n"
    with _lock:
        if _console:
            try:
                sys.stderr.write(line)
                sys.stderr.flush()
            except Exception:
                pass
        try:
            day = f"{now:%Y%m%d}"
            if _fh is None or _fh_day != day:
                if _fh is not None:
                    try:
                        _fh.close()
                    except Exception:
                        pass
                    _fh = None
                _log_dir.mkdir(parents=True, exist_ok=True)
                _fh = (_log_dir / f"{_FILE_PREFIX}-{day}.log").open("a", encoding="utf-8")
                _fh_day = day
            _fh.write(line)
            _fh.flush()
        except Exception:
            # 日志写入失败静默，不影响主程序
            pass


class Logger:
    """模块级日志器：debug/info/warn/error，支持 %-style 惰性格式化"""

    def __init__(self, name: str):
        self.name = name

    def enabled(self, level_name: str) -> bool:
        """该模块当前是否输出指定级别（用于昂贵的调试信息按需构造）"""
        limit = _module_levels.get(self.name, _level_value)
        return _LEVEL_VALUE[level_name] >= limit

    def _log(self, level_name: str, message: str, *args) -> None:
        if not self.enabled(level_name):
            return
        try:
            text = message % args if args else message
        except Exception:
            text = f"{message} (日志格式化失败: {args!r})"
        _write(level_name, self.name, _clip(text))

    def debug(self, message: str, *args) -> None:
        self._log("debug", message, *args)

    def info(self, message: str, *args) -> None:
        self._log("info", message, *args)

    def warn(self, message: str, *args) -> None:
        self._log("warn", message, *args)

    warning = warn

    def error(self, message: str, *args) -> None:
        self._log("error", message, *args)


def get_logger(name: str) -> Logger:
    """按模块名获取日志器；同名复用同一实例"""
    logger = _loggers.get(name)
    if logger is None:
        logger = Logger(name)
        _loggers[name] = logger
    return logger


def log_path() -> Path:
    """当前日志文件路径（未写入过时为今天对应文件）"""
    return _log_dir / f"{_FILE_PREFIX}-{datetime.now():%Y%m%d}.log"


def bootstrap() -> None:
    """Config 加载前先读 data/config.json 的 log 段（失败保持默认）"""
    try:
        raw = _DEFAULT_CONFIG_PATH.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict) and isinstance(data.get("log"), dict):
            init(data["log"])
    except Exception:
        pass


bootstrap()
