# Hook 系统：进程内多处理函数事件链 + external_apis HTTP 外部接口
#
# 两类调用语义：
#   call_hook    —— 变换链：按优先级依次执行，处理函数返回 dict 时与 payload 合并
#                   （浅合并）并继续向后传递；最终返回值为链条中各 dict 贡献的浅合并
#                   （后者覆盖前者），非 dict 结果原样返回；全部返回 None 或无处理
#                   函数时返回 default。适配 llm_request/prompt_refine/before_tool
#                   这类"可改写输入"的扩展点。
#   collect_hook —— 观察链：收集所有非 None 返回值组成列表返回。适配
#                   context_supplement/after_turn/session_start 这类"多方贡献"的扩展点。
#
# 每个处理函数带 owner 归属（插件 id），支持按 owner 注销（插件卸载）；单个处理
# 函数异常被隔离记录，不中断链条。
#
# 与 config.external_apis 的关系：external_apis.<事件>=URL 时自动挂接 HTTP POST
# 处理函数（owner=external_apis），失败回落本地默认逻辑（返回 None）。
#
# 事件目录见 EVENTS 与 docs/plugin-development.md。
import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.log import get_logger

log = get_logger("hooks")

# 事件名 -> [{owner, priority, name, handler, seq}]
_handlers: Dict[str, List[dict]] = {}
_seq = 0
_lock = threading.Lock()

# 默认超时（秒）
DEFAULT_TIMEOUT = 60

# 事件目录：call=变换链 collect=观察链。
# 用户参与型外部能力（external_apis 对应项）+ 生命周期事件。
EVENTS: Dict[str, dict] = {
    # ---- 用户参与型能力（external_apis 同名映射；call 变换链）----
    "prompt_refine": {"kind": "call", "desc": "改写用户的编码任务提示"},
    "plan_generate": {"kind": "call", "desc": "外部生成/接收计划内容"},
    "plan_confirm": {"kind": "call", "desc": "计划确认回调"},
    "step_generate": {"kind": "call", "desc": "外部生成执行步骤（返回 {steps: []} 生效）"},
    "step_update": {"kind": "call", "desc": "步骤状态变更通知"},
    "memory_write": {"kind": "call", "desc": "长期记忆写入旁路"},
    "memory_read": {"kind": "call", "desc": "长期记忆读取旁路"},
    "rag_add": {"kind": "call", "desc": "RAG 文档加入旁路"},
    "rag_query": {"kind": "call", "desc": "RAG 查询旁路"},
    "computer_use": {"kind": "call", "desc": "computer-use 外部执行接口"},
    "llm_request": {"kind": "call", "desc": "整段接管/转发 LLM 请求（返回 chat 响应结构生效）"},
    "tool_confirm": {"kind": "call", "desc": "工具确认代答，首个有效应答生效；None 交回 UI 面板"},
    "tool_confirm_closed": {"kind": "collect", "desc": "工具确认已应答，按 request_id 关闭其他通道请求"},
    "ui_request": {"kind": "call", "desc": "交互弹窗代答 confirm/choose/line/ask（返回与键盘输入同语义的应答值生效；None 交回 UI 面板）"},
    # ---- 生命周期事件 ----
    "session_start": {"kind": "collect", "desc": "会话初始化完成（collect）"},
    "before_turn": {"kind": "call", "desc": "回合开始，可改写 user_text（返回 {user_text: ...} 生效）"},
    "after_turn": {"kind": "collect", "desc": "回合结束通知（collect）"},
    "stream_delta": {"kind": "collect", "desc": "流式输出增量通知 {kind, piece}（collect，高频率）"},
    "turn_status": {"kind": "collect", "desc": "回合状态/阶段变化通知 {status} 或 {phase}（collect）"},
    "before_tool": {"kind": "call", "desc": "工具执行前，可改写 args 或拒绝执行"},
    "after_tool": {"kind": "collect", "desc": "工具执行后通知（collect）"},
    "context_supplement": {"kind": "collect", "desc": "回合上下文补充文本（collect，返回 str 生效）"},
    "message_added": {"kind": "collect", "desc": "会话消息新增通知 {role, content, type, ...extra}（collect，content 可能为多模态数组；处理函数内禁止再写会话消息，否则递归）"},
    "ui_tree_nodes": {"kind": "collect", "desc": "每帧提供插件主窗口会话树根节点（collect）"},
    "ui_bottom_rows": {"kind": "collect", "desc": "每帧提供主窗口统计状态栏下方的 ANSI 文本行（collect）"},
}


