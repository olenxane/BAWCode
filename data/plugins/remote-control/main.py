"""remote-control —— 浏览器远程查看与控制 BAWCode 终端。

装载时（或 /plugin reload）在后台线程启动一个 HTTP 服务：
  - 启动时生成七天有效期的唯一密钥，持久化在 ctx.storage_dir()/key.json
    （未过期跨重启复用，链接稳定；过期或 /remote revoke 时重新生成）
  - 链接形如 http://<局域网IP>:<端口>/?key=<密钥>，session_start 时经 notify
    展示一次；/remote 随时可查
浏览器打开链接后进入远程控制页（web/index.html，SSE 实时事件流 + 快照拉取）：
  - 实时观看：流式输出增量（含思考链）、工具活动、计划/步骤、回合状态
  - 远程控制：代发消息（ctx.submit_turn，busy 时按设置排队/中断）、停止按钮、
    工具确认远程代答（tool_confirm 扩展点：有浏览器在线时把确认请求推给浏览器，
    confirm_timeout 内未响应则交回终端面板）
  - 会话管理：历史会话列表/切换/删除、新建/清空/重命名（busy 时拒绝，
    与主循环 /new /clear /resume 同规则）
  - 运行控制：切换模型、访问模式（auto/manual/full）、工作流、回合快照回滚
    （/undo）、清空回收站、查余额
  - 信息面板：计划与步骤、工具/子代理/技能/MCP/插件清单
  - 远程斜杠命令：/api/command 执行非交互命令（/settings /resume /refine /run
    /exit 等需终端交互或同步跑回合的命令被拦截；输出取命令写入会话的反馈文本）
安全模型：面向受信局域网（无 TLS，密钥经 URL 携带）。所有端点校验密钥
（hmac.compare_digest）；/remote revoke 重置密钥并踢掉已在线浏览器。

私有配置（config.plugins_config["remote-control"]，均可缺省）：
  {"enabled": true, "port": 8399, "bind": "0.0.0.0",
   "remote_confirm": true, "confirm_timeout": 90, "max_history": 80}

依赖的宿主扩展（若宿主较旧则相应能力降级，插件仍可装载）：
  hooks 事件 stream_delta / turn_status（collect）；
  plugins.runtime() 只读运行时载体；ctx.register_teardown 卸载回调。
"""
import hmac
import json
import queue
import secrets
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from core import memory as memory_mod
    from core import plugins as plugins_mod
except ImportError:  # 插件目录被单独导入（类型检查等场景）时的防御
    memory_mod = None
    plugins_mod = None

try:  # 远程命令/信息面板依赖的宿主模块（缺失时对应端点降级提示）
    from core import commands as commands_mod
    from core import mcp as mcp_mod
    from core import policy as policy_mod
    from core import register as register_mod
    from core import session_store as session_store_mod
    from core import skills as skills_mod
    from core import snapshot as snapshot_mod
    from core import subagent as subagent_mod
    from core import workflow as workflow_mod
except ImportError:
    commands_mod = None
    mcp_mod = None
    policy_mod = None
    register_mod = None
    session_store_mod = None
    skills_mod = None
    snapshot_mod = None
    subagent_mod = None
    workflow_mod = None

KEY_VALID_DAYS = 7
SSE_HEARTBEAT = 10  # 秒；顺带用于探测死连接与退出检查


class _State:
    """单个插件实例的运行态（reload 后随新模块重建，线程经 teardown 停止）"""

    def __init__(self):
        self.lock = threading.RLock()
        self.clients = set()      # {{"q": Queue, "epoch": int}}
        self.confirms = []        # 待远程确认：{id, name, arguments, event, response}
        self.busy = False         # hook 观察的回合忙标志（与 runner.busy 合并取真）
        self.status = ""
        self.phase = ""
        self.link_shown = False
        self.key_data = {}        # {"key", "created_at", "expires_at"}
        self.key_epoch = 0        # revoke 时 +1，踢掉旧密钥建立的 SSE 连接
        self.shutdown_flag = threading.Event()
        self.httpd = None
        self.thread = None


