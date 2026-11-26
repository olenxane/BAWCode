#该文件实现工具装饰器的注册，是插件系统的基础
import inspect
import json
import time
from typing import Any, Callable, Dict, List, Optional

from core.log import get_logger

log = get_logger("register")

# name -> 工具元数据
_registry: Dict[str, dict] = {}
# 插件覆盖同名内建工具时的原条目留底：插件卸载后恢复内建
_overridden: Dict[str, dict] = {}


def register(
    name: Optional[str] = None,
    description: str = "",
    usage: str = "",
    schema: Optional[dict] = None,
    owner: str = "",
    safe: bool = False,
    safe_actions: Optional[list] = None,
) -> Callable[[Callable], Callable]:
    """工具注册装饰器

    :param name: 工具名，默认取函数名
    :param description: 工具描述，供模型选择
    :param usage: 用法说明
    :param schema: JSON Schema 参数结构
    :param owner: 归属标识即插件 id，便于整体注销
    :param safe: 免确认声明（纯交互/无副作用类，与 policy.SAFE_TOOLS 同待遇；
        标志随注册表条目存亡，插件卸载自动消失）
    :param safe_actions: action 参数级免确认白名单（分发式工具用，如 computer_use
        的 screenshot/window_list 观测动作；命中 args["action"] 即放行，其余 action 照常过门禁）
    """

    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        existing = _registry.get(tool_name)
        if existing is not None and existing.get("owner") != owner and tool_name not in _overridden:
            # 跨归属覆盖一律留底：插件先注册再被内建反覆盖（注册顺序见 main）同样
            # 需要原条目在卸载/再注册时可用
            _overridden[tool_name] = existing
        _registry[tool_name] = {
            "name": tool_name,
            "func": func,
            "description": description,
            "usage": usage,
            "schema": schema or {"type": "object", "properties": {}, "required": []},
            "owner": owner or "",
            "safe": safe is True,  # 严格布尔：仅显式 True 生效，防 "false" 等真值误声明
            # 防 "screenshot" 被拆成单字符名单：字符串包成列表，非迭代类型归空
            "safe_actions": [
                str(a).strip().lower()
                for a in (
                    [safe_actions] if isinstance(safe_actions, str)
                    else safe_actions if isinstance(safe_actions, (list, tuple, set)) else []
                )
                if str(a).strip()
            ],
        }
        if safe is True:
            log.info("注册免确认工具(safe): %s (owner=%s)", tool_name, owner or "-")
        log.debug("注册工具: %s", tool_name)
        return func

    return decorator


def get_tool(name: str) -> Optional[dict]:
    """按名称获取工具元数据"""
    return _registry.get(name)


def list_tools() -> List[dict]:
    """列出全部已注册工具"""
    return list(_registry.values())


def has_tool(name: str) -> bool:
    """判断工具是否存在"""
    return name in _registry


def unregister(name: str) -> bool:
    """注销工具，MCP 重连时撤销已下线的工具；返回是否确有注销"""
    return _registry.pop(name, None) is not None


def unregister_owner(owner: str) -> List[str]:
    """注销某归属插件的全部工具并恢复被覆盖的内建工具，返回被注销的工具名列表"""
    if not owner:
        return []
    removed = [n for n, t in _registry.items() if t.get("owner") == owner]
    for n in removed:
        original = _overridden.pop(n, None)
        if original is not None:
            _registry[n] = original
        else:
            _registry.pop(n, None)
    # 被内建反覆盖时本插件条目留在 _overridden：卸载即清，避免复活已卸载的工具
    for n in [n for n, t in _overridden.items() if t.get("owner") == owner]:
        _overridden.pop(n, None)
    if removed:
        log.debug("注销 owner=%s 的工具: %s", owner, ",".join(removed))
    return removed


def call(tool_name: str, **kwargs: Any) -> Any:
    """调用已注册工具；首参命名避开 kwargs 撞名，工具形参可含 name"""
    if tool_name not in _registry:
        log.warn("调用未注册工具: %s", tool_name)
        return f"工具不存在: {tool_name}"
    started = time.monotonic()
    try:
        return _registry[tool_name]["func"](**kwargs)
    finally:
        log.debug("工具调用 %s 用时 %.3fs", tool_name, time.monotonic() - started)


# 调用说明参数：随每次工具调用的 arguments 传入，回合末作为该调用的留档记录，见 memory.finalize_turn
# 执行前会被剥除，不进入工具函数
DESCRIPTION_PARAM = {
    "type": "string",
    "description": "One-sentence note of what this tool call intends to do, kept as the record of this call",
}


def _with_description_param(schema: Optional[dict]) -> dict:
    """向工具参数 schema 注入公共 description 属性，幂等不覆盖已有定义"""
    base: dict = dict(schema or {"type": "object", "properties": {}, "required": []})
    props_raw = base.get("properties")
    properties: dict = dict(props_raw) if isinstance(props_raw, dict) else {}
    properties.setdefault("description", dict(DESCRIPTION_PARAM))
    base["properties"] = properties
    req_raw = base.get("required")
    required: list = list(req_raw) if isinstance(req_raw, list) else []
    if "description" not in required:
        required.append("description")
    base["required"] = required
    return base