def _register(
    event: str,
    handler: Callable[[dict], Any],
    priority: int = 100,
    owner: str = "",
    name: str = "",
) -> None:
    global _seq
    if not callable(handler):
        raise TypeError("hook handler 必须可调用")
    with _lock:
        _seq += 1
        _handlers.setdefault(event, []).append(
            {"owner": owner or "", "priority": int(priority), "name": name or "", "handler": handler, "seq": _seq}
        )
    log.debug("注册扩展点: %s owner=%s priority=%s", event, owner or "-", priority)


def register_hook(
    event: str,
    handler: Optional[Callable[[dict], Any]] = None,
    priority: int = 100,
    owner: str = "",
    name: str = "",
):
    """注册扩展点处理函数（同一事件可注册多个，按 priority 升序执行，同优先级按注册顺序）

    支持直接传入 handler，或作为装饰器使用：
      @register_hook("prompt_refine", owner="my-plugin")
      def fn(payload): ...

    :param event: 事件名，见 EVENTS 目录
    :param handler: 接收 payload(dict) 并返回结果的可调用对象
    :param priority: 优先级，数值小者先执行
    :param owner: 归属标识（插件 id），便于卸载与诊断
    :param name: 处理函数名（诊断用，缺省取函数 __name__）
    :return: 原 handler 或装饰器
    """

    def _decorator(func: Callable[[dict], Any]) -> Callable[[dict], Any]:
        _register(event, func, priority=priority, owner=owner, name=name or getattr(func, "__name__", ""))
        return func

    if handler is not None:
        _register(event, handler, priority=priority, owner=owner, name=name or getattr(handler, "__name__", ""))
        return handler
    return _decorator


def unregister_hook(event: str, handler: Optional[Callable] = None, owner: str = "", name: str = "") -> int:
    """按 handler 身份 / owner / name 注销处理函数（任一条件匹配即注销），返回注销数量"""
    with _lock:
        entries = _handlers.get(event)
        if not entries:
            return 0
        kept, removed = [], 0
        for entry in entries:
            match = (
                (handler is not None and entry["handler"] is handler)
                or (owner and entry["owner"] == owner)
                or (name and entry["name"] == name)
            )
            if match:
                removed += 1
            else:
                kept.append(entry)
        if removed:
            if kept:
                _handlers[event] = kept
            else:
                _handlers.pop(event, None)
    if removed:
        log.debug("注销扩展点 %s: %d 个", event, removed)
    return removed


def remove_owner(owner: str) -> int:
    """注销某插件/来源注册的全部处理函数（插件卸载时调用），返回注销数量"""
    if not owner:
        return 0
    total = 0
    with _lock:
        for event in list(_handlers.keys()):
            entries = _handlers.get(event)
            if not entries:
                continue
            kept = [e for e in entries if e["owner"] != owner]
            if len(kept) != len(entries):
                total += len(entries) - len(kept)
                if kept:
                    _handlers[event] = kept
                else:
                    _handlers.pop(event, None)
    if total:
        log.debug("注销 owner=%s 的扩展点: %d 个", owner, total)
    return total


def clear_hooks(event: Optional[str] = None) -> None:
    """清除指定事件或全部扩展点"""
    with _lock:
        if event is None:
            log.debug("清除全部扩展点")
            _handlers.clear()
        else:
            log.debug("清除扩展点: %s", event)
            _handlers.pop(event, None)


def _ordered(event: str) -> List[dict]:
    """按 (priority, 注册顺序) 排序的处理函数快照（注册并发安全）"""
    with _lock:
        entries = _handlers.get(event)
        if not entries:
            return []
        return sorted(entries, key=lambda e: (e["priority"], e["seq"]))


