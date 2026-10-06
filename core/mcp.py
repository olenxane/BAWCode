#MCP（Model Context Protocol）客户端桥接：把外部 MCP 服务器的工具接入本机工具循环
#设计见 docs/mcp-integration.md。要点：
#- 全部 MCP IO 跑在一个 daemon 事件循环线程里，同步侧经 run_coroutine_threadsafe 桥接，
#  不改动 BAWCode 纯同步 + threading 的整体模型（信号处理只在主线程，loop 线程无此需求）
#- 工具调用超时用协程内 asyncio.wait_for 实现真实取消（不留悬挂协程），外层 .result() 仅兜底
#- 启动后台连接发现，不阻塞 TUI；工具注册进 register 后下一轮 get_tool_defs 自动可见
#- 三态状态表 connected/connecting/disconnected + last_error（无 FAILED 态，失败=disconnected+原因）
#- 注册名 mcp__<server>__<tool> 按 qwen-code 算法规范化；wrapper 闭包持有原始工具名做 callTool
import asyncio
import contextlib
import copy
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import register
from core.log import get_logger

log = get_logger("mcp")

_ROOT = Path(__file__).resolve().parent.parent

# 状态常量（字符串便于直接进 UI/日志）
CONNECTED = "connected"
CONNECTING = "connecting"
DISCONNECTED = "disconnected"
_STATUS_ICON = {CONNECTED: "✔", CONNECTING: "▸", DISCONNECTED: "✘"}

# name -> {status, last_error, transport, tools:[注册名], server_tool:{注册名:原始名}}
_STATE: Dict[str, dict] = {}
# 进程级运行时：loop/thread + 每服务器 handle{stack, session, entry}；started 幂等闸
_RUNTIME: Dict[str, Any] = {"loop": None, "thread": None, "handles": {}, "started": False}
# 正在连接/重连的服务器集合（worker 线程间互斥，防重复 spawn）
_INFLIGHT: set = set()
_LOCK = threading.Lock()

_MCP_DEFAULTS = {
    "enabled": False,
    "servers": {},
    "discovery_timeout_stdio": 30,
    "discovery_timeout_http": 5,
    "call_timeout": 600,
    "auto_reconnect": True,
}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def mcp_cfg(config) -> dict:
    """配置解析：默认值 + 类型钳制集中一处（照 subagent_cfg 范式）"""
    data = dict(_MCP_DEFAULTS)
    data.update((getattr(config, "data", None) or {}).get("mcp") or {})
    data["enabled"] = bool(data.get("enabled"))
    data["discovery_timeout_stdio"] = max(1, _int(data.get("discovery_timeout_stdio"), 30))
    data["discovery_timeout_http"] = max(1, _int(data.get("discovery_timeout_http"), 5))
    data["call_timeout"] = max(1, _int(data.get("call_timeout"), 600))
    data["auto_reconnect"] = bool(data.get("auto_reconnect", True))
    servers = data.get("servers")
    data["servers"] = servers if isinstance(servers, dict) else {}
    return data


# ---------------------------------------------------------------------------
# 命名与 schema


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _fnv1a36(text: str) -> str:
    """FNV-1a 36bit → base36 7 位（规范化后缀，保证不同原始名不互相覆盖）"""
    h = 2166136261
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 16777619) % (1 << 36)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    n = h
    while n:
        out = digits[n % 36] + out
        n //= 36
    return (out or "0").rjust(7, "0")[:7]


def normalize_name(server: str, tool: str) -> str:
    """注册名规范化（qwen-code 同款）：≤63 字符且 ^[A-Za-z][A-Za-z0-9_-]*$ 原样保留，
    否则非法字符替换 _、字母开头补齐、截断后追加 _<fnv 哈希>；注册名与原始名分离"""
    raw = f"mcp__{server}__{tool}"
    if len(raw) <= 63 and _NAME_RE.match(raw):
        return raw
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if not re.match(r"^[A-Za-z]", cleaned):
        cleaned = "tool_" + cleaned
    suffix = _fnv1a36(raw)
    return cleaned[: 63 - len(suffix) - 1] + "_" + suffix