# 常见模型先验参数名 → 本工具真实参数名。只在该工具确实声明目标参数时启用，
# 避免把其他工具的合法参数误映射。未知键仍然丢弃并提示。
_ARG_ALIASES: Dict[str, Dict[str, str]] = {
    "read": {"path": "file_path", "start_line": "offset"},
    "write": {"path": "file_path"},
    "edit_file": {"path": "file_path"},
    "delete_file": {"path": "file_path"},
    "search": {"context_lines": "context", "text": "pattern"},
    "glob": {"text": "pattern"},
}


_ACCEPTED_CACHE: Dict[str, tuple] = {}


def _accepted_params(tool: dict) -> tuple:
    """工具可接受的参数集合；函数含 **kwargs 时不裁剪未知键。

    结果按工具条目对象缓存：历史回放会对每个 tool_call 调用 sanitize，
    避免每轮请求重复 inspect.signature。
    """
    name = str(tool.get("name") or "")
    cached = _ACCEPTED_CACHE.get(name)
    if cached is not None and cached[0] is tool:
        return cached[1]
    schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
    raw_props = schema.get("properties") if isinstance(schema, dict) else None
    props = raw_props if isinstance(raw_props, dict) else {}
    accepted = set(props)
    accept_all = False
    func = tool.get("func")
    if callable(func):
        try:
            params = inspect.signature(func).parameters
            accepted.update(params)
            accept_all = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            pass
    result = (accepted, accept_all)
    _ACCEPTED_CACHE[name] = (tool, result)
    return result


def sanitize_args(tool_name: str, args: Any) -> dict:
    """按工具 schema + 函数签名清洗参数，不修改入参。

    返回:
        args      清洗后的参数（含别名映射）
        dropped   被丢弃的未声明键
        missing   缺失的必填参数
        aliases   实际应用的 {别名: 目标名}
        required  声明的必填参数
        allowed   允许的参数名
    """
    result = {
        "args": dict(args) if isinstance(args, dict) else {},
        "dropped": [],
        "missing": [],
        "aliases": {},
        "required": [],
        "allowed": [],
    }
    tool = _registry.get(tool_name)
    if tool is None:
        return result
    clean = result["args"]
    schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
    raw_required = schema.get("required") if isinstance(schema, dict) else None
    required = [str(k) for k in (raw_required if isinstance(raw_required, list) else []) if str(k)]
    accepted, accept_all = _accepted_params(tool)
    result["required"] = required
    result["allowed"] = sorted(accepted, key=str)
    for alias, target in _ARG_ALIASES.get(tool_name, {}).items():
        if alias in clean and target not in clean and (accept_all or target in accepted):
            clean[target] = clean.pop(alias)
            result["aliases"][alias] = target
    if not accept_all:
        dropped = [k for k in clean if k not in accepted]
        for key in dropped:
            clean.pop(key, None)
        result["dropped"] = dropped
    result["missing"] = [k for k in required if k not in clean]
    return result


def _example_value(prop: Any) -> Any:
    if not isinstance(prop, dict):
        return "..."
    kind = str(prop.get("type") or "").lower()
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    return "..."


def arg_error_message(tool_name: str, sanitized: dict, detail: str = "") -> str:
    """把参数校验结果转成模型可自纠的结构化错误（允许参数 + 必填 + 最小示例）"""
    missing = sanitized.get("missing") or []
    allowed = sanitized.get("allowed") or []
    required = sanitized.get("required") or []
    lines = []
    if missing:
        suffix = f"（{detail}）" if detail else ""
        lines.append(f"工具参数错误: {tool_name} 缺少必填参数: {', '.join(missing)}{suffix}")
    elif detail:
        lines.append(f"工具参数错误: {tool_name} 调用参数不合法: {detail}")
    else:
        lines.append(f"工具参数错误: {tool_name} 调用参数不合法")
    if allowed:
        lines.append("允许参数: " + ", ".join(str(k) for k in allowed))
    if required:
        lines.append("必填参数: " + ", ".join(str(k) for k in required))
    tool = _registry.get(tool_name) or {}
    schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
    raw_props = schema.get("properties") if isinstance(schema, dict) else None
    props = raw_props if isinstance(raw_props, dict) else {}
    keys = required or [k for k in props if k in allowed][:2]
    if keys:
        example = {str(k): _example_value(props.get(k)) for k in keys}
        lines.append("示例: " + json.dumps(example, ensure_ascii=False))
    lines.append("请只输出上述 schema 中声明的参数，不要枚举其他工具的参数。")
    return "\n".join(lines)


def get_tool_defs() -> List[dict]:
    """导出 OpenAI function calling 格式工具定义，自动注入公共 description 参数
    迭代用快照：MCP 工具经后台线程注册，防注册期间字典变更 RuntimeError"""
    defs = []
    for tool in list(_registry.values()):
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": _with_description_param(tool["schema"]),
                },
            }
        )
    return defs


MCP_OWNER_PREFIX = "mcp:"


def get_tool_defs_grouped() -> tuple:
    """按来源分组导出工具定义：(内置, MCP)

    MCP 工具注册时 owner="mcp:{server}"（core/mcp.py），据此拆分供分类预算计量；
    两组合并即 get_tool_defs() 的完整集合，发送顺序不变。"""
    builtin: List[dict] = []
    mcp: List[dict] = []
    for tool in list(_registry.values()):
        defn = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": _with_description_param(tool["schema"]),
            },
        }
        if str(tool.get("owner") or "").startswith(MCP_OWNER_PREFIX):
            mcp.append(defn)
        else:
            builtin.append(defn)
    return builtin, mcp