def setup(ctx):
    st = _State()

    # ---- 配置 ----
    port = int(ctx.settings.get("port") or 8399)
    bind = str(ctx.settings.get("bind") or "0.0.0.0")
    remote_confirm = bool(ctx.settings.get("remote_confirm", True))
    confirm_timeout = float(ctx.settings.get("confirm_timeout") or 90)
    max_history = int(ctx.settings.get("max_history") or 80)
    page_path = Path(ctx.dir) / "web" / "index.html"

    # ---- 密钥 ----

    def _load_or_create_key(revoke: bool = False) -> dict:
        path = ctx.storage_dir() / "key.json"
        now = datetime.now()
        if not revoke:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                expires = datetime.fromisoformat(data["expires_at"])
                if data.get("key") and expires > now:
                    return data
            except Exception:
                pass
        data = {
            "key": secrets.token_urlsafe(24),
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(days=KEY_VALID_DAYS)).isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.log.info("远程控制密钥已生成，有效期至 %s", data["expires_at"])
        return data

    st.key_data = _load_or_create_key()

    def _lan_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("223.5.5.5", 80))  # 不实际发包，仅取默认路由出口 IP
                ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"

    def _link() -> str:
        return f"http://{_lan_ip()}:{port}/?key={st.key_data.get('key', '')}"

    def _status_line() -> str:
        exp = str(st.key_data.get("expires_at", "")).replace("T", " ")
        if st.httpd is None:
            return f"远程控制服务未运行（/remote on 启动）\n密钥有效期至 {exp}"
        return (
            f"远程控制链接: {_link()}\n"
            f"服务 {bind}:{port} · 浏览器在线 {len(st.clients)} · 密钥有效期至 {exp}\n"
            "（同一局域网的手机/电脑浏览器打开即可远程查看与控制）"
        )

    # ---- 事件广播（SSE）----

    def _broadcast(event: dict) -> None:
        raw = json.dumps(event, ensure_ascii=False)
        with st.lock:
            targets = list(st.clients)
        for client_q, _epoch in targets:
            try:
                client_q.put_nowait(raw)
            except queue.Full:
                pass  # 慢客户端丢事件，快照接口兜底

    def _trunc_args(args, cap: int = 300) -> dict:
        out = {}
        for k, v in (args or {}).items():
            s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            s = str(s)
            out[str(k)] = s[:cap] + ("…（已截断）" if len(s) > cap else "")
        return out

    # ---- hook 订阅（观察链一律返回 None，不参与改写）----

    def _on_before_turn(payload):
        st.busy = True
        text = str(payload.get("user_text") or "")
        _broadcast({"t": "turn_start", "user_text": text[:4000]})

    def _on_after_turn(payload):
        st.busy = False
        _broadcast({"t": "turn_end"})

    def _on_stream_delta(payload):
        piece = payload.get("piece")
        if piece:
            _broadcast({"t": "delta", "kind": payload.get("kind") or "content", "piece": piece})

    def _on_turn_status(payload):
        if "status" in payload:
            st.status = payload.get("status") or ""
        if "phase" in payload:
            st.phase = payload.get("phase") or ""
        _broadcast({"t": "status", "status": st.status, "phase": st.phase})

    def _on_before_tool(payload):
        _broadcast({"t": "tool_before", "name": payload.get("name"),
                    "arguments": _trunc_args(payload.get("args"))})

    def _on_after_tool(payload):
        _broadcast({"t": "tool_after", "name": payload.get("name"),
                    "arguments": _trunc_args(payload.get("args")),
                    "output": str(payload.get("output") or "")[:2000],
                    "elapsed": payload.get("elapsed")})

    def _on_step_update(payload):
        step = payload.get("step") or {}
        _broadcast({"t": "step",
                    "step": {"content": str(step.get("content") or step.get("title") or "")[:200],
                             "status": str(step.get("status") or "")}})

    def _on_session_start(payload):
        _broadcast({"t": "reset"})
        if not st.link_shown:
            st.link_shown = True
            exp = str(st.key_data.get("expires_at", "")).replace("T", " ")
            ctx.notify(
                f"[远程控制] 浏览器打开以下链接可远程查看与控制本终端:\n{_link()}\n"
                f"密钥有效期至 {exp} · 详情见 /remote"
            )

    ctx.register_hook("before_turn", _on_before_turn)
    ctx.register_hook("after_turn", _on_after_turn)
    ctx.register_hook("stream_delta", _on_stream_delta)
    ctx.register_hook("turn_status", _on_turn_status)
    ctx.register_hook("before_tool", _on_before_tool)
    ctx.register_hook("after_tool", _on_after_tool)
    ctx.register_hook("step_update", _on_step_update)
    ctx.register_hook("session_start", _on_session_start)

    # ---- 工具确认远程代答（tool_confirm 变换链：返回 dict 生效，None 交回终端面板）----

    def _on_tool_confirm(payload):
        if not remote_confirm:
            return None
        with st.lock:
            online = bool(st.clients)
        if not online:
            return None  # 无浏览器在线：立即交回 TUI 确认面板，不空等
        name = str(payload.get("name") or "")
        arguments = payload.get("arguments") or {}
        item = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "arguments": arguments,
            "event": threading.Event(),
            "response": None,
        }
        with st.lock:
            st.confirms.append(item)
        _broadcast({"t": "confirm", "id": item["id"], "name": name,
                    "arguments": _trunc_args(arguments, 500)})
        ctx.notify(f"[远程控制] 工具 {name} 等待远程确认（{confirm_timeout:.0f}s 内未响应将转回终端面板）")
        try:
            answered = item["event"].wait(timeout=confirm_timeout)
        finally:
            with st.lock:
                try:
                    st.confirms.remove(item)
                except ValueError:
                    pass
        resp = item["response"]
        if answered and isinstance(resp, dict):
            action = str(resp.get("action") or "").lower()
            if action in ("allow_once", "allow_always"):
                ctx.notify(f"[远程控制] 远程已允许 {name}")
                return {"action": action}
            if action in ("deny", "reject"):
                ctx.notify(f"[远程控制] 远程已拒绝 {name}")
                return {"action": "deny", "reason": str(resp.get("reason") or "")}
        _broadcast({"t": "confirm_done", "id": item["id"], "action": "timeout"})
        return None

    ctx.register_hook("tool_confirm", _on_tool_confirm, priority=50)

    # ---- 宿主运行时访问（缺载体/缺属性时降级为 None，端点各自兜底）----

    def _session():
        return memory_mod.get_session() if memory_mod else None

    def _runner():
        return plugins_mod.runtime().get("runner") if plugins_mod else None

    def _app():
        return plugins_mod.runtime().get("app") if plugins_mod else None

    def _llm():
        runner = _runner()
        return getattr(runner, "llm", None) if runner is not None else None

    def _busy() -> bool:
        runner = _runner()
        return bool(st.busy or getattr(runner, "busy", False))

    def _app_refresh(session) -> None:
        """会话状态变更后刷新 UI 缓存（只改不渲染，帧循环统一上屏）"""
        app = _app()
        if app is None or session is None:
            return
        try:
            app.refresh_from_session(session, render=False)
        except Exception:
            pass

    def _tokens_info() -> dict:
        meter = getattr(_llm(), "meter", None)
        if meter is None:
            return {}
        out = {
            "context_tokens": int(getattr(meter, "context_tokens", 0) or 0),
            "context_window": int(getattr(meter, "context_window", 0) or 0),
            "billed_tokens": int(getattr(meter, "billed_tokens", 0) or 0),
        }
        if getattr(meter, "balance", None) is not None:
            out["balance"] = meter.balance
            out["balance_currency"] = getattr(meter, "balance_currency", "") or ""
        return out

    # ---- 快照 ----

    def _snapshot() -> dict:
        messages = []
        session = _session()
        if session is not None:
            for m in list(session.messages)[-max_history:]:
                entry = {"role": m.get("role"), "content": str(m.get("content") or "")[:2000]}
                if m.get("tool_name"):
                    entry["tool_name"] = str(m.get("tool_name"))
                if m.get("type"):
                    entry["type"] = str(m.get("type"))
                if m.get("description"):
                    entry["description"] = str(m.get("description"))[:200]
                thinking = m.get("thinking")
                if thinking and not m.get("thinking_stripped"):
                    entry["thinking"] = str(thinking)[:2000]
                messages.append(entry)
        runner = _runner()
        app = _app()
        streaming = None
        sm = getattr(app, "streaming_msg", None) if app is not None else None
        if sm:
            streaming = {"content": str(sm.get("content") or ""), "thinking": str(sm.get("thinking") or "")}
        queue_len = len(getattr(runner, "_queue", None) or [])
        mode = str(getattr(app, "mode", "") or getattr(ctx.config, "mode", "") or "auto")
        info = {}
        if session is not None:
            info = {
                "session": {
                    "id": session.session_id,
                    "title": session.session_title,
                    "created_at": session.session_created_at,
                    "message_count": len(session.messages),
                    "project_id": session.project_id,
                },
                "plan": dict(session.plan or {}),
                "steps": [
                    {"id": s.get("id"), "title": str(s.get("title") or "")[:120],
                     "status": str(s.get("status") or ""), "detail": str(s.get("detail") or "")[:200]}
                    for s in list(session.steps or [])
                ],
            }
        try:
            info["model"] = ctx.config.active_model_name()
        except Exception:
            info["model"] = ""
        info["mode"] = mode
        if policy_mod is not None:
            info["mode_label"] = policy_mod.MODE_LABELS.get(mode, mode)
        if workflow_mod is not None:
            try:
                info["workflow"] = workflow_mod.active_name(ctx.config)
            except Exception:
                pass
        info["tokens"] = _tokens_info()
        with st.lock:
            pending = [{"id": c["id"], "name": c["name"], "arguments": _trunc_args(c["arguments"], 500)}
                       for c in list(st.confirms)]
            clients_n = len(st.clients)
        return {
            "ok": True,
            "busy": _busy(),
            "queue": queue_len,
            "status": st.status,
            "phase": st.phase,
            "streaming": streaming,
            "messages": messages,
            "pending": pending,
            "clients": clients_n,
            "expires_at": st.key_data.get("expires_at", ""),
            **info,
        }

    # ---- 远程操作：会话管理 / 运行控制 / 信息面板 / 斜杠命令 ----

    def _require_session():
        session = _session()
        if session is None:
            return None, {"ok": False, "message": "会话未就绪（宿主仍在启动）"}
        return session, None

    def _session_op_guard():
        """会话结构操作在回合进行中拒绝（与主循环 /new /clear /resume /undo 同规则）"""
        if _busy():
            return {"ok": False, "message": "回合进行中：请先停止，或发送新消息按设置中断/排队后再操作"}
        return None

    def _op_session_new() -> dict:
        err = _session_op_guard()
        if err:
            return err
        session, err = _require_session()
        if err:
            return err
        session.save_session()
        session.start_new_session()
        app = _app()
        if app is not None:
            try:
                app.task = ""
                app.status = "新会话已开启"
            except Exception:
                pass
        _app_refresh(session)
        _broadcast({"t": "reset"})
        return {"ok": True, "result": "新会话已开启"}

    def _op_session_clear() -> dict:
        err = _session_op_guard()
        if err:
            return err
        session, err = _require_session()
        if err:
            return err
        session.clear()
        app = _app()
        if app is not None:
            try:
                app.task = ""
                app.status = "会话已清空"
            except Exception:
                pass
        _app_refresh(session)
        _broadcast({"t": "reset"})
        return {"ok": True, "result": "会话已清空"}

    def _op_session_rename(title: str) -> dict:
        session, err = _require_session()
        if err:
            return err
        title = (title or "").strip()
        if not title:
            return {"ok": False, "message": "标题不能为空"}
        name = session.rename_session(title)
        session.save_session()
        _app_refresh(session)
        _broadcast({"t": "reset"})
        return {"ok": True, "result": f"已重命名: {name}", "title": name}

    def _op_session_switch(sid: str) -> dict:
        err = _session_op_guard()
        if err:
            return err
        session, err = _require_session()
        if err:
            return err
        if session_store_mod is None:
            return {"ok": False, "message": "宿主缺少 session_store 模块"}
        sid = (sid or "").strip()
        if not sid:
            return {"ok": False, "message": "缺少会话 id"}
        if sid == session.session_id:
            return {"ok": True, "result": "已是当前会话"}
        directory = session_store_mod.sessions_dir(ctx.config, session.project_id)
        data = session_store_mod.load_session_data(directory, sid)
        if not data:
            return {"ok": False, "message": f"会话加载失败: {sid}"}
        session.save_session()
        session.switch_to(data)
        app = _app()
        if app is not None:
            try:
                app._tree_follow_tail = True  # 切换后贴底显示恢复的消息
                app.status = f"已切换: {session.session_title or session.session_id}"
            except Exception:
                pass
        _app_refresh(session)
        _broadcast({"t": "reset"})
        return {"ok": True, "result": f"已切换: {session.session_title or session.session_id}"}

    def _op_session_delete(sid: str) -> dict:
        session, err = _require_session()
        if err:
            return err
        if session_store_mod is None:
            return {"ok": False, "message": "宿主缺少 session_store 模块"}
        sid = (sid or "").strip()
        if sid == session.session_id:
            return {"ok": False, "message": "不能删除当前会话（可用清空/新建）"}
        directory = session_store_mod.sessions_dir(ctx.config, session.project_id)
        if session_store_mod.delete_session_data(directory, sid):
            return {"ok": True, "result": f"会话已删除: {sid}"}
        return {"ok": False, "message": f"会话不存在: {sid}"}

    def _sessions_listing() -> dict:
        session, err = _require_session()
        if err:
            return err
        if session_store_mod is None:
            return {"ok": False, "message": "宿主缺少 session_store 模块"}
        directory = session_store_mod.sessions_dir(ctx.config, session.project_id)
        items = []
        for row in session_store_mod.list_sessions(directory):
            items.append({
                "id": row.get("id"),
                "title": row.get("title") or "（无标题会话）",
                "created_at": row.get("created_at") or "",
                "updated_at": row.get("updated_at") or "",
                "message_count": row.get("message_count") or 0,
                "current": row.get("id") == session.session_id,
            })
        return {"ok": True, "sessions": items}

    def _models_listing() -> dict:
        try:
            rows = ctx.config.list_models()
        except Exception as e:
            return {"ok": False, "message": f"模型列表读取失败: {e}"}
        models = [
            {
                "model_name": r.get("model_name"),
                "provider_id": r.get("provider_id"),
                "model_id": r.get("model_id"),
                "context_window": r.get("context_window") or 0,
                "is_active": bool(r.get("is_active")),
            }
            for r in rows
        ]
        return {"ok": True, "models": models}

    def _op_model_switch(name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "message": "缺少模型名"}
        row = ctx.config.switch_model(name)
        if not row:
            return {"ok": False, "message": f"未找到模型: {name}"}
        llm = _llm()
        if llm is not None and hasattr(llm, "refresh_from_config"):
            llm.refresh_from_config(ctx.config)
        app = _app()
        if app is not None and hasattr(app, "bind_config"):
            app.bind_config(ctx.config)
        ctx.config.save()
        _broadcast({"t": "reset"})
        return {"ok": True, "result": f"已切换 {row['model_name']}", "model": row["model_name"]}

    def _op_mode(mode: str) -> dict:
        if policy_mod is None:
            return {"ok": False, "message": "宿主缺少 policy 模块"}
        mode = (mode or "").strip().lower()
        if mode not in policy_mod.MODES:
            return {"ok": False, "message": "模式: auto | manual | full"}
        app = _app()
        if app is not None:
            try:
                app.mode = mode
            except Exception:
                pass
        ctx.config.data.setdefault("ui", {})["mode"] = mode
        ctx.config.mode = mode
        ctx.config.save()
        label = policy_mod.MODE_LABELS.get(mode, mode)
        _broadcast({"t": "reset"})
        return {"ok": True, "result": f"访问模式: {label}", "mode": mode, "mode_label": label}

    def _workflows_listing() -> dict:
        if workflow_mod is None:
            return {"ok": False, "message": "宿主缺少 workflow 模块"}
        try:
            enabled = bool(getattr(workflow_mod, "workflow_enabled", lambda c: True)(ctx.config))
            return {
                "ok": True,
                "enabled": enabled,
                "active": workflow_mod.active_name(ctx.config),
                "workflows": workflow_mod.list_workflows(ctx.config),
            }
        except Exception as e:
            return {"ok": False, "message": f"工作流列表读取失败: {e}"}

    def _op_workflow_switch(name: str) -> dict:
        if workflow_mod is None:
            return {"ok": False, "message": "宿主缺少 workflow 模块"}
        name = (name or "").strip()
        if name not in workflow_mod.list_workflows(ctx.config):
            return {"ok": False, "message": f"未找到工作流: {name}"}
        workflow_mod.set_active(ctx.config, name)
        # 显式选择工作流即启用（工作流默认关闭=直接对话，与 /workflow 命令同语义）
        ctx.config.data.setdefault("workflow", {})["enabled"] = True
        ctx.config.save()
        return {"ok": True, "result": f"已启用工作流: {name}（下一回合生效）", "workflow": name}

    def _undo_listing() -> dict:
        session, err = _require_session()
        if err:
            return err
        if snapshot_mod is None:
            return {"ok": False, "message": "宿主缺少 snapshot 模块"}
        turns = snapshot_mod.list_turns(ctx.config, session.project_id, session.session_id)
        trash = snapshot_mod.trash_count(ctx.config, session.project_id)
        return {"ok": True, "turns": turns, "trash": trash}

    def _op_undo(which) -> dict:
        err = _session_op_guard()
        if err:
            return err
        session, err = _require_session()
        if err:
            return err
        if snapshot_mod is None:
            return {"ok": False, "message": "宿主缺少 snapshot 模块"}
        result = snapshot_mod.undo(ctx.config, session.project_id, session.session_id, which)
        if result is None:
            return {"ok": False, "message": "没有可回滚的回合快照"}
        lines = [f"已回滚回合 #{result['seq']}（{result['task'] or '（无任务摘要）'}）:"]
        for p in result["restored"]:
            lines.append(f"  还原 {p}")
        for p in result["deleted"]:
            lines.append(f"  删除 {p}")
        for p, why in result["skipped"]:
            lines.append(f"  跳过 {p}（{why}）")
        for p in result["external_changes"]:
            lines.append(f"  无法还原（非工具改动）{p}")
        if not (result["restored"] or result["deleted"] or result["skipped"]):
            lines.append("  （该回合无文件改动）")
        text = "\n".join(lines)
        session.add_message("system", text, type="help")
        app = _app()
        if app is not None:
            try:
                app.status = f"已回滚回合 #{result['seq']}"
            except Exception:
                pass
        _app_refresh(session)
        _broadcast({"t": "reset"})
        return {"ok": True, "result": text}

    def _tools_listing() -> dict:
        if register_mod is None:
            return {"ok": False, "message": "宿主缺少 register 模块"}
        return {"ok": True, "tools": [
            {"name": t.get("name"), "description": t.get("description") or ""}
            for t in register_mod.list_tools()
        ]}

    def _agents_listing() -> dict:
        if subagent_mod is None:
            return {"ok": False, "message": "宿主缺少 subagent 模块"}
        session = _session()
        records = []
        if session is not None:
            try:
                for rec in subagent_mod.list_records(ctx.config, session)[-20:]:
                    records.append({
                        "id": rec.get("id"),
                        "role": rec.get("role"),
                        "status": str(rec.get("status") or ""),
                        "status_label": subagent_mod.status_label(str(rec.get("status") or "")),
                        "rounds": rec.get("rounds", 0),
                        "task": " ".join(str(rec.get("task") or "").split())[:120],
                    })
            except Exception:
                pass
        return {"ok": True, "records": records, "roles": subagent_mod.roles_listing()}

    def _skills_listing() -> dict:
        if skills_mod is None:
            return {"ok": False, "message": "宿主缺少 skills 模块"}
        loader = skills_mod.get_loader(ctx.config)
        if loader is None:
            return {"ok": False, "message": "技能系统未启用（config.skills.enabled 或缺少 pyyaml）"}
        listing = loader.listing()
        return {"ok": True, "count": len(loader),
                "listing": listing or "暂无技能（放置 data/skills/<name>/SKILL.md）"}

    def _mcp_listing() -> dict:
        if mcp_mod is None:
            return {"ok": False, "message": "宿主缺少 mcp 模块"}
        return {"ok": True, "status": mcp_mod.status_listing()}

    def _plugins_listing() -> dict:
        if plugins_mod is None:
            return {"ok": False, "message": "宿主缺少 plugins 模块"}
        return {"ok": True, "plugins": plugins_mod.statuses(),
                "text": plugins_mod.status_listing()}

    def _commands_listing() -> dict:
        if commands_mod is None:
            return {"ok": False, "message": "宿主缺少 commands 模块"}
        items = []
        for meta in commands_mod.list_commands():
            if not meta.get("callable"):
                continue
            items.append({
                "name": meta.get("name"),
                "hint": meta.get("hint") or "",
                "usage": meta.get("usage") or "",
            })
        return {"ok": True, "commands": items}

    # 需要终端交互 / 同步跑回合 / 退出进程：远程一律拦截（对应能力由专用端点覆盖）
    _CMD_BLOCKED = {"/settings", "/resume", "/refine", "/exit", "/quit", "/run"}
    # 回合进行中拒绝的会话结构操作（与主循环口径一致）
    _CMD_BUSY_BLOCKED = {"/clear", "/new", "/resume", "/undo", "/clear-trash"}

    def _op_command(line: str) -> dict:
        if commands_mod is None:
            return {"ok": False, "message": "宿主缺少 commands 模块"}
        line = (line or "").strip()
        if not line.startswith("/"):
            return {"ok": False, "message": "命令须以 / 开头"}
        head = line.split()[0].lower()
        if head in _CMD_BLOCKED:
            return {"ok": False,
                    "message": f"{head} 需要终端交互，远程不可用（对应功能请用网页侧栏面板）"}
        if head == "/workflow" and "edit" in [p.lower() for p in line.split()[1:]]:
            return {"ok": False, "message": "工作流编辑器是桌面 GUI，远程不可用"}
        if head in _CMD_BUSY_BLOCKED and _busy():
            return {"ok": False, "message": "回合进行中：请先停止，或发送新消息按设置中断/排队后再操作"}
        session, err = _require_session()
        if err:
            return err
        runner = _runner()
        app = _app()
        cctx = {"llm": getattr(runner, "llm", None), "session": session, "config": ctx.config, "app": app}
        before = len(session.messages)
        try:
            result = commands_mod.execute(line, cctx)
        except Exception as e:
            return {"ok": False, "message": f"命令执行异常: {e}"}
        # 命令反馈多数经 _echo 写入会话 system 消息；部分 handler 返回说明文本
        echoed = [str(m.get("content") or "") for m in session.messages[before:]
                  if m.get("role") == "system"]
        payload = {"ok": True, "output": "\n".join(t for t in echoed if t)}
        if isinstance(result, str) and result.strip() and result != "EXIT":
            payload["result"] = result
        elif isinstance(result, dict):
            if result.get("reason") in ("unknown", "no_handler"):
                return {"ok": False, "message": f"未知命令: {head}"}
            payload["result"] = str(result)
        status = str(getattr(app, "status", "") or "")
        if status:
            payload["status"] = status
        _app_refresh(session)
        _broadcast({"t": "reset"})
        return payload

    # ---- HTTP 服务 ----

    def _check_key(self) -> bool:
        given = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
        real = str(st.key_data.get("key") or "")
        return bool(real) and hmac.compare_digest(real, given)

    def _make_handler():
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 静默：默认写 stderr 会刷 TUI
                ctx.log.debug("http %s", fmt % args)

            def _json(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _deny(self):
                self._json(403, {"ok": False, "message": "无效密钥或链接已过期"})

            def _body(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length > 0 else b""
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except (ValueError, json.JSONDecodeError, OSError):
                    return None

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/favicon.ico":
                    self._json(404, {"ok": False})
                    return
                if path == "/" or path == "/index.html":
                    # 页面静态放行（地址栏清洗/刷新后 URL 无密钥）：密钥门禁在 JS 启动时
                    # 经 /api/state 校验，无效则页内横幅提示；全部 API/SSE 仍强制校验
                    try:
                        html = page_path.read_bytes()
                    except OSError:
                        self._json(500, {"ok": False, "message": "页面文件缺失"})
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(html)
                    return
                if not _check_key(self):
                    self._deny()
                    return
                if path == "/api/state":
                    self._json(200, _snapshot())
                elif path == "/api/events":
                    self._do_events()
                elif path == "/api/sessions":
                    self._json(200, _sessions_listing())
                elif path == "/api/models":
                    self._json(200, _models_listing())
                elif path == "/api/tools":
                    self._json(200, _tools_listing())
                elif path == "/api/agents":
                    self._json(200, _agents_listing())
                elif path == "/api/skills":
                    self._json(200, _skills_listing())
                elif path == "/api/mcp":
                    self._json(200, _mcp_listing())
                elif path == "/api/plugins":
                    self._json(200, _plugins_listing())
                elif path == "/api/workflows":
                    self._json(200, _workflows_listing())
                elif path == "/api/undo":
                    self._json(200, _undo_listing())
                elif path == "/api/commands":
                    self._json(200, _commands_listing())
                else:
                    self._json(404, {"ok": False, "message": "not found"})

            def _do_events(self):
                client_q = queue.Queue(maxsize=400)
                client = (client_q, st.key_epoch)  # (队列, 密钥纪元)：revoke 时纪元变化即断开
                with st.lock:
                    st.clients.add(client)
                self.close_connection = True
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    hello = json.dumps({"t": "hello", "expires_at": st.key_data.get("expires_at", "")},
                                       ensure_ascii=False)
                    self.wfile.write(f"data: {hello}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    while not st.shutdown_flag.is_set():
                        try:
                            raw = client_q.get(timeout=SSE_HEARTBEAT)
                        except queue.Empty:
                            raw = None
                        if st.shutdown_flag.is_set() or client[1] != st.key_epoch:
                            break
                        try:
                            if raw is None:
                                self.wfile.write(b": ping\n\n")
                            else:
                                self.wfile.write(f"data: {raw}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                            break
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    with st.lock:
                        st.clients.discard(client)
            def do_POST(self):
                path = urlparse(self.path).path
                if not _check_key(self):
                    self._deny()
                    return
                data = self._body()
                if data is None:
                    self._json(400, {"ok": False, "message": "请求体不是合法 JSON"})
                    return
                if path == "/api/send":
                    text = str(data.get("text") or "").strip()
                    if not text:
                        self._json(400, {"ok": False, "message": "空消息"})
                        return
                    result = ctx.submit_turn(text)  # 返回状态说明（提交/排队/中断）
                    self._json(200, {"ok": True, "result": result})
                elif path == "/api/stop":
                    runner = plugins_mod.runtime().get("runner") if plugins_mod else None
                    if runner is None:
                        self._json(200, {"ok": False, "message": "调度器未就绪"})
                        return
                    if not getattr(runner, "busy", False):
                        self._json(200, {"ok": False, "message": "当前空闲，无需停止"})
                        return
                    llm = getattr(runner, "llm", None)
                    cancel = getattr(llm, "cancel", None)
                    if not callable(cancel):
                        self._json(200, {"ok": False, "message": "运行时不支持中断"})
                        return
                    cancel()
                    self._json(200, {"ok": True, "result": "已请求中断在途回合"})
                elif path == "/api/confirm":
                    cid = str(data.get("id") or "")
                    with st.lock:
                        item = next((c for c in st.confirms if c["id"] == cid), None)
                    if item is None:
                        self._json(200, {"ok": False, "message": "确认请求不存在或已处理"})
                        return
                    action = str(data.get("action") or "").lower()
                    if action not in ("allow_once", "allow_always", "deny", "reject"):
                        self._json(400, {"ok": False, "message": "action 须为 allow_once/allow_always/deny"})
                        return
                    item["response"] = {"action": action, "reason": str(data.get("reason") or "")}
                    item["event"].set()
                    _broadcast({"t": "confirm_done", "id": cid, "action": action})
                    self._json(200, {"ok": True})
                elif path == "/api/session/new":
                    self._json(200, _op_session_new())
                elif path == "/api/session/clear":
                    self._json(200, _op_session_clear())
                elif path == "/api/session/rename":
                    self._json(200, _op_session_rename(str(data.get("title") or "")))
                elif path == "/api/session/switch":
                    self._json(200, _op_session_switch(str(data.get("id") or "")))
                elif path == "/api/session/delete":
                    self._json(200, _op_session_delete(str(data.get("id") or "")))
                elif path == "/api/model/switch":
                    self._json(200, _op_model_switch(str(data.get("name") or "")))
                elif path == "/api/mode":
                    self._json(200, _op_mode(str(data.get("mode") or "")))
                elif path == "/api/workflow/switch":
                    self._json(200, _op_workflow_switch(str(data.get("name") or "")))
                elif path == "/api/undo":
                    which = data.get("which", "last")
                    if isinstance(which, str) and which.isdigit():
                        which = int(which)
                    self._json(200, _op_undo(which))
                elif path == "/api/command":
                    self._json(200, _op_command(str(data.get("line") or "")))
                else:
                    self._json(404, {"ok": False, "message": "not found"})

        return Handler

    def _start_server() -> str:
        if st.httpd is not None:
            return "远程控制服务已在运行"
        try:
            st.httpd = ThreadingHTTPServer((bind, port), _make_handler())
        except OSError as e:
            st.httpd = None
            ctx.log.warn("HTTP 服务启动失败: %s", e)
            return f"远程控制服务启动失败: {e}（端口被占用？）"
        st.shutdown_flag.clear()
        st.thread = threading.Thread(
            target=st.httpd.serve_forever, kwargs={"poll_interval": 0.5},
            daemon=True, name="remote-control-http",
        )
        st.thread.start()
        ctx.log.info("HTTP 服务已启动 %s:%s", bind, port)
        return _status_line()

    def _stop_server() -> str:
        httpd, thread = st.httpd, st.thread
        if httpd is None:
            return "远程控制服务未在运行"
        st.httpd = None
        st.thread = None
        st.shutdown_flag.set()
        _broadcast({"t": "bye"})  # 唤醒 SSE 等待，令其立刻退出
        try:
            httpd.shutdown()      # serve_forever 在独立线程，此处可安全阻塞等待
            httpd.server_close()
        except OSError as e:
            ctx.log.warn("HTTP 服务关闭异常: %s", e)
        if thread is not None:
            thread.join(timeout=3)
        ctx.log.info("HTTP 服务已停止")
        return "远程控制服务已停止"

    ctx.register_teardown(_stop_server)

    # ---- 斜杠命令 ----

    def _cmd_remote(cctx, args):
        arg = (args or "").strip().lower()
        if arg == "on":
            return _start_server()
        if arg == "off":
            return _stop_server()
        if arg == "revoke":
            st.key_data = _load_or_create_key(revoke=True)
            st.key_epoch += 1
            _broadcast({"t": "kick"})  # 旧密钥连接将被断开，重连时 403
            ctx.notify("[远程控制] 密钥已重置，旧链接全部失效，新链接:")
            return _status_line()
        return _status_line()

    def _complete_remote(config, arg):
        items = [
            {"name": "/remote revoke", "hint": "重置密钥并使旧链接失效", "source": "arg", "callable": True},
            {"name": "/remote on", "hint": "启动远程控制服务", "source": "arg", "callable": True},
            {"name": "/remote off", "hint": "停止远程控制服务", "source": "arg", "callable": True},
        ]
        return [i for i in items if i["name"].lower().endswith((arg or "").lower())] or items

    ctx.register_command(
        "/remote",
        hint="远程控制：显示链接 / 重置密钥 / 启停服务",
        usage="/remote [revoke|on|off]",
        handler=_cmd_remote,
        completer=_complete_remote,
    )

    # ---- 启动 ----

    if ctx.settings.get("enabled", True):
        result = _start_server()
        ctx.log.info("remote-control 装载完成: %s", result.splitlines()[0] if result else "")
    else:
        ctx.log.info("remote-control 服务按配置关闭（enabled=false），/remote on 可手动启动")
