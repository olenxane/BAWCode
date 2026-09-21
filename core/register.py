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
) -> Callable[[Callable], Callable]:
    """工具注册装饰器

    :param name: 工具名，默认取函数名
    :param description: 工具描述，供模型选择
    :param usage: 用法说明
    :param schema: JSON Schema 参数结构
    """

    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        _registry[tool_name] = {
            "name": tool_name,
            "func": func,
            "description": description,
            "usage": usage,
            "schema": schema or {"type": "object", "properties": {}, "required": []},
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


def call(name: str, **kwargs: Any) -> Any:
    """调用已注册工具"""
    if name not in _registry:
        log.warn("调用未注册工具: %s", name)
        return f"工具不存在: {name}"
    started = time.monotonic()
    try:
        return _registry[name]["func"](**kwargs)
    finally:
        log.debug("工具调用 %s 用时 %.3fs", name, time.monotonic() - started)


def get_tool_defs() -> List[dict]:
    """导出 OpenAI function calling 格式的工具定义"""
    defs = []
    for tool in _registry.values():
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["schema"],
                },
            }
        )
    return defs