def _attr(obj: Any, *names: str):
    """跨版本取属性：mcp 1.x 驼峰（inputSchema/isError/structuredContent/mimeType），
    2.x 蛇形（input_schema/is_error/structured_content/mime_type）——按序探测"""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _normalize_schema(schema: Any) -> dict:
    """inputSchema 规整：必须是 object 型，补齐 properties/required"""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}
    out = copy.deepcopy(schema)
    if out.get("type") != "object":
        # MCP 规范 inputSchema 应为 object；异常声明包一层 value 参数兜底
        out = {"type": "object", "properties": {"value": out}, "required": []}
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    if not isinstance(out.get("required"), list):
        out["required"] = []
    return out


def _coerce_scalar(text: str, kind: str):
    if kind == "boolean":
        low = text.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        return text
    try:
        if kind == "integer":
            return int(text.strip())
        return float(text.strip())
    except (TypeError, ValueError):
        return text


def _coerce_args(schema: dict, args: dict) -> dict:
    """按 schema 把字符串实参矫正为声明类型（GLM/DeepSeek 常发字符串实参）；
    矫正失败保留原值，由服务器端报错喂回模型"""
    props = (schema or {}).get("properties") or {}
    out = {}
    for key, value in (args or {}).items():
        kind = str((props.get(key) or {}).get("type") or "").lower()
        if isinstance(value, str) and kind in ("number", "integer", "boolean"):
            value = _coerce_scalar(value, kind)
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# 结果转换：CallToolResult → 文本（LLM 侧只见文本，同内置工具范式）


def _result_to_text(result: Any) -> str:
    parts: List[str] = []
    for block in (_attr(result, "content") or []):
        kind = _attr(block, "type")
        if kind == "text":
            parts.append(str(_attr(block, "text") or ""))
        elif kind == "image":
            parts.append(f"[图片: {_attr(block, 'mime_type', 'mimeType') or '?'}，base64 {len(_attr(block, 'data') or '')} 字符]")
        elif kind == "audio":
            parts.append(f"[音频: {_attr(block, 'mime_type', 'mimeType') or '?'}]")
        elif kind == "resource_link":
            name = _attr(block, "name") or _attr(block, "title") or ""
            parts.append(f"资源链接: {name} ({_attr(block, 'uri')})".strip())
        elif kind == "resource":
            res = _attr(block, "resource")
            text = _attr(res, "text") if res is not None else None
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(f"[嵌入资源: {_attr(res, 'mime_type', 'mimeType') or '?'} {_attr(res, 'uri') if res else ''}]")
        else:
            parts.append(str(block))
    text = "\n".join(p for p in parts if p).strip()
    if _attr(result, "is_error", "isError"):
        text = f"MCP 工具返回错误: {text or '（服务器未给出详情）'}"
    if not text:
        structured = _attr(result, "structured_content", "structuredContent")
        if structured:
            try:
                text = json.dumps(structured, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(structured)
    return text or "（空结果）"


# ---------------------------------------------------------------------------
# 事件循环线程与同步桥接


def _loop_main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _RUNTIME["loop"] = loop
    try:
        loop.run_forever()
    finally:
        loop.close()


def _ensure_loop() -> None:
    if _RUNTIME.get("loop") is not None and _RUNTIME["loop"].is_running():
        return
    if _RUNTIME.get("thread") is None or not _RUNTIME["thread"].is_alive():
        _RUNTIME["thread"] = threading.Thread(target=_loop_main, daemon=True, name="mcp-loop")
        _RUNTIME["thread"].start()
        # 等 loop 就绪（start 后立即可能还没跑到 set_event_loop）
        for _ in range(100):
            if _RUNTIME.get("loop") is not None:
                return
            threading.Event().wait(0.01)


def _submit(coro, timeout: Optional[float] = None):
    """提交协程到 MCP loop 线程并同步等待；timeout 仅兜底，真实取消靠协程内 wait_for"""
    _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, _RUNTIME["loop"])
    return fut if timeout is None else fut.result(timeout=timeout)


