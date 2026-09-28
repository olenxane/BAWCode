#命令系统：执行器与元数据解耦，预留插件扩展接口（核心命令；UI/配置不挂外部 API）
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.log import get_logger

log = get_logger("commands")

_ROOT = Path(__file__).resolve().parent.parent

_handlers: Dict[str, Callable] = {}
_metas: Dict[str, dict] = {}
_aliases: Dict[str, str] = {}
# 命令参数补全器：name -> fn(config, arg) -> list[dict]
_ARG_COMPLETERS: Dict[str, Callable] = {}


def _normalize(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if not name.startswith("/"):
        name = "/" + name
    return name.lower()


def register_arg_completer(name: str, fn: Callable) -> None:
    """注册命令参数补全器（插件可扩展）；fn(config, arg) -> [{name,hint}, ...]"""
    key = _normalize(name)
    if key and fn is not None:
        _ARG_COMPLETERS[key] = fn


def _filter_arg_items(items: List[dict], arg: str) -> List[dict]:
    """参数补全：对最后一个参数做前缀匹配（/mode a → auto）"""
    arg = (arg or "").strip().lower()
    if not arg:
        return items
    out = []
    for item in items:
        full = str(item.get("name", ""))
        parts = full.split()
        token = parts[-1].lower() if parts else full.lower()
        if token.startswith(arg):
            out.append(item)
    return out


def _complete_mode(config, arg: str) -> List[dict]:
    rows = [
        ("auto", "自动模式 · 安全指令放行"),
        ("manual", "手动模式 · 只读放行"),
        ("full", "完全访问 · 全部放行"),
    ]
    items = [{"name": f"/mode {m}", "hint": h, "source": "arg", "callable": True} for m, h in rows]
    return _filter_arg_items(items, arg)


def _complete_theme(config, arg: str) -> List[dict]:
    names = ["dark", "ocean"]
    if config is not None and hasattr(config, "list_themes"):
        try:
            names = config.list_themes() or names
        except Exception:
            pass
    items = [{"name": f"/theme {n}", "hint": "主题", "source": "arg", "callable": True} for n in names]
    return _filter_arg_items(items, arg)


def _complete_model(config, arg: str) -> List[dict]:
    if config is None:
        return []
    rows = config.match_models(arg or "", limit=20) if hasattr(config, "match_models") else []
    items = []
    for row in rows:
        items.append(
            {
                "name": f"/model {row['model_name']}",
                "hint": f"{row['model_id']} · {row.get('base_url','')}",
                "source": "arg",
                "callable": True,
                "match_rank": 0 if (arg and row["model_name"].lower().startswith(arg.lower())) else 1,
            }
        )
    items.sort(key=lambda x: (x.get("match_rank", 1), x["name"]))
    return items


def _complete_plugin_echo(config, arg: str) -> List[dict]:
    samples = ["hello", "world", "测试"]
    items = [{"name": f"/plugin_echo {s}", "hint": "示例参数", "source": "arg", "callable": True} for s in samples]
    return _filter_arg_items(items, arg)


# 内置参数补全
register_arg_completer("/mode", _complete_mode)
register_arg_completer("/theme", _complete_theme)
register_arg_completer("/model", _complete_model)
register_arg_completer("/use", _complete_model)
register_arg_completer("/plugin_echo", _complete_plugin_echo)


def set_handler(name: str, handler: Callable) -> None:
    key = _normalize(name)
    if not key or handler is None:
        return
    _handlers[key] = handler
    _metas.setdefault(key, {"hint": "", "usage": "", "aliases": [], "source": "runtime"})
    log.debug("注册命令: %s", key)


def set_meta(
    name: str,
    hint: str = "",
    usage: str = "",
    aliases: Optional[List[str]] = None,
    source: str = "runtime",
) -> None:
    key = _normalize(name)
    if not key:
        return
    meta = _metas.setdefault(key, {"hint": "", "usage": "", "aliases": [], "source": source})
    if hint:
        meta["hint"] = hint
    if usage:
        meta["usage"] = usage
    if aliases:
        cleaned = []
        for alias in aliases:
            an = _normalize(alias)
            if an:
                _aliases[an] = key
                cleaned.append(an)
        meta["aliases"] = cleaned
    meta["source"] = source


def register(
    name: str,
    hint: str = "",
    usage: str = "",
    aliases: Optional[List[str]] = None,
    handler: Optional[Callable] = None,
    source: str = "builtin",
):
    def _bind(fn: Callable) -> Callable:
        set_handler(name, fn)
        set_meta(name, hint=hint, usage=usage, aliases=aliases, source=source)
        return fn

    if handler is not None:
        set_handler(name, handler)
        set_meta(name, hint=hint, usage=usage, aliases=aliases, source=source)
        return handler
    return _bind


def bind_alias(alias: str, target: str) -> None:
    a = _normalize(alias)
    t = _normalize(target)
    if a and t:
        _aliases[a] = t
        meta = _metas.setdefault(t, {"hint": "", "usage": "", "aliases": [], "source": "runtime"})
        if a not in meta.get("aliases", []):
            meta.setdefault("aliases", []).append(a)


def resolve(name: str) -> str:
    key = _normalize(name)
    return _aliases.get(key, key)


def has_handler(name: str) -> bool:
    return resolve(name) in _handlers


def get_handler(name: str) -> Optional[Callable]:
    return _handlers.get(resolve(name))


def get_meta(name: str) -> dict:
    key = resolve(name)
    meta = dict(_metas.get(key, {}))
    meta["name"] = key
    return meta


def list_commands(include_handlerless: bool = True) -> List[dict]:
    names = set(_metas) | set(_handlers)
    items = []
    for key in sorted(names):
        if not include_handlerless and key not in _handlers:
            continue
        meta = get_meta(key)
        meta["callable"] = key in _handlers
        items.append(meta)
    return items


def complete(prefix: str, limit: int = 12, config: Any = None) -> List[dict]:
    """命令名 + 参数补全

    - `/mo` → 命令名补全
    - `/mode ` 或 `/mode a` → 参数补全（auto/manual/full）
    - `/model deep` → 模型名补全（model_name 优先于 provider_id）
    - 插件可用 register_arg_completer 扩展任意命令参数
    """
    raw = (prefix or "")
    if not raw.startswith("/"):
        return []
    # 保留原始是否已输入空格（用于判断是否进入参数补全）
    has_arg_sep = " " in raw
    parts = raw.split(maxsplit=1)
    head = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    resolved = resolve(head) if head else ""

    # 参数补全：已出现空格，或命令名已精确命中且带参数片段
    completer = _ARG_COMPLETERS.get(resolved) or _ARG_COMPLETERS.get(head)
    if completer is not None and (has_arg_sep or (arg and resolved in _ARG_COMPLETERS)):
        items = completer(config, arg)
        if items:
            return items[:limit]
        if has_arg_sep:
            return []

    # 命令名补全
    lowered = raw.lower()
    exact_prefix, substring = [], []
    for item in list_commands(include_handlerless=True):
        name = item.get("name", "")
        aliases = item.get("aliases") or []
        pool = [name] + list(aliases)
        if any(p.lower().startswith(lowered) for p in pool):
            exact_prefix.append(dict(item))
        elif any(lowered in p.lower() for p in pool):
            substring.append(dict(item))
    exact_prefix.sort(key=lambda x: (len(x.get("name", "")), x.get("name", "")))
    substring.sort(key=lambda x: (len(x.get("name", "")), x.get("name", "")))
    items = exact_prefix + substring
    # 有参数补全器的命令：在命令名精确命中后追加首条参数建议，便于一次 Tab 看到参数
    if not has_arg_sep:
        expanded = list(items)
        for item in list(items):
            name = (item.get("name") or "").lower()
            fn = _ARG_COMPLETERS.get(resolve(name)) or _ARG_COMPLETERS.get(name)
            if fn:
                try:
                    suggestions = fn(config, "")[:2]
                except Exception:
                    suggestions = []
                expanded.extend(suggestions)
        items = expanded
    return items[:limit]


def execute(line: str, ctx: Any = None) -> Any:
    text = (line or "").strip()
    if not text.startswith("/"):
        return {"ok": False, "reason": "not_command"}
    parts = text.split(maxsplit=1)
    name = resolve(parts[0])
    args = parts[1] if len(parts) > 1 else ""
    if name not in _handlers and name not in _metas:
        log.warn("未知命令: %s", parts[0])
        return {"ok": False, "reason": "unknown", "name": parts[0]}
    handler = _handlers.get(name)
    if handler is None:
        log.warn("命令无处理器: %s", name)
        return {"ok": False, "reason": "no_handler", "name": name}
    log.info("执行命令: %s %s", name, args)
    return handler(ctx, args)


def load_plugins(directory: Optional[str] = None) -> List[str]:
    plugin_dir = Path(directory) if directory else _ROOT / "data" / "commands"
    loaded: List[str] = []
    if not plugin_dir.exists():
        log.debug("插件目录不存在: %s", plugin_dir)
        return loaded
    for path in sorted(plugin_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"bawcode_cmdplugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                log.warn("插件加载失败（无法构造 spec）: %s", path.name)
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            module.commands = sys.modules[__name__]
            spec.loader.exec_module(module)
            setup = getattr(module, "setup", None)
            if callable(setup):
                setup(sys.modules[__name__])
            loaded.append(str(path))
            log.info("插件已加载: %s", path.name)
        except Exception as e:
            log.warn("插件加载失败 %s: %s: %s", path.name, type(e).__name__, e)
            continue
    log.info("插件加载完成: %d个（目录 %s）", len(loaded), plugin_dir)
    return loaded


def help_text() -> str:
    lines = ["命令:"]
    for item in list_commands():
        name = item.get("name", "")
        hint = item.get("hint") or ""
        mark = "" if item.get("callable") else " [meta]"
        lines.append(f"  {name:16} {hint}{mark}")
    return "\n".join(lines)
