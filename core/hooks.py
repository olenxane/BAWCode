# 外部 API 扩展点：用户参与及相关能力可挂接外部服务
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from core.log import get_logger

log = get_logger("hooks")

# 事件名 -> 处理函数
_handlers = {}

# 默认超时（秒）
DEFAULT_TIMEOUT = 60


def register_hook(event: str, handler: Optional[Callable[[dict], Any]] = None):
    """注册扩展点处理函数

    支持直接传入 handler，或作为装饰器使用：
      @register_hook("prompt_refine")
      def fn(payload): ...

    :param event: 事件名，如 prompt_refine / plan_confirm
    :param handler: 接收 payload(dict) 并返回结果的可调用对象
    :return: 原 handler 或装饰器
    """

    def _decorator(func: Callable[[dict], Any]) -> Callable[[dict], Any]:
        _handlers[event] = func
        log.debug("注册扩展点: %s", event)
        return func

    if handler is not None:
        log.debug("注册扩展点: %s", event)
        return _decorator(handler)
    return _decorator


def clear_hooks(event: Optional[str] = None) -> None:
    """清除指定事件或全部扩展点"""
    if event is None:
        log.debug("清除全部扩展点")
        _handlers.clear()
    else:
        log.debug("清除扩展点: %s", event)
        _handlers.pop(event, None)


def set_external_apis(api_map: Optional[dict]) -> None:
    """根据配置中的 external_apis 注册 HTTP 外部接口

    配置值为 URL 字符串时，自动挂接 HTTP POST 扩展点；
    值为 null 时保持本地默认逻辑。
    """
    if not api_map:
        return
    for event, url in api_map.items():
        if url:
            _handlers[event] = _make_http_handler(event, url)
            log.info("外部接口已挂接: %s -> %s", event, url)


def call_hook(event: str, payload: Optional[dict] = None, default: Any = None) -> Any:
    """触发扩展点；无处理函数时返回 default；自动注入 _event"""
    handler = _handlers.get(event)
    if handler is None:
        return default
    data = dict(payload) if payload is not None else {}
    data.setdefault("_event", event)
    try:
        result = handler(data)
    except Exception as e:
        log.warn("扩展点执行失败 event=%s: %s", event, e)
        return default
    return default if result is None else result


def _make_http_handler(event: str, url: str) -> Callable[[dict], Any]:
    """构造 POST JSON 的外部接口处理函数

    请求体: {"event": <事件名>, "payload": {...}}
    响应体: JSON 对象，将原样作为扩展点返回值
    """

    def _handler(payload: dict) -> Any:
        body = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            # 外部接口失败时回落到本地默认逻辑
            log.warn("外部接口调用失败，回落本地逻辑: event=%s url=%s (%s)", event, url, e)
            return None

    return _handler


def call_user_participating(
    event: str,
    payload: Optional[dict] = None,
    handler: Optional[Callable[[dict], Any]] = None,
    default: Any = None,
) -> Any:
    """用户参与型功能统一入口

    优先级：显式 handler > 注册扩展点 > default
    显式 handler 便于调用方临时注入外部 API，而不修改全局配置。
    """
    data = dict(payload or {})
    if handler is not None:
        data.setdefault("_event", event)
        try:
            result = handler(data)
        except Exception as e:
            log.warn("用户参与扩展点失败 event=%s: %s", event, e)
            return default
        return default if result is None else result
    return call_hook(event, data, default=default)