# ---------------------------------------------------------------------------
# 连接 / 发现 / 注册


def _transport_of(entry: dict) -> str:
    kind = str(entry.get("transport") or "").strip().lower()
    if kind in ("stdio", "http", "sse"):
        return kind
    if entry.get("command"):
        return "stdio"
    url = str(entry.get("url") or "")
    if url.lower().rstrip("/").endswith("/sse"):
        return "sse"
    return "http"


def _streamable_ctx(url: str, headers: dict):
    """Streamable HTTP 传输（mcp 1.x/2.x 兼容）：1.x 名为 streamablehttp_client 且直收
    headers；2.x 改名 streamable_http_client 并要求经 create_mcp_http_client 注入"""
    try:
        from mcp.client.streamable_http import streamable_http_client  # mcp 2.x
    except ImportError:
        from mcp.client.streamable_http import streamablehttp_client  # mcp 1.x

        return streamablehttp_client(url, headers=headers or None)
    if headers:
        from mcp.client.streamable_http import create_mcp_http_client

        return streamable_http_client(url, http_client=create_mcp_http_client(headers=headers))
    return streamable_http_client(url)


def _unpack_streams(streams):
    """传输流解包：sse 与 2.x streamable 为 (read, write)；1.x streamable 多带
    get_session_id 回调——统一取前两位"""
    if isinstance(streams, tuple):
        return streams[0], streams[1]
    return streams.read_stream, streams.write_stream


async def _connect_coro(name: str, entry: dict, cfg: dict):
    """建连 + 发现 + 注册（在 MCP loop 线程执行）；handle 字段随之更新。
    传输与 ClientSession 的长生命周期上下文挂在 AsyncExitStack 上，随断开关闭"""
    from mcp import ClientSession
    from mcp.client.stdio import get_default_environment, stdio_client
    from mcp import StdioServerParameters

    handle = _RUNTIME["handles"].setdefault(name, {})
    stack = contextlib.AsyncExitStack()
    try:
        transport = _transport_of(entry)
        if transport == "stdio":
            env = get_default_environment()
            env.update({str(k): str(v) for k, v in (entry.get("env") or {}).items()})
            cwd = str(entry.get("cwd") or "") or None
            params = StdioServerParameters(
                command=str(entry.get("command")),
                args=[str(a) for a in (entry.get("args") or [])],
                env=env,
                cwd=cwd,
            )
            # 服务器 stderr 收编到独立日志（默认 errlog=syd.stderr 会直喷 TUI 终端）
            errlog_path = _ROOT / "data" / "log" / f"mcp-{name}.stderr.log"
            errlog_path.parent.mkdir(parents=True, exist_ok=True)
            errlog = open(errlog_path, "ab")
            stack.callback(errlog.close)
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            timeout = cfg["discovery_timeout_stdio"]
        else:
            url = str(entry.get("url") or "")
            headers = {str(k): str(v) for k, v in (entry.get("headers") or {}).items()}
            if transport == "sse":
                from mcp.client.sse import sse_client

                streams = await stack.enter_async_context(sse_client(url, headers=headers or None))
                read, write = _unpack_streams(streams)
            else:
                streams = await stack.enter_async_context(_streamable_ctx(url, headers))
                read, write = _unpack_streams(streams)
            timeout = cfg["discovery_timeout_http"]
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=timeout)
        listing = await asyncio.wait_for(session.list_tools(), timeout=timeout)

        include = [str(x) for x in (entry.get("include_tools") or [])]
        exclude = [str(x) for x in (entry.get("exclude_tools") or [])]
        registered: List[str] = []
        server_tool: Dict[str, str] = {}
        call_timeout = _int(entry.get("timeout"), 0) or cfg["call_timeout"]
        for tool in (listing.tools or []):
            original = str(tool.name)
            if exclude and original in exclude:
                continue
            if include and original not in include:
                continue
            reg_name = normalize_name(name, original)
            schema = _normalize_schema(_attr(tool, "input_schema", "inputSchema"))
            wrapper = _make_wrapper(name, handle, original, schema, call_timeout, cfg)
            register.register(
                name=reg_name,
                description=f"[mcp:{name}] {getattr(tool, 'description', '') or original}".strip(),
                usage=original,
                schema=schema,
            )(wrapper)
            registered.append(reg_name)
            server_tool[reg_name] = original

        # 重连场景：旧连接注册、新连接已不存在的工具予以撤销
        for stale in (_STATE.get(name) or {}).get("tools") or []:
            if stale not in server_tool:
                register.unregister(stale)
        handle["stack"] = stack
        handle["session"] = session
        handle["entry"] = entry
        _STATE[name] = {
            "status": CONNECTED,
            "last_error": "",
            "transport": transport,
            "tools": registered,
            "server_tool": server_tool,
        }
        log.info("MCP 已连接: %s（%s，%d 个工具）", name, transport, len(registered))
    except BaseException as exc:
        # 未成形的 stack 自行收尾；已成形的留给 disconnect 流程
        try:
            if handle.get("stack") is not stack:
                await asyncio.wait_for(stack.aclose(), timeout=3)
        except Exception:
            pass
        raise