def _invoke(entry: dict, data: dict) -> Any:
    try:
        return entry["handler"](data)
    except Exception as e:
        log.warn("扩展点处理函数执行失败 event=%s owner=%s: %s", data.get("_event"), entry["owner"] or "-", e)
        return None


def call_hook(event: str, payload: Optional[dict] = None, default: Any = None) -> Any:
    """变换链触发：无结果/无处理函数返回 default

    处理函数返回 dict 时与当前 payload 浅合并后继续向后传递（可逐步改写）；
    返回 None 表示"不参与"，不影响链条。最终返回值为各 dict 贡献的浅合并
    （后者覆盖前者），非 dict 结果原样返回——观察型处理函数应返回 None。
    """
    data = dict(payload) if payload is not None else {}
    data.setdefault("_event", event)
    if event == "tool_confirm":
        return confirm_hook(data, default)
    result = None
    merged: Dict[str, Any] = {}
    for entry in _ordered(event):
        out = _invoke(entry, data)
        if out is None:
            continue
        result = out
        if isinstance(out, dict):
            merged.update(out)
            data.update(out)
    if result is None:
        return default
    return merged if isinstance(result, dict) else result


def confirm_hook(data: dict, default: Any = None) -> Any:
    """工具确认采用首个有效应答，并通知其余通道关闭请求。"""
    entries = _ordered("tool_confirm")
    data = dict(data)
    cancelled = data.pop("cancelled", None)
    data.setdefault("request_id", uuid.uuid4().hex)
    for winner in entries:
        if callable(cancelled) and cancelled():
            collect_hook("tool_confirm_closed", dict(data, phase="closed", action="deny"))
            return {"action": "deny"}
        out = _invoke(winner, dict(data))
        if callable(cancelled) and cancelled():
            collect_hook("tool_confirm_closed", dict(data, phase="closed", action="deny"))
            return {"action": "deny"}
        if not isinstance(out, dict):
            continue
        action = str(out.get("action") or "").lower()
        if action in ("denied", "reject"):
            action = "deny"
        if action not in ("allow_once", "allow_always", "deny"):
            continue
        result = dict(out, action=action)
        collect_hook("tool_confirm_closed", dict(data, phase="closed", action=result["action"]))
        return result
    return default


def collect_hook(event: str, payload: Optional[dict] = None) -> List[Any]:
    """观察链触发：收集全部非 None 返回值（顺序=执行顺序），异常隔离"""
    data = dict(payload) if payload is not None else {}
    data.setdefault("_event", event)
    results = []
    for entry in _ordered(event):
        out = _invoke(entry, data)
        if out is not None:
            results.append(out)
    return results


def set_external_apis(api_map: Optional[dict]) -> None:
    """根据配置中的 external_apis 注册 HTTP 外部接口

    配置值为 URL 字符串时，自动挂接 HTTP POST 扩展点（owner=external_apis，
    重复调用时替换旧挂接）；值为 null 时保持本地默认逻辑。
    """
    if not api_map:
        return
    for event, url in api_map.items():
        if url:
            unregister_hook(event, owner="external_apis")
            _register(event, _make_http_handler(event, url), priority=50, owner="external_apis", name="http")
            log.info("外部接口已挂接: %s -> %s", event, url)


def list_hooks() -> Dict[str, List[dict]]:
    """诊断视图：事件 -> [{owner, priority, name}]（不暴露 handler 本体）"""
    with _lock:
        events = list(_handlers.keys())
    out: Dict[str, List[dict]] = {}
    for event in events:
        out[event] = [
            {"owner": e["owner"], "priority": e["priority"], "name": e["name"]}
            for e in _ordered(event)
        ]
    return out


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

    优先级：显式 handler > 注册扩展点（变换链） > default
    显式 handler 便于调用方临时注入外部 API，而不修改全局配置。
    """
    if handler is not None:
        data = dict(payload or {})
        data.setdefault("_event", event)
        try:
            result = handler(data)
        except Exception as e:
            log.warn("用户参与扩展点失败 event=%s: %s", event, e)
            return default
        return default if result is None else result
    return call_hook(event, payload, default=default)
