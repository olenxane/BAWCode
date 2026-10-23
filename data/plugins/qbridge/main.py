"""qbridge —— 经 OneBot 11 正向 WebSocket 连接 QQ 机器人框架，双向同步终端与 QQ。

连接方式（正向 WS）：BAWCode 作为客户端连接机器人框架（NapCat / LLOneBot /
Lagrange.OneBot / go-cqhttp 等）暴露的 WebSocket 服务端（配置 ws_url，
可选 access_token 鉴权）。断线自动重连（5s 退避）；机器人 QQ 号可留空，
连接后经 get_login_info 自动获取（事件帧里的 self_id 同样会被采纳）。

同步语义（/qbridge on 开启后）：
  终端 → QQ  助手回复（after_turn，超长分段）、终端侧输入的用户消息（[终端]
            前缀，含粘贴图片→消息链）、系统消息（[系统] 前缀，可开关）、
            工具执行活动（🔧 一行摘要，可开关）
  QQ → 终端  白名单用户的消息作为输入框消息注入（ctx.submit_turn：idle 开新
            回合，busy 按设置排队/中断）；图片段下载到本地后走宿主多模态
            管线（app.pending_images）；消息链=文本+图片混合转发
  交互代答  工具确认（tool_confirm 扩展点）与选择框/行输入/询问面板
            （ui_request 扩展点）推送到 QQ 回复编号/文本即完成应答；
            不设 QQ 侧超时——终端提交新消息即手动接管（转回终端面板），
            终端面板自身的取消/超时机制不受影响

安全模型：严格白名单（allow_users，逗号分隔 QQ 号）——名单外的消息一律
忽略；白名单为空时 QQ 侧纯只读镜像（任何消息都不注入终端）。机器人自身
消息（self_id）与 QQ 提交文本的回声均被过滤，不会自激。

依赖宿主扩展点（缺失时相应能力静默降级，插件仍可装载）：
  hooks: tool_confirm / ui_request / message_added / after_turn / before_tool；
  ctx.submit_turn / ctx.notify；plugins.runtime()。

依赖：websockets>=13（requirements.txt 已声明；缺失时 /qbridge 给出提示）。
"""

from bawcode_sdk import PluginContext
import json
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

try:  # 图片下载（requests 为宿主运行时依赖，正常必然可用）
    import requests
except ImportError:
    requests = None

try:  # websockets>=12 提供线程友好的 sync 客户端；缺失时优雅降级
    from websockets.sync.client import connect as _ws_connect
    _WS_OK = True
except ImportError:
    _WS_OK = False

_CQ_RE = re.compile(r"\[CQ:([a-zA-Z]+)((?:,[^\[\]]*)?)\]")
_YES = ("1", "y", "yes", "是", "允许")
_ALWAYS = ("2", "always", "总是", "总是允许")
_NO = ("3", "n", "no", "否", "拒绝")
_SKIP = ("跳过", "skip", "忽略", "decline")
_TX_CAP = 500  # 发送队列上限：慢消费时丢事件，不拖垮 hook 线程


class _State:
    """单个插件实例的运行态（reload 后随新模块重建，线程经 teardown 停止）"""

    def __init__(self):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.ws = None
        self.connected = False
        self.echo_seq = 0
        self.pending = None         # 待应答交互槽（同刻最多一个）：{kind, event, response, ...}
        self.pending_submits = []   # 防回声：经 QQ 提交的文本（message_added 比对后移除）
        self.bound = ""             # 生效的同步目标 "private/<qq>" / "group/<gid>"
        self.bound_user = ""
        self.self_id = ""           # 机器人 QQ 号（事件帧 / get_login_info 自动获取）
        self.last_error = ""
        self.reconnect_requested = False
        self.clear_confirmation = None
        self.qq_origin = None
        self.tool_call_targets = {}