async def _disconnect_coro(name: str):
    handle = _RUNTIME["handles"].get(name) or {}
    stack = handle.pop("stack", None)
    handle.pop("session", None)
    # 状态必须同步落地：否则旧 CONNECTED 残留，/mcp 与重连等待都会被假状态误导
    state = _STATE.get(name)
    if state is not None:
        state["status"] = DISCONNECTED
    if stack is not None:
        # Windows stdio aclose 可能挂起：限时，超时放弃（daemon 线程不阻进程退出）
        try:
            await asyncio.wait_for(stack.aclose(), timeout=5)
        except (Exception, asyncio.CancelledError):
            pass


def _init_state(name: str, entry: dict) -> dict:
    """服务器状态条目取回/初始化（缺省 CONNECTING，transport 由配置推导）"""
    return _STATE.setdefault(
        name,
        {"status": CONNECTING, "last_error": "", "transport": _transport_of(entry), "tools": [], "server_tool": {}},
    )


def _connect_once(name: str, entry: dict, cfg: dict) -> None:
    """一次连接尝试：置 CONNECTING → 连接协程 → 失败落 DISCONNECTED + last_error。
    inflight 互斥由调用方负责（_connect_attempt 或 reconnect 的前置闸）"""
    state = _init_state(name, entry)
    state["status"] = CONNECTING
    try:
        _submit(_connect_coro(name, entry, cfg), timeout=cfg.get("discovery_timeout_stdio", 30) + 30)
    except BaseException as exc:
        state["status"] = DISCONNECTED
        state["last_error"] = _brief_error(exc)
        log.error("MCP 连接失败 %s: %s", name, state["last_error"])


def _connect_attempt(name: str, entry: dict, cfg: dict) -> bool:
    """带 inflight 互斥的一次连接尝试；已在途返回 False"""
    with _LOCK:
        if name in _INFLIGHT:
            return False
        _INFLIGHT.add(name)
    try:
        _connect_once(name, entry, cfg)
        return True
    finally:
        with _LOCK:
            _INFLIGHT.discard(name)


def _disconnect_quiet(name: str) -> None:
    """重连前的静默断开：旧连接清理失败不阻断后续重连"""
    try:
        _submit(_disconnect_coro(name), timeout=8)
    except Exception:
        pass


def _worker(config, name: str, entry: dict, cfg: dict) -> None:
    """连接 worker（后台线程）：单个服务器失败不拖累其他服务器"""
    _connect_attempt(name, entry, cfg)


