"""Plugin configuration declaration parsing, independent of the host."""
import logging
import math
import re
from typing import Any, List

log = logging.getLogger("bawcode_sdk.configuration")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CONFIG_TYPES = ("str", "list", "int", "float", "bool")


def _coerce_decl_value(ctype: str, value: Any) -> Any:
    """按声明类型规整值；不可转换返回 None"""
    try:
        if value is None:
            return None
        if ctype == "str":
            return str(value)
        if ctype == "bool":
            if isinstance(value, str):
                text = value.strip().lower()
                if text in ("true", "1", "yes", "on", "开"):
                    return True
                if text in ("false", "0", "no", "off", "关"):
                    return False
                return None
            return bool(value) if isinstance(value, (bool, int)) and value in (0, 1) else None
        if ctype in ("int", "float"):
            number = float(value)
            if not math.isfinite(number):
                return None
            return int(number) if ctype == "int" else number
    except (TypeError, ValueError, OverflowError):
        return None
    return value


def _parse_config_decls(manifest: dict, pid: str) -> List[dict]:
    """解析 plugin.json 的 config 声明数组；非法项 warn 后丢弃（不影响装载）"""
    raw = manifest.get("config")
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.warn("插件 %s 的 config 声明必须是数组，已忽略", pid)
        return []
    decls: List[dict] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            log.warn("插件 %s config 声明项必须是对象: %r", pid, item)
            continue
        key = str(item.get("key") or "").strip()
        ctype = str(item.get("type") or "").strip().lower()
        if not _KEY_RE.match(key):
            log.warn("插件 %s config 声明 key 非法（字母开头，字母数字下划线）: %r", pid, key)
            continue
        if ctype not in CONFIG_TYPES:
            log.warn("插件 %s config 声明 %s 类型非法（支持 %s）: %r", pid, key, "/".join(CONFIG_TYPES), ctype)
            continue
        if key in seen:
            log.warn("插件 %s config 声明 key 重复，已跳过: %s", pid, key)
            continue
        seen.add(key)
        decl = {
            "key": key,
            "type": ctype,
            "label": str(item.get("label") or key),
            "hint": str(item.get("hint") or ""),
            "default": _coerce_decl_value(ctype, item.get("default")),
        }
        if ctype == "list":
            options = item.get("options")
            if not isinstance(options, list) or not options:
                log.warn("插件 %s config 声明 %s 为 list 类型但缺非空 options，已跳过", pid, key)
                continue
            decl["options"] = [str(o) for o in options]
            if decl["default"] is not None and decl["default"] not in decl["options"]:
                decl["default"] = decl["options"][0]
        if ctype in ("int", "float"):
            lo = _coerce_decl_value(ctype, item.get("min"))
            hi = _coerce_decl_value(ctype, item.get("max"))
            if lo is not None:
                decl["min"] = lo
            if hi is not None:
                decl["max"] = hi
        decls.append(decl)
    return decls