def setup(ctx: PluginContext):
    st = _State()
    tx: queue.Queue = queue.Queue(maxsize=_TX_CAP)

    # ---- 配置（ctx.settings 为实时视图，处理函数内每次现读）----

    def _cfg(key, default=None):
        return ctx.settings.get(key, default)

    def _cfg_bool(key, default=True):
        return bool(_cfg(key, default))

    def _max_len() -> int:
        try:
            return max(200, min(4000, int(_cfg("max_len") or 2000)))
        except (TypeError, ValueError):
            return 2000

    def _self_id() -> str:
        cfg_id = str(_cfg("self_id") or "").strip()
        if cfg_id:
            return cfg_id
        with st.lock:
            return st.self_id

    def _allowlist() -> set:
        raw = _cfg("allow_users") or ""
        if isinstance(raw, (list, tuple)):
            items = [str(i) for i in raw]
        else:
            items = re.split(r"[,，;；\s]+", str(raw))
        return {i.strip() for i in items if i.strip()}

    def _enabled() -> bool:
        return bool(_cfg("enabled"))

    def _running() -> bool:
        t = st.thread
        return t is not None and t.is_alive() and st.connected

    def _online() -> bool:
        """桥可用（已启用+WS 在线+已绑定目标）——交互代答与发送的前提"""
        with st.lock:
            return _enabled() and _running() and bool(st.bound) and _target_allowed(st.bound)

    def _runner():
        return ctx.runtime.get("runner")

    def _app():
        return ctx.runtime.get("app")

    def _session():
        runner = _runner()
        return getattr(runner, "session", None) if runner is not None else None

    def _refresh_session(session):
        app = _app()
        if app is not None:
            app.refresh_from_session(session, render=False)
            app._tree_follow_tail = True

    # ---- 发送（hook 线程入队，WS 线程消费）----

    def _target_allowed(target: str) -> bool:
        configured = str(_cfg("target") or "").strip()
        with st.lock:
            if configured == target:
                return True
            return bool(st.bound_user and st.bound_user in _allowlist() and st.bound == target)

    def _send(target: str, segments: list, force: bool = False) -> None:
        if not target or not segments or (not force and not _target_allowed(target)):
            return
        try:
            tx.put_nowait({"target": target, "segments": segments, "force": force})
        except queue.Full:
            ctx.log.warn("qbridge 发送队列已满，丢弃消息")

    def _bound_or_none() -> str:
        with st.lock:
            return st.bound

    def _split_text(text: str) -> list:
        cap = _max_len()
        text = str(text or "")
        return [text[i:i + cap] for i in range(0, len(text), cap)] or [""]

    def _send_text(text: str) -> None:
        target = _bound_or_none()
        if not target:
            return
        for chunk in _split_text(text):
            _send(target, [{"type": "text", "data": chunk}])

    def _origin_target(origin: dict) -> str:
        if origin.get("message_type") == "group" and origin.get("group_id") is not None:
            return f"group/{origin['group_id']}"
        return f"private/{origin['user_id']}"

    def _send_reply_to_source(uid: str, message_type: str, group_id, text: str) -> None:
        target = f"group/{group_id}" if message_type == "group" and group_id is not None else f"private/{uid}"
        for chunk in _split_text(text):
            _send(target, [{"type": "text", "data": chunk}], force=True)

    def _tool_send_image(file_path: str, caption: str = "") -> str:
        suffix = Path(file_path).suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            return "图片路径扩展名不受支持"
        return _send_tool_attachment("image", file_path, caption)

    def _tool_send_file(file_path: str, caption: str = "") -> str:
        return _send_tool_attachment("file", file_path, caption)

    def _send_tool_attachment(kind: str, file_path: str, caption: str = "") -> str:
        if not _enabled():
            return "QQ桥当前已停用，不能发送附件。"
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            return f"文件不存在或不是普通文件：{path}"
        with st.lock:
            origin = st.qq_origin
        if not origin:
            return "本回合没有可用的 QQ 发起会话；请从白名单 QQ 消息启动任务后再发送。"
        segment = {"type": kind, "data": str(path)}
        if caption:
            segments = [{"type": "text", "data": caption}, segment]
        else:
            segments = [segment]
        _send(origin["target"], segments, force=True)
        with st.lock:
            st.qq_origin = None
        ctx.notify(f"[QQ桥] Agent 请求发送{('图片' if kind == 'image' else '文件')}到 {origin['target']}：{path.name}")
        return f"已提交发送到 {origin['target']}：{path.name}"

    def _send_to_target(target: str, segments: list) -> None:
        _send(target, segments, force=True)

    def _send_segments(segments: list) -> None:
        target = _bound_or_none()
        if not target:
            return
        _send(target, segments)

    def _oneline(text: str) -> str:
        return " ".join(str(text or "").split())

    # ---- 图片：QQ → 终端（下载落盘），终端 → QQ（data URL → base64://）----

    def _img_dir() -> Path:
        d = ctx.storage_dir() / "qq-images"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _render_tool_image(name: str, args: dict, output: str):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            ctx.log.warn("qbridge 工具改动图片发送需要 Pillow")
            return None
        source = str(args.get("new_str") or args.get("content") or output)
        if name == "edit_file":
            old_lines = [f"- {line}" for line in str(args.get("old_str") or "").splitlines()]
            new_lines = [f"+ {line}" for line in str(args.get("new_str") or "").splitlines()]
            source = "\n".join(old_lines + new_lines)
        lines = (source[:6000].splitlines() or [""])[:140]
        filename = str(args.get("file_path") or "")
        try:
            font = ImageFont.truetype("msyh.ttc", 18)
        except OSError:
            font = ImageFont.load_default()
        padding, line_height, title_height = 24, 24, 48
        width = min(1400, max(720, max((len(line) for line in lines), default=0) * 11 + padding * 2))
        height = title_height + len(lines) * line_height + padding * 2
        image = Image.new("RGB", (width, height), "#171923")
        draw = ImageDraw.Draw(image)
        draw.text((padding, padding), f"{name} · {filename}", fill="#79c0ff", font=font)
        for index, line in enumerate(lines):
            draw.text((padding, title_height + index * line_height), line[:160], fill="#e6edf3", font=font)
        path = _img_dir() / f"tool-preview-{uuid.uuid4().hex[:10]}.png"
        image.save(path, format="PNG")
        return path

    def _fetch_image(ref: str):
        """图片引用 → 本地路径（http(s) 下载；file:// 或本地路径直接使用）"""
        ref = str(ref or "").strip()
        if not ref:
            return None
        low = ref.lower()
        if low.startswith("file://"):
            path = unquote(urlparse(ref).path)
            if re.match(r"^/[A-Za-z]:", path):  # Windows file:///C:/... 还原盘符路径
                path = path[1:]
            return path if Path(path).is_file() else None
        if low.startswith(("http://", "https://")):
            if requests is None:
                return None
            try:
                resp = requests.get(ref, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                ctx.log.warn("qbridge 图片下载失败: %r", e)
                return None
            suffix = Path(urlparse(ref).path).suffix.lower()
            if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                          "image/webp": ".webp", "image/bmp": ".bmp"}.get(ctype, ".png")
            path = _img_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:8]}{suffix}"
            path.write_bytes(resp.content)
            return str(path)
        return ref if Path(ref).is_file() else None

    def _extract_user_content(content):
        """会话用户消息 content → (文本, [data URL 图片])；content 可能为多模态数组"""
        if isinstance(content, str):
            return content.strip(), []
        texts, images = [], []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                elif part.get("type") == "image_url":
                    url = str((part.get("image_url") or {}).get("url") or "")
                    if url.startswith("data:"):
                        images.append(url)
        return "\n".join(t for t in texts if t).strip(), images

    # ---- 终端 → QQ：hook 观察（一律只入队，不阻塞）----

    def _on_after_turn(payload):
        if not _enabled():
            return
        text = str(payload.get("response") or "").strip()
        with st.lock:
            origin = st.qq_origin
            st.qq_origin = None
        if text and origin:
            _send_reply_to_source(origin["user_id"], origin["message_type"], origin.get("group_id"), text)
        elif text:
            _send_text(text)

    def _on_before_tool(payload):
        if not _enabled() or not _cfg_bool("sync_tools", True):
            return
        name = str(payload.get("name") or "?")
        args = payload.get("args") if payload.get("args") is not None else payload.get("arguments") or {}
        try:
            arg_line = _oneline(json.dumps(args, ensure_ascii=False))
        except (TypeError, ValueError):
            arg_line = _oneline(str(args))
        _send_text(f"🔧 {name} {arg_line[:200]}".rstrip())
        if name in ("write", "edit_file"):
            session = _session()
            call_id = ""
            if session is not None:
                for message in reversed(session.messages):
                    if message.get("role") == "assistant" and message.get("tool_calls"):
                        call_id = next((str(call.get("id") or "") for call in message.get("tool_calls", [])
                                        if isinstance(call, dict) and str(call.get("name") or "") == name), "")
                        if call_id:
                            break
            with st.lock:
                if st.qq_origin and call_id:
                    st.tool_call_targets[call_id] = dict(st.qq_origin)

    def _on_after_tool(payload):
        if not _enabled() or not _cfg_bool("sync_tool_images", True):
            return
        name = str(payload.get("name") or "")
        if name not in ("write", "edit_file"):
            return
        call_id = str(payload.get("call_id") or "")
        with st.lock:
            origin = st.tool_call_targets.pop(call_id, None)
        if not origin:
            return
        output = str(payload.get("output") or "")
        if not re.search(r"(?:写入成功|位置写入成功|编辑成功)", output):
            return
        image_path = _render_tool_image(name, payload.get("args") or {}, output)
        if image_path:
            _send_to_target(origin["target"], [
                {"type": "text", "data": f"📝 {name} 改动预览"},
                {"type": "image", "data": str(image_path)},
            ])

    def _on_message_added(payload):
        if not _enabled():
            return
        role = payload.get("role")
        content = payload.get("content")
        if role == "user":
            text, images = _extract_user_content(content)
            with st.lock:
                if text and text in st.pending_submits:
                    st.pending_submits.remove(text)  # QQ 提交的回声，不回发
                    return
            if not text and not images:
                return
            segments = [{"type": "text", "data": ("[终端] " + text).strip()}] if text else []
            for url in images:
                segments.append({"type": "image", "data": "base64://" + url.split("base64,", 1)[-1]})
            _send_segments(segments)
        elif role == "system":
            if not _cfg_bool("sync_system", True):
                return
            text = content.strip() if isinstance(content, str) else _extract_user_content(content)[0]
            if not text or text.startswith("[QQ桥]"):
                return
            _send_text("[系统] " + text)
        # assistant/tool：回复由 after_turn 统一同步，工具输出不同步

    ctx.register_hook("after_turn", _on_after_turn)
    ctx.register_hook("before_tool", _on_before_tool)
    ctx.register_hook("after_tool", _on_after_tool)
    ctx.register_hook("message_added", _on_message_added)

    def _register_attachment_tools():
        ctx.register_tool(
            name="qbridge_send_image",
            description="将本机图片文件发送到本回合发起 QQ 会话。仅在任务由 QQ 消息发起时可用。",
            usage="qbridge_send_image(file_path, caption)",
            schema={"type": "object", "properties": {
                "file_path": {"type": "string", "description": "本机图片路径"},
                "caption": {"type": "string", "description": "可选图片说明"},
            }, "required": ["file_path"]},
        )(_tool_send_image)
        ctx.register_tool(
            name="qbridge_send_file",
            description="将本机文件发送到本回合发起 QQ 会话。文件内容会外发，请仅发送用户要求分享的文件。",
            usage="qbridge_send_file(file_path, caption)",
            schema={"type": "object", "properties": {
                "file_path": {"type": "string", "description": "本机文件路径"},
                "caption": {"type": "string", "description": "可选文件说明"},
            }, "required": ["file_path"]},
        )(_tool_send_file)

    _register_attachment_tools()
    # ---- 交互等待：无 QQ 超时，终端提交新消息即手动接管 ----

    def _release_pending(reason: str) -> None:
        with st.lock:
            item, st.pending = st.pending, None
        if item is not None:
            item["response"] = None
            item["event"].set()
            ctx.log.info("qbridge 待应答已释放: %s", reason)

    def _wait_answer(item):
        """agent 线程轮询等待 QQ 应答（不设超时）；等待期间终端有新提交/中断/桥断开时释放"""
        while not item["event"].wait(0.2):
            if not _online():
                return None
            runner = _runner()
            llm = getattr(runner, "llm", None) if runner is not None else None
            if llm is not None and callable(getattr(llm, "cancelled", None)) and llm.cancelled():
                return None
            queue = getattr(runner, "_queue", None) or []
            if len(queue) > item.get("qbase", 0):  # 仅等待期间的新提交触发手动接管
                ctx.notify("[QQ桥] 终端有新输入，QQ 等待已转回终端面板")
                return None
        return item.get("response")

    def _take_pending(kind: str, request_id=None):
        with st.lock:
            if st.pending is not None or not _online():
                return None
            runner = _runner()
            qbase = len(getattr(runner, "_queue", None) or []) if runner is not None else 0
            item = {"kind": kind, "event": threading.Event(), "response": None, "qbase": qbase,
                    "request_id": request_id}
            st.pending = item
            return item

    def _drop_pending(item) -> None:
        with st.lock:
            if st.pending is item:
                st.pending = None

    # ---- 工具确认代答（tool_confirm 变换链；返回 dict 生效，None 交回终端面板）----

    def _on_tool_confirm(payload):
        if not _cfg_bool("confirm_via_qq", True):
            return None
        name = str(payload.get("name") or "")
        arguments = payload.get("arguments") or {}
        item = _take_pending("confirm", payload.get("request_id"))
        if item is None:
            return None
        try:
            try:
                arg_line = _oneline(json.dumps(arguments, ensure_ascii=False))[:500]
            except (TypeError, ValueError):
                arg_line = _oneline(str(arguments))[:500]
            card = "⚠️ 工具确认请求\n名称: " + name
            if arg_line and arg_line != "{}":
                card += "\n参数: " + arg_line
            card += "\n回复：1=本次允许，2=总是允许，3 拒绝 [原因]"
            _send_text(card)
            ctx.notify(f"[QQ桥] 工具 {name} 已请求 QQ 确认（终端提交新消息可转回终端面板）")
            resp = _wait_answer(item)
        finally:
            _drop_pending(item)
        if isinstance(resp, dict):
            action = str(resp.get("action") or "")
            if action in ("allow_once", "allow_always"):
                ctx.notify(f"[QQ桥] QQ 已{'总是允许' if action == 'allow_always' else '允许（本次）'} {name}")
                return {"action": action}
            if action == "deny":
                ctx.notify(f"[QQ桥] QQ 已拒绝 {name}")
                return {"action": "deny", "reason": str(resp.get("reason") or "")}
        return None

    def _on_tool_confirm_closed(payload):
        request_id = payload.get("request_id")
        with st.lock:
            item = st.pending
            if not request_id or item is None or item.get("kind") != "confirm" or item.get("request_id") != request_id:
                return
            st.pending = None
            item["response"] = None
            item["event"].set()

    ctx.register_hook("tool_confirm", _on_tool_confirm, priority=50)
    ctx.register_hook("tool_confirm_closed", _on_tool_confirm_closed)

    # ---- 选择框/行输入/询问面板代答（ui_request 变换链）----

    def _render_choose_card(options, prompt: str) -> str:
        lines = [str(prompt or "请选择") + "（回复编号或文本）"]
        for opt in options:
            if isinstance(opt, (tuple, list)) and len(opt) >= 2:
                lines.append(f"{opt[0]}. {opt[1]}")
            else:
                lines.append(str(opt))
        return "\n".join(lines)

    def _on_ui_request(payload):
        kind = str(payload.get("kind") or "")
        if kind == "confirm":
            return None  # tool_confirm 扩展点已覆盖，避免双重代答
        if kind not in ("choose", "line", "ask"):
            return None
        item = _take_pending(kind)
        if item is None:
            return None
        if kind == "choose":
            options = list(payload.get("options") or [])
            item["options"] = options
            card = _render_choose_card(options, payload.get("prompt"))
        elif kind == "ask":
            question = str(payload.get("question") or "")
            opts = [str(o.get("title") or "") for o in (payload.get("options") or [])
                    if isinstance(o, dict) and str(o.get("title") or "").strip()]
            item["options"] = opts
            lines = [question or "请回答"]
            for i, title in enumerate(opts, 1):
                lines.append(f"{i}. {title}")
            lines.append("（回复编号/文本，或回复 跳过）")
            card = "\n".join(lines)
        else:
            card = str(payload.get("prompt") or "请输入") + "（回复文本）"
        try:
            _send_text(card)
            ctx.notify(f"[QQ桥] 交互面板已推送 QQ 等待应答（{kind}）")
            resp = _wait_answer(item)
        finally:
            _drop_pending(item)
        if resp is None:
            return None
        if kind == "choose":
            return str(resp)  # 原始行语义：与键盘输入一致，序号匹配由下游完成
        if kind == "line":
            return str(resp)
        # ask：映射为 show_ask_form 的返回结构
        text = str(resp).strip()
        opts = item.get("options") or []
        if text.lower() in _SKIP:
            return {"status": "declined"}
        if text.isdigit() and 1 <= int(text) <= len(opts):
            i = int(text) - 1
            return {"status": "answer", "index": i, "answer": opts[i]}
        return {"status": "answer", "answer": text}

    ctx.register_hook("ui_request", _on_ui_request)

    # ---- QQ 应答解析 ----

    def _answer_pending(item, text: str) -> bool:
        """把 QQ 文本路由给待应答项；返回 True 表示消息已被消费"""
        kind = item.get("kind")
        t = text.strip()
        if kind == "confirm":
            head, _, rest = t.partition(" ")
            low = head.lower()
            if low in _ALWAYS or t.lower() in _ALWAYS:
                item["response"] = {"action": "allow_always"}
            elif low in _YES or t.lower() in _YES:
                item["response"] = {"action": "allow_once"}
            elif low in _NO:
                item["response"] = {"action": "deny", "reason": rest.strip()}
            else:
                _send_text("未识别的回复。请回复：1=本次允许，2=总是允许，3 拒绝 [原因]")
                return True  # 未识别：继续等待
            item["event"].set()
            return True
        if kind in ("choose", "line"):
            if not t:
                _send_text("请回复文本内容")
                return True
            item["response"] = t
            item["event"].set()
            return True
        if kind == "ask":
            if not t:
                _send_text("请回复编号或文本")
                return True
            item["response"] = t
            item["event"].set()
            return True
        return False

    # ---- QQ → 终端 ----

    def _parse_incoming(ev):
        """OneBot 消息事件 → (文本, [图片引用])；优先 message 数组，退回 CQ 码解析"""
        segs = ev.get("message")
        texts, images = [], []
        if isinstance(segs, list):
            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                stype = str(seg.get("type") or "")
                data = seg.get("data") or {}
                if stype == "text":
                    texts.append(str(data.get("text") or ""))
                elif stype == "image":
                    images.append(str(data.get("url") or data.get("file") or ""))
                elif stype == "at":
                    qq = str(data.get("qq") or "")
                    texts.append("@全体成员" if qq == "all" else f"@{qq}")
                elif stype == "face":
                    texts.append("[表情]")
                elif stype == "record":
                    texts.append("[语音]")
                elif stype == "video":
                    texts.append("[视频]")
        else:
            raw = str(ev.get("raw_message") or segs or "")
            pos = 0
            for m in _CQ_RE.finditer(raw):
                texts.append(raw[pos:m.start()])
                pos = m.end()
                ctype = m.group(1).lower()
                params = {}
                for pair in (m.group(2) or "").split(","):
                    k, _, v = pair.partition("=")
                    if k.strip():
                        params[k.strip()] = v.strip()
                if ctype == "image":
                    images.append(params.get("url") or params.get("file") or "")
                elif ctype == "at":
                    texts.append(f"@{params.get('qq', '')}")
                elif ctype == "face":
                    texts.append("[表情]")
                elif ctype:
                    texts.append(f"[{ctype}]")
            texts.append(raw[pos:])
        return "".join(texts).strip(), [i for i in images if i]

    def _submit_to_terminal(text: str, image_paths: list) -> None:
        if not text and not image_paths:
            return
        if not text:
            text = "[图片]"
        with st.lock:
            st.pending_submits.append(text)
            if len(st.pending_submits) > 50:  # 回合被丢弃时条目无处比对，防无限累积
                del st.pending_submits[:len(st.pending_submits) - 50]
        result = ctx.submit_turn(text, images=image_paths)
        if "未提交" in result or "未就绪" in result:
            with st.lock:
                try:
                    st.pending_submits.remove(text)
                except ValueError:
                    pass
            _send_text(result)

    def _handle_qq_command(text: str, uid: str, message_type: str, group_id=None) -> bool:
        command = text.strip()
        if not command.startswith("/") and command not in ("帮助", "状态", "解绑", "重连") and not command.startswith("绑定 "):
            return False
        if command.startswith("/"):
            parts = command.split(maxsplit=1)
            op = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
        else:
            op = {"帮助": "/help", "状态": "/status", "解绑": "/unbind", "重连": "/reconnect"}.get(command, "/bind" if command.startswith("绑定 ") else "")
            arg = command[3:].strip() if command.startswith("绑定 ") else ""
        if op in ("/help", "/qbridge"):
            reply = "QQ桥指令：/help /status /bind private/<id> /bind group/<id> /unbind /reconnect /new /rename <标题> /sessions /resume <id> /clear /clear confirm\n图片/文件：qbridge_send_image、qbridge_send_file（由 Agent 在任务中调用）"
        elif op in ("/status",):
            reply = _status_line()
        elif op in ("/unbind",):
            if str(_cfg("target") or "").strip():
                reply = "当前同步目标由配置项 target 固定，请先在终端清空配置后再解绑。"
            else:
                with st.lock:
                    st.bound = ""
                    st.bound_user = ""
                reply = "已解除同步目标绑定。"
        elif op in ("/bind",):
            target = arg
            if not re.fullmatch(r"(private|group)/\d+", target):
                reply = "格式错误。用法：/bind private/<QQ号> 或 /bind group/<群号>"
            elif str(_cfg("target") or "").strip():
                reply = "当前同步目标由配置项 target 固定，请在终端修改配置后再绑定。"
            else:
                with st.lock:
                    st.bound = target
                    st.bound_user = uid
                reply = f"已绑定同步目标 {target}"
        elif op == "/reconnect":
            with st.lock:
                ws = st.ws
                running = st.thread is not None and st.thread.is_alive()
                st.reconnect_requested = running
            reply = "已请求重连。" if running and ws is not None else "桥接当前未运行或尚未连接，无法重连。"
        elif op in ("/new", "/rename", "/resume", "/sessions", "/clear"):
            reply = _session_command(op, arg, uid)
        else:
            reply = "未知指令。发送 /help 查看可用 QQ 桥指令。"
            if not command.startswith("/"):
                return False
        _send_reply_to_source(uid, message_type, group_id, reply)
        ctx.notify(f"[QQ桥] QQ 用户 {uid} 执行“{command}”：{reply.splitlines()[0]}")
        return True

    def _session_command(op: str, arg: str, uid: str) -> str:
        runner = _runner()
        session = getattr(runner, "session", None) if runner is not None else None
        if runner is None or session is None:
            return "会话运行时尚未就绪。"
        if getattr(runner, "busy", False):
            return "当前回合进行中，请停止或等待回合结束后再操作会话。"
        if op == "/clear":
            with st.lock:
                if arg.lower() != "confirm":
                    st.clear_confirmation = (uid, session.session_id, time.monotonic() + 60)
                    return f"将清空会话 {session.session_id} 的内容及相关记录。请在 60 秒内发送 /clear confirm。"
                confirmation = st.clear_confirmation
                st.clear_confirmation = None
            if not confirmation or confirmation[0] != uid or confirmation[1] != session.session_id or confirmation[2] < time.monotonic():
                return "清空确认无效或已过期，请先发送 /clear。"
            session.clear()
            app = _app()
            if app is not None:
                app.task = ""
                app.status = "会话已清空"
            _refresh_session(session)
            return f"会话 {session.session_id} 已清空。"
        if op == "/new":
            session.save_session()
            session.start_new_session()
            app = _app()
            if app is not None:
                app.task = ""
                app.status = "新会话已开启"
            _refresh_session(session)
            return f"新会话已开启：{session.session_id}"
        if op == "/rename":
            if not arg:
                return "用法：/rename <标题>"
            title = session.rename_session(arg)
            session.save_session()
            _refresh_session(session)
            return f"当前会话已重命名：{title}"
        if ctx.host.sessions is None:
            return "宿主缺少会话存储模块。"
        directory = ctx.host.sessions.sessions_dir(ctx.config, session.project_id)
        if op == "/sessions":
            rows = ctx.host.sessions.list_sessions(directory)[:20]
            if not rows:
                return "当前没有已保存的历史会话。"
            return "最近历史会话：\\n" + "\\n".join(
                f"{row['id']} · {row['title']} · {row['message_count']} 条" for row in rows
            )
        if op == "/resume":
            if not arg:
                return "用法：/resume <会话ID>（先用 /sessions 查看列表）"
            data = ctx.host.sessions.load_session_data(directory, arg)
            if not data:
                return f"找不到可加载的会话：{arg}"
            session.save_session()
            session.switch_to(data)
            app = _app()
            if app is not None:
                app._tree_follow_tail = True
                app.status = f"已切换：{session.session_title or session.session_id}"
            _refresh_session(session)
            return f"已切换会话：{session.session_title or session.session_id}"
        return "未知会话指令。"

    def _route_qq_message(ev):
        if not _enabled():
            return
        uid = str(ev.get("user_id") or "")
        if not uid or uid == _self_id():
            return  # 机器人自身（含其他端）消息回声
        if uid not in _allowlist():
            return
        if ev.get("message_type") == "group":
            key = f"group/{ev.get('group_id')}"
        else:
            key = f"private/{uid}"
        configured = str(_cfg("target") or "").strip()
        auto_bound = False
        with st.lock:
            if configured:
                st.bound = configured
                matched = key == configured
            elif st.bound:
                matched = key == st.bound
            else:
                st.bound = key
                st.bound_user = uid
                matched = True
                auto_bound = True
        if auto_bound:
            ctx.notify(f"[QQ桥] 已绑定同步会话 {key}（/qbridge bind <private|group>/<id> 可更改）")
        elif not matched:
            return
        if uid not in _allowlist():
            ctx.log.debug("qbridge 白名单外消息已忽略: user=%s", uid)
            return
        text, images = _parse_incoming(ev)
        if text.startswith("/"):
            if _handle_qq_command(text, uid, ev.get("message_type"), ev.get("group_id")):
                return
        elif not images and _handle_qq_command(text, uid, ev.get("message_type"), ev.get("group_id")):
            return
        with st.lock:
            item = st.pending
        if item is not None and text and _answer_pending(item, text):
            return
        paths = []
        for ref in images:
            p = _fetch_image(ref)
            if p:
                paths.append(p)
        runner = _runner()
        busy_origin = runner is not None and getattr(runner, "busy", False)
        if not busy_origin:
            with st.lock:
                st.qq_origin = {
                    "user_id": uid,
                    "message_type": ev.get("message_type"),
                    "group_id": ev.get("group_id"),
                    "target": key,
                }
        else:
            ctx.notify(f"[QQ桥] 收到 {uid} 的后续输入，排队/插话消息不会切换附件发送目标。")
        _submit_to_terminal(text, paths)

    # ---- WS 线程 ----

    def _api_call(ws, action: str, params: dict, echo: str = "") -> None:
        if not echo:
            with st.lock:
                st.echo_seq += 1
                echo = f"qb-{st.echo_seq}"
        ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))

    def _drain_tx(ws) -> None:
        while True:
            try:
                job = tx.get_nowait()
            except queue.Empty:
                return
            target = job["target"]
            if not job.get("force") and not _target_allowed(target):
                continue
            kind, _, val = target.partition("/")
            ident = int(val) if val.isdigit() else val
            if kind == "group":
                action, pid = "send_group_msg", {"group_id": ident}
            else:
                action, pid = "send_private_msg", {"user_id": ident}
            message = []
            for seg in job["segments"]:
                if seg.get("type") in ("image", "file"):
                    file_value = str(seg.get("data") or "")
                    seg_type = seg["type"]
                    if not file_value.lower().startswith(("http://", "https://", "base64://")):
                        if not Path(file_value).is_file():
                            ctx.log.warn("qbridge 附件不存在，跳过发送: %s", file_value)
                            continue
                        file_path = Path(file_value).resolve()
                        if seg_type == "image":
                            image_bytes = file_path.read_bytes()
                            if len(image_bytes) > 8 * 1024 * 1024:
                                ctx.log.warn("qbridge 图片超过 OneBot 直接发送限制，跳过: %s", file_path)
                                continue
                            file_value = "base64://" + base64.b64encode(image_bytes).decode("ascii")
                        else:
                            file_value = str(file_path)
                    message.append({"type": seg_type, "data": {"file": file_value}})
                else:
                    message.append({"type": "text", "data": {"text": str(seg.get("data") or "")}})
            try:
                _api_call(ws, action, {**pid, "message": message})
            except Exception as e:
                ctx.log.warn("qbridge 发送失败: %r", e)

    def _handle_frame(ws, raw) -> None:
        data = json.loads(raw)
        if data.get("post_type"):
            sid = data.get("self_id")
            if sid and not str(_cfg("self_id") or "").strip():
                with st.lock:
                    st.self_id = str(sid)
            if data.get("post_type") == "message":
                try:
                    _route_qq_message(data)
                except Exception as e:
                    ctx.log.warn("qbridge 消息处理异常: %r", e)
            return
        if data.get("echo"):
            if data.get("echo") == "qb-login" and isinstance(data.get("data"), dict):
                uid = data["data"].get("user_id")
                if uid:
                    with st.lock:
                        st.self_id = str(uid)
                    ctx.log.info("qbridge 机器人账号: %s", uid)
                return
            retcode = data.get("retcode")
            if retcode not in (0, 1) and data.get("status") != "async":
                ctx.log.warn("qbridge API 失败 echo=%s retcode=%s data=%s",
                             data.get("echo"), retcode, str(data.get("data"))[:200])

    ctx._qbridge_handle_frame = _handle_frame

    def _ws_loop():
        while not st.stop.is_set():
            url = str(_cfg("ws_url") or "").strip()
            if not url:
                st.last_error = "未配置 ws_url"
                ctx.log.warn("qbridge 未配置 ws_url，10s 后重试")
                st.stop.wait(10)
                continue
            headers = {}
            token = str(_cfg("access_token") or "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                ws = _ws_connect(url, additional_headers=headers or None, open_timeout=5,
                                 max_size=20 * 1024 * 1024)
            except Exception as e:
                st.last_error = str(e) or e.__class__.__name__
                ctx.log.warn("qbridge 连接失败: %r（5s 后重试）", e)
                st.stop.wait(5)
                continue
            st.last_error = ""
            with st.lock:
                st.ws = ws
                st.connected = True
                configured = str(_cfg("target") or "").strip()
                if configured:
                    st.bound = configured  # 配置了目标即生效，无需等首个消息
            ctx.log.info("qbridge 已连接 %s", url)
            try:
                _api_call(ws, "get_login_info", {}, echo="qb-login")
                while not st.stop.is_set():
                    with st.lock:
                        reconnect_requested = st.reconnect_requested
                        st.reconnect_requested = False
                    if reconnect_requested:
                        break
                    try:
                        raw = ws.recv(timeout=0.2)
                    except TimeoutError:
                        raw = None
                    if raw:
                        try:
                            _handle_frame(ws, raw)
                        except (json.JSONDecodeError, ValueError) as e:
                            ctx.log.warn("qbridge 非法帧: %r", e)
                    _drain_tx(ws)
            except Exception as e:
                st.last_error = str(e) or e.__class__.__name__
                ctx.log.warn("qbridge 连接中断: %r（5s 后重连）", e)
            finally:
                with st.lock:
                    st.connected = False
                    st.ws = None
                _release_pending("连接断开")
                try:
                    ws.close()
                except Exception:
                    pass
            if st.stop.is_set():
                break
            with st.lock:
                reconnect_requested = st.reconnect_requested
                st.reconnect_requested = False
            st.stop.wait(0 if reconnect_requested else 5)

    def _start() -> str:
        if not _WS_OK:
            return "缺少依赖 websockets（pip install websockets>=13），无法启动"
        if st.thread is not None and st.thread.is_alive():
            return "qbridge 已在运行"
        st.stop.clear()
        st.thread = threading.Thread(target=_ws_loop, daemon=True, name="qbridge-ws")
        st.thread.start()
        return f"qbridge 已启动，正在连接 {_cfg('ws_url')}"

    def _stop() -> str:
        thread = st.thread
        if thread is None or not thread.is_alive():
            st.thread = None
            return "qbridge 未在运行"
        st.stop.set()
        _release_pending("桥已关闭")
        ws = st.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread.join(timeout=3)
        st.thread = None
        return "qbridge 已停止（QQ 同步关闭，待应答已转回终端）"

    ctx.register_teardown(_stop)

    # ---- 斜杠命令 ----

    def _status_line() -> str:
        if not _WS_OK:
            return "qbridge：缺少依赖 websockets（pip install websockets>=13）"
        with st.lock:
            pending = st.pending.get("kind") if st.pending else ""
        state = "已连接" if _running() else ("运行中(未连接)" if st.thread is not None and st.thread.is_alive() else "已停止")
        allow = ", ".join(sorted(_allowlist())) or "（空=只读镜像）"
        lines = [
            f"qbridge：{state} · WS={_cfg('ws_url')} · 机器人={_self_id() or '未获取'}",
            f"同步目标: {_bound_or_none() or '未绑定（等待首个消息来源）'} · 白名单: {allow}",
            f"待应答: {pending or '无'}" + (f" · 最近错误: {st.last_error}" if st.last_error else ""),
        ]
        return "\n".join(lines)

    def _cmd(cctx, args):
        arg = (args or "").strip()
        low = arg.lower()
        if low == "on":
            if not _WS_OK:
                result = "缺少依赖 websockets（pip install websockets>=13）"
                ctx.notify(f"[QQ桥] /qbridge on 失败：{result}")
                return result
            if ctx.host.plugins is not None:
                ctx.set_setting("enabled", True)
            result = _start()
            ctx.notify(f"[QQ桥] /qbridge on：{result}")
            return result
        if low == "off":
            if ctx.host.plugins is not None:
                ctx.set_setting("enabled", False)
            result = _stop()
            ctx.notify(f"[QQ桥] /qbridge off：{result}")
            return result
        if low.startswith("bind"):
            val = arg[4:].strip()
            if not re.fullmatch(r"(private|group)/\d+", val):
                result = "目标格式: private/<QQ号> 或 group/<群号>"
                ctx.notify(f"[QQ桥] /qbridge bind 失败：{result}")
                return result
            if ctx.host.plugins is not None:
                ctx.set_setting("target", val)
            with st.lock:
                st.bound = val
                st.bound_user = ""
            result = f"qbridge 已绑定 {val}"
            ctx.notify(f"[QQ桥] /qbridge bind：{result}")
            return result
        result = _status_line()
        if low in ("", "status"):
            ctx.notify(f"[QQ桥] 查询状态：{result.splitlines()[0]}")
        return result

    def _complete(config, arg):
        items = [
            {"name": "/qbridge on", "hint": "启动桥接", "source": "arg", "callable": True},
            {"name": "/qbridge off", "hint": "停止桥接", "source": "arg", "callable": True},
            {"name": "/qbridge status", "hint": "查看状态", "source": "arg", "callable": True},
            {"name": "/qbridge bind private/", "hint": "绑定私聊同步目标", "source": "arg", "callable": True},
            {"name": "/qbridge bind group/", "hint": "绑定群聊同步目标", "source": "arg", "callable": True},
        ]
        return [i for i in items if i["name"].lower().startswith(f"/qbridge {(arg or '')}".strip().lower())] or items

    ctx.register_command(
        "/qbridge",
        hint="QQ 桥接：状态 / 启停 / 绑定同步目标",
        usage="/qbridge [on|off|status|bind <private|group>/<id>]",
        handler=_cmd,
        completer=_complete,
    )

    # ---- 启动 ----

    if _enabled() and _WS_OK:
        _start()
    elif _enabled() and not _WS_OK:
        ctx.log.warn("qbridge 已启用但缺少 websockets 依赖，未启动（pip install websockets>=13）")