def _brief_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else repr(exc))[:200]


def _tool_error_state(name: str, message: str, cfg: dict) -> None:
    """调用期连接失败：落状态；auto_reconnect 时后台重连一次（本次调用仍返回错误文本）"""
    state = _STATE.get(name)
    if state is None:
        return
    state["status"] = DISCONNECTED
    state["last_error"] = message
    if cfg.get("auto_reconnect"):
        threading.Thread(
            target=_reconnect_after_failure, args=(name,), daemon=True, name=f"mcp-reconnect-{name}"
        ).start()


def _reconnect_after_failure(name: str) -> None:
    handle = _RUNTIME["handles"].get(name) or {}
    entry = handle.get("entry")
    if not entry:
        return
    _disconnect_quiet(name)
    cfg = mcp_cfg(_LAST_CONFIG[0]) if _LAST_CONFIG[0] else dict(_MCP_DEFAULTS)
    _connect_attempt(name, entry, cfg)


def _make_wrapper(server: str, handle: dict, original: str, schema: dict, call_timeout: int, cfg: dict):
    """生成注册进 register 的同步工具函数；执行桥接到 MCP loop 线程"""

    def _fn(**kwargs) -> str:
        session = handle.get("session")
        if session is None or _STATE.get(server, {}).get("status") != CONNECTED:
            return f"MCP 调用失败({server}): 未连接（可 /mcp reconnect {server} 重连）"
        args = _coerce_args(schema, kwargs)

        async def _invoke():
            return await asyncio.wait_for(session.call_tool(original, args), timeout=call_timeout)

        try:
            fut = asyncio.run_coroutine_threadsafe(_invoke(), _RUNTIME["loop"])
            result = fut.result(timeout=call_timeout + 10)
            return _result_to_text(result)
        except asyncio.TimeoutError:
            return f"MCP 调用失败({server}): 调用超时（{call_timeout}s）"
        except Exception as exc:
            brief = _brief_error(exc)
            _tool_error_state(server, brief, cfg)
            return f"MCP 调用失败({server}): {brief}"

    _fn.__name__ = f"mcp_{server}_{original}"
    _fn.__doc__ = f"MCP 工具 {original}（服务器 {server}）"
    return _fn


# ---------------------------------------------------------------------------
# 对外入口


# register_tools 时的 config 引用（重连时重读配置解析器；单元素列表避开 global 声明）
_LAST_CONFIG: list = [None]


def register_tools(config) -> bool:
    """main 启动挂钩：enabled 且配置了服务器时启动桥接并后台连接发现；
    不阻塞——连接/注册在后台线程完成，工具随注册进度逐个对模型可见"""
    cfg = mcp_cfg(config)
    entries = {k: v for k, v in (cfg.get("servers") or {}).items() if isinstance(v, dict) and v.get("enabled", True)}
    if not cfg["enabled"] or not entries:
        log.info("MCP 未启用或未配置服务器，跳过（config.mcp）")
        return False
    try:
        import mcp  # noqa: F401
    except ImportError:
        log.warn("config.mcp.enabled 但缺少 mcp 包：pip install mcp，已跳过 MCP 桥接")
        return False
    with _LOCK:
        if _RUNTIME.get("started"):
            log.warn("MCP 桥接已启动，忽略重复调用")
            return True
        _RUNTIME["started"] = True
    _LAST_CONFIG[0] = config
    _ensure_loop()
    for name, entry in entries.items():
        name = str(name)
        _init_state(name, entry)
        threading.Thread(target=_worker, args=(config, name, entry, cfg), daemon=True, name=f"mcp-{name}").start()
    log.info("MCP 桥接启动: %d 个服务器后台连接中", len(entries))
    return True


