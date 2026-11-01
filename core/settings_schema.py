# -*- coding: utf-8 -*-
"""设置字段声明加载：data/settings_schema.json 驱动「系统」「快捷键」标签页。

TUI（core/settings.py）与远控插件网页设置面板共用同一份声明，字段清单/
类型/选项/缺省值不再分散硬编码于各面板；新增或调整配置项只需编辑声明
文件。带副作用的少数键（mode 走属性 setter、log_level 重初始化日志、
workflow_active / active_model_name / font_size / default_shell）在
special_read / special_write 分发，其余走通用点路径读写。声明文件缺失
或损坏时返回空列表（记录日志），面板显示空页，不影响其它标签页。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List

from core import policy
from core import shells
from core.config import init_logging
from core.log import get_logger

log = get_logger("settings_schema")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "data" / "settings_schema.json"

LOG_LEVELS = ("debug", "info", "warn", "error", "off")

_cached_mtime = None
_cached_tabs: Dict[str, List[dict]] = {}


def _load() -> Dict[str, List[dict]]:
    """读取声明文件（按修改时间缓存）；缺失/损坏返回空结构"""
    global _cached_mtime, _cached_tabs
    try:
        mtime = SCHEMA_PATH.stat().st_mtime
    except OSError:
        # False 哨兵：缺失告警只记一次，文件出现后仍能重新加载
        if _cached_mtime is not False:
            log.warn("设置声明文件不存在，系统/快捷键页退化为空: %s", SCHEMA_PATH)
            _cached_mtime = False
            _cached_tabs = {}
        return _cached_tabs
    if mtime == _cached_mtime:
        return _cached_tabs
    try:
        raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        log.error("设置声明文件解析失败，系统/快捷键页退化为空: %s", e)
        _cached_mtime = mtime
        _cached_tabs = {}
        return _cached_tabs
    tabs = raw if isinstance(raw, dict) else {}
    _cached_mtime = mtime
    _cached_tabs = {
        tab: [f for f in rows if isinstance(f, dict) and f.get("key") and f.get("type")]
        for tab, rows in tabs.items() if isinstance(rows, list)
    }
    return _cached_tabs


def shell_availability_hint() -> str:
    """默认执行器字段的提示：列出本机探测结果，选到不可用的会自动回退 cmd"""
    ready = [name for name in shells.SHELL_NAMES if shells.available(name)]
    missing = [name for name in shells.SHELL_NAMES if name not in ready]
    parts = [f"本机可用: {'/'.join(ready) or '无'}"]
    if missing:
        parts.append(f"不可用: {'/'.join(missing)}（选用后自动回退 {shells.label(shells.CMD)}）")
    return " · ".join(parts)


def dynamic_options(source: str, config) -> List[Any]:
    """@ 开头的动态候选源解析"""
    if source == "@themes":
        return ["dark"] + [t for t in config.list_themes() if t != "dark"]
    if source == "@modes":
        return list(policy.MODES)
    if source == "@shells":
        return list(shells.SHELL_NAMES)
    if source == "@workflows":
        from core import workflow

        names = workflow.list_workflows(config)
        active = workflow.active_name(config)
        if active not in names:
            names.append(active)
        return names or ["default"]
    if source == "@models":
        return config.list_model_names() or [config.model_name]
    log.warn("未知的设置选项动态源: %s", source)
    return []


def fields(tab: str, config) -> List[dict]:
    """某标签页的声明列表：解析动态选项、附加 current 当前值"""
    out = []
    for field in _load().get(tab) or []:
        item = dict(field)
        spec = field.get("options")
        if isinstance(spec, str):
            item["options"] = dynamic_options(spec, config)
            if spec == "@shells":
                item["hint"] = (field.get("hint") or "") + " · " + shell_availability_hint()
        item["current"] = read_field(config, item)
        if item["type"] == "csv" and isinstance(item["current"], list):
            item["current"] = ", ".join(str(x) for x in item["current"])
        out.append(item)
    return out


def read_field(config, field: dict) -> Any:
    """按声明读取当前值：特殊键走专用读取，其余按点路径取，缺失用缺省"""
    reader = special_read.get(field["key"])
    if reader is not None:
        return reader(config)
    node: Any = config.data
    for seg in field.get("path", "").split("."):
        node = node.get(seg) if isinstance(node, dict) else None
        if node is None:
            break
    return field.get("default") if node is None else node


def write_field(config, field: dict, raw) -> bool:
    """按声明写入：特殊键走专用写入，其余类型转换并钳制后写点路径；
    无法转换时保留原值返回 False"""
    writer = special_write.get(field["key"])
    if writer is not None:
        return writer(config, raw)
    value = coerce(field, raw)
    if value is None:
        return False
    node = config.data
    segs = field.get("path", "").split(".")
    for seg in segs[:-1]:
        node = node.setdefault(seg, {})
    node[segs[-1]] = value
    return True


def coerce(field: dict, raw) -> Any:
    """声明类型转换与 min/max 钳制；失败返回 None"""
    ftype = field["type"]
    try:
        if ftype == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("true", "1", "on", "开")
            return bool(raw)
        if ftype in ("int", "float"):
            number = float(raw)
            if not math.isfinite(number):
                return None
            value = int(number) if ftype == "int" else number
        elif ftype == "csv":
            return [p.strip() for p in str(raw).split(",") if p.strip()]
        elif ftype == "choice" and isinstance(field.get("options"), list):
            text = str(raw)
            return text if text in [str(o) for o in field["options"]] else None
        else:  # str / choice / binding
            return str(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if "min" in field:
        value = max(field["min"], value)
    if "max" in field:
        value = min(field["max"], value)
    return value


# ----- 特殊键：读取带归一化或不在 config.data 点路径上 -----

def _read_mode(config):
    return config.mode


def _read_workflow_active(config):
    from core import workflow

    return workflow.active_name(config)


def _read_active_model(config):
    return config.model_name


def _read_log_level(config):
    level = str((config.data.get("log") or {}).get("level") or "info").strip().lower()
    if level == "off":
        return "关闭"
    return level if level in LOG_LEVELS else "info"


special_read: Dict[str, Callable] = {
    "mode": _read_mode,
    "workflow_active": _read_workflow_active,
    "active_model_name": _read_active_model,
    "log_level": _read_log_level,
}


# ----- 特殊键：写入带副作用或校验 -----

def _write_mode(config, raw) -> bool:
    config.mode = str(raw)  # 属性 setter 校验并落盘 ui.mode
    return True


def _write_font_size(config, raw) -> bool:
    size = coerce({"type": "int", "min": 8}, raw)
    if size is None:
        return False
    config.data.setdefault("ui", {})["font_size"] = size
    config.data.setdefault("system", {})["font_size"] = size
    return True


def _write_log_level(config, raw) -> bool:
    level = {"关闭": "off"}.get(str(raw).strip(), str(raw).strip().lower())
    if level not in LOG_LEVELS:
        return False
    log_cfg = config.data.setdefault("log", {})
    if log_cfg.get("level") != level:
        log_cfg["level"] = level
        init_logging(log_cfg)
    return True


def _write_workflow_active(config, raw) -> bool:
    if not raw:
        return False
    from core import workflow

    workflow.set_active(config, str(raw))
    return True


def _write_active_model(config, raw) -> bool:
    if not raw:
        return False
    config.switch_model(str(raw))
    return True


def _write_default_shell(config, raw) -> bool:
    name = shells.resolve_name(str(raw))
    if not shells.is_valid(name):
        return False
    config.data.setdefault("tools", {})["default_shell"] = name
    return True


special_write: Dict[str, Callable] = {
    "mode": _write_mode,
    "font_size": _write_font_size,
    "log_level": _write_log_level,
    "workflow_active": _write_workflow_active,
    "active_model_name": _write_active_model,
    "default_shell": _write_default_shell,
}
