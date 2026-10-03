#该文件实现一个工具装饰器的注册，将工具独立出来，便于后续导入和增加工具，使程序插件化
import time
from typing import Any, Callable, Dict, List, Optional

from core.log import get_logger

log = get_logger("register")

# name -> 工具元数据
_registry: Dict[str, dict] = {}


def register(
    name: Optional[str] = None,
    description: str = "",
    usage: str = "",
    schema: Optional[dict] = None,
    owner: str = "",
) -> Callable[[Callable], Callable]:
    """工具注册装饰器

    :param name: 工具名，默认取函数名
    :param description: 工具描述，供模型选择
    :param usage: 用法说明
    :param schema: JSON Schema 参数结构
    :param owner: 归属标识（插件 id），便于整体注销
    """

    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        _registry[tool_name] = {
            "name": tool_name,
            "func": func,
            "description": description,
            "usage": usage,
            "schema": schema or {"type": "object", "properties": {}, "required": []},
            "owner": owner or "",
        }
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
    """注销工具（MCP 重连时撤销已下线的工具）；返回是否确有注销"""
    return _registry.pop(name, None) is not None


def unregister_owner(owner: str) -> List[str]:
    """注销某归属（插件）注册的全部工具，返回被注销的工具名列表"""
    if not owner:
        return []
    removed = [n for n, t in _registry.items() if t.get("owner") == owner]
    for n in removed:
        _registry.pop(n, None)
    if removed:
        log.debug("注销 owner=%s 的工具: %s", owner, ",".join(removed))
    return removed


def call(tool_name: str, **kwargs: Any) -> Any:
    """调用已注册工具（首参命名避开 kwargs 撞名：工具形参可含 name）"""
    if tool_name not in _registry:
        log.warn("调用未注册工具: %s", tool_name)
        return f"工具不存在: {tool_name}"
    started = time.monotonic()
    try:
        return _registry[tool_name]["func"](**kwargs)
    finally:
        log.debug("工具调用 %s 用时 %.3fs", tool_name, time.monotonic() - started)


# 调用说明参数：每次工具调用随 arguments 传入，回合末作为该调用的简明记录
# （见 memory.finalize_turn）；执行前会被剥除，不进入工具函数
DESCRIPTION_PARAM = {
    "type": "string",
    "description": "本次工具调用的简短说明（一句话，说明本次调用想做什么，供调用记录留档）",
}


def _with_description_param(schema: Optional[dict]) -> dict:
    """向工具参数 schema 注入公共 description 属性（幂等，不覆盖已有定义）"""
    base = dict(schema or {"type": "object", "properties": {}, "required": []})
    properties = dict(base.get("properties") or {})
    properties.setdefault("description", dict(DESCRIPTION_PARAM))
    base["properties"] = properties
    required = list(base.get("required") or [])
    if "description" not in required:
        required.append("description")
    base["required"] = required
    return base


def get_tool_defs() -> List[dict]:
    """导出 OpenAI function calling 格式的工具定义（自动注入公共 description 参数）。
    迭代用快照：MCP 工具经后台线程注册，避免注册期间字典变更导致 RuntimeError"""
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