async def _cancel_pending():
    """取消 loop 上所有在途任务（半途的连接/调用），避免 stop 后残留 destroyed-task 报错"""
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def shutdown() -> None:
    """退出挂钩：逐服务器带超时断开，清在途任务后停 loop；任何异常不阻进程退出"""
    loop = _RUNTIME.get("loop")
    if loop is None or not loop.is_running():
        return
    names = list(_RUNTIME.get("handles") or {})
    for name in names:
        try:
            _submit(_disconnect_coro(name), timeout=8)
        except Exception:
            pass
        state = _STATE.get(name)
        if state is not None:
            state["status"] = DISCONNECTED
    try:
        _submit(_cancel_pending(), timeout=5)
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    log.info("MCP 桥接已关闭")


def reconnect(name: str) -> tuple:
    """/mcp reconnect <名字>：手动重连；返回 (ok, 提示文本)"""
    name = (name or "").strip()
    if not name:
        return False, "用法: /mcp reconnect <服务器名>"
    if name not in _STATE:
        return False, f"未配置 MCP 服务器: {name}（可用: {', '.join(_STATE) or '无'}）"
    with _LOCK:
        if name in _INFLIGHT:
            return False, f"{name} 正在连接中"
        _INFLIGHT.add(name)

    def _worker() -> None:
        try:
            _disconnect_quiet(name)
            handle = _RUNTIME["handles"].get(name) or {}
            entry = handle.get("entry")
            if entry is None:
                _STATE[name]["status"] = DISCONNECTED
                _STATE[name]["last_error"] = "无连接配置缓存，请重启程序"
                return
            cfg = mcp_cfg(_LAST_CONFIG[0]) if _LAST_CONFIG[0] else dict(_MCP_DEFAULTS)
            # inflight 闸由 reconnect 前置持有，此处直接尝试连接
            _connect_once(name, entry, cfg)
        finally:
            with _LOCK:
                _INFLIGHT.discard(name)

    threading.Thread(target=_worker, daemon=True, name=f"mcp-reconnect-{name}").start()
    return True, f"{name} 重连中（稍后 /mcp 查看结果）"


def statuses() -> Dict[str, dict]:
    return {name: dict(state) for name, state in _STATE.items()}


def status_listing() -> str:
    """/mcp 命令输出"""
    if not _STATE:
        return "MCP 未启用或未配置服务器（config.mcp.enabled + config.mcp.servers）"
    lines = [f"MCP 服务器 {len(_STATE)} 个:"]
    for name, state in _STATE.items():
        icon = _STATUS_ICON.get(state.get("status"), "?")
        line = (
            f"  {name}  {icon} {state.get('status')} · {state.get('transport')}"
            f" · {len(state.get('tools') or [])} 工具"
        )
        if state.get("last_error"):
            line += f"\n      └ {state['last_error']}"
        lines.append(line)
    lines.append("提示: /mcp tools [名字] 列工具 · /mcp reconnect <名字> 重连")
    return "\n".join(lines)


def tools_listing(name: str = "") -> str:
    """/mcp tools [名字]：列出桥接进来的工具与描述"""
    targets = [name] if name else list(_STATE)
    lines = []
    for server in targets:
        state = _STATE.get(server)
        if state is None:
            lines.append(f"未配置 MCP 服务器: {server}")
            continue
        lines.append(f"{server}（{state.get('status')}）:")
        for reg_name in state.get("tools") or []:
            meta = register.get_tool(reg_name) or {}
            lines.append(f"  {reg_name}: {meta.get('description', '')}")
    if not lines:
        return "MCP 未启用或未配置服务器"
    return "\n".join(lines)


def status_line() -> str:
    """树尾状态行（UI 每帧读取）：无 MCP 活动时返回空串不占渲染"""
    if not _STATE:
        return ""
    parts = []
    for name, state in sorted(_STATE.items()):
        icon = _STATUS_ICON.get(state.get("status"), "?")
        seg = f"{name}{icon}"
        if state.get("status") == CONNECTED:
            seg += f" {len(state.get('tools') or [])}"
        elif state.get("last_error"):
            seg += " " + state["last_error"][:40]
        parts.append(seg)
    return "⧉ MCP " + " · ".join(parts)
