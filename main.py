#该部分为程序的主逻辑，调用各个模块实现完整功能
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import commands
from core import hooks
from core import mcp as mcp_mod
from core import memory as memory_mod
from core import plugins as plugins_mod
from core import policy
from core import project_identity
from core import register
from core import session_store
from core import snapshot as snapshot_mod
from core import subagent as subagent_mod
from core import tools as tools_mod  # noqa: F401
from core import ui
from core import workflow as workflow_mod
from core.config import Config
from core.llm import CANCELLED, LLM
from core.log import get_logger

log = get_logger("main")


def _sync(app: "ui.TuiApp", session, task: str = "", status: str = "", render: bool = True) -> None:
    if status:
        app.status = status
    app.mode = getattr(app, "mode", policy.MODE_AUTO)
    app.refresh_from_session(session, task=task or None, render=render)
    app.tool_count = len(register.list_tools())


def _echo(ctx, content: str, mtype: str = "help") -> None:
    """命令反馈写入 session，避免 _sync 用 session 覆盖 app.messages 时丢失"""
    ctx["session"].add_message("system", content, type=mtype)


def _register_commands(llm: LLM, session, config: Config, app: "ui.TuiApp") -> None:
    def _ctx():
        return {"llm": llm, "session": session, "config": config, "app": app}

    @commands.register("/help", hint="帮助", source="builtin")
    def _help(ctx, args):
        _echo(ctx, commands.help_text())
        return True

    @commands.register("/exit", hint="退出", aliases=["/quit"], source="builtin")
    def _exit(ctx, args):
        return "EXIT"

    @commands.register("/settings", hint="设置", source="builtin")
    def _settings(ctx, args):
        ui.settings(ctx["config"])
        ctx["llm"].refresh_from_config(ctx["config"])
        ctx["app"].bind_config(ctx["config"])
        return True

    @commands.register("/resume", hint="历史会话 · 加载/删除", source="builtin")
    def _resume(ctx, args):
        sess = ctx["session"]
        directory = session_store.sessions_dir(ctx["config"], sess.project_id)
        result = ctx["app"].show_sessions_form(
            session_store.list_sessions(directory), current_id=sess.session_id, directory=directory
        )
        if not result:
            return True
        data = session_store.load_session_data(directory, result.get("id"))
        if not data:
            _echo(ctx, f"会话加载失败: {result.get('id')}")
            return True
        sess.save_session()
        sess.switch_to(data)
        ctx["app"]._tree_follow_tail = True  # 切换后贴底显示恢复的消息
        ctx["app"].status = f"已切换: {sess.session_title or sess.session_id}"
        return True

    @commands.register("/new", hint="新建会话 · 当前会话已保存", source="builtin")
    def _new(ctx, args):
        sess = ctx["session"]
        sess.save_session()
        sess.start_new_session()
        ctx["app"].task = ""
        ctx["app"].status = "新会话已开启"
        return True

    @commands.register("/clear", hint="清空当前会话 · 恢复为空对话", source="builtin")
    def _clear(ctx, args):
        ctx["session"].clear()
        ctx["app"].task = ""
        ctx["app"].status = "会话已清空"
        return True

    @commands.register("/rename", hint="重命名当前会话 /rename <标题>", usage="/rename <标题>", source="builtin")
    def _rename(ctx, args):
        title = (args or "").strip()
        if not title:
            _echo(ctx, "用法: /rename <标题>（未命名会话在 /resume 列表中显示为「（无标题会话）」）")
            return True
        name = ctx["session"].rename_session(title)
        ctx["app"].status = f"已重命名: {name}"
        return True

    @commands.register("/model", hint="切换模型 /model <model_name>", usage="/model <provider-model>", source="builtin")
    def _model(ctx, args):
        name = (args or "").strip()
        if not name:
            rows = ctx["config"].list_models()
            lines = ["模型列表 (model_name = provider_id-model_id):"]
            for row in rows:
                mark = " *" if row["is_active"] else ""
                lines.append(f"  {row['model_name']}{mark}")
            _echo(ctx, "\n".join(lines))
            return True
        row = ctx["config"].switch_model(name)
        if not row:
            _echo(ctx, f"未找到模型: {name}")
            return True
        ctx["llm"].refresh_from_config(ctx["config"])
        ctx["app"].bind_config(ctx["config"])
        ctx["config"].save()
        ctx["app"].status = f"已切换 {row['model_name']}"
        return True

    @commands.register("/mode", hint="访问模式 auto|manual|full · 也可按 ~", usage="/mode [auto|manual|full]", source="builtin")
    def _mode(ctx, args):
        arg = (args or "").strip().lower()
        if not arg:
            arg = ctx["app"].cycle_mode()
        elif arg not in policy.MODES:
            _echo(ctx, "模式: auto | manual | full")
            return True
        else:
            ctx["app"].mode = arg
        ctx["config"].data.setdefault("ui", {})["mode"] = ctx["app"].mode
        ctx["config"].mode = ctx["app"].mode
        ctx["config"].save()
        ctx["app"].status = policy.MODE_LABELS[ctx["app"].mode]
        return True

    @commands.register("/theme", hint="主题 /theme [名称]", usage="/theme [dark|ocean]", source="builtin")
    def _theme(ctx, args):
        name = (args or "").strip()
        themes = ctx["config"].list_themes()
        if not name:
            _echo(ctx, "主题: " + ", ".join(themes))
            return True
        if name not in themes:
            # 仍尝试载入，无效会回落默认
            pass
        ctx["config"].data.setdefault("ui", {})["theme"] = name
        ctx["config"].theme = name
        ctx["app"].load_theme(name)
        ctx["config"].save()
        ctx["app"].status = f"主题 {name}"
        return True

    @commands.register("/tools", hint="工具列表", source="builtin")
    def _tools(ctx, args):
        lines = [f"{t['name']}: {t['description']}" for t in register.list_tools()]
        _echo(ctx, "工具:\n" + "\n".join(lines))
        return True

    @commands.register("/agents", hint="子代理 · 角色列表/本会话记录/重载", usage="/agents [reload]", source="builtin")
    def _agents(ctx, args):
        arg = (args or "").strip().lower()
        if arg == "reload":
            count = subagent_mod.load_specs(ctx["config"])
            _echo(ctx, f"子代理角色已重扫: {count} 个")
            return True
        sess = ctx["session"]
        records = subagent_mod.list_records(ctx["config"], sess)
        lines = [f"本会话子代理 {len(records)} 个:"]
        for rec in records[-20:]:
            label = subagent_mod.status_label(str(rec.get("status")))
            task_brief = " ".join(str(rec.get("task") or "").split())[:40]
            lines.append(
                f"  #{rec.get('id')} · {rec.get('role')} · {label}"
                f" · {rec.get('rounds', 0)}轮 · {task_brief}"
            )
        lines.append("角色:")
        lines.append(subagent_mod.roles_listing())
        lines.append("提示: /agents reload 重扫 data/agents；子代理经 task 工具派发、query_subagent 查询")
        _echo(ctx, "\n".join(lines))
        return True

    @commands.register("/skill", hint="技能系统 · 列表/查看/重载", usage="/skill [名称|reload]", source="builtin")
    def _skill(ctx, args):
        from core import skills as skills_mod

        arg = (args or "").strip()
        loader = skills_mod.get_loader(ctx["config"])
        if loader is None:
            _echo(ctx, "技能系统未启用（config.skills.enabled 或缺少 pyyaml）")
            return True
        if arg == "reload":
            _echo(ctx, f"技能已重扫: {loader.reload()} 个")
            return True
        if arg:
            sk = loader.load(arg)
            if sk is None:
                _echo(ctx, f"技能不存在: {arg}\n可用:\n{loader.listing()}")
            else:
                _echo(ctx, f"[{sk.name}] {sk.path}\n正文约 {sk.token_count} tokens\n\n{sk.body}")
            return True
        listing = loader.listing()
        _echo(
            ctx,
            f"可用技能 {len(loader)} 个:\n{listing}" if listing else "暂无技能（放置 data/skills/<name>/SKILL.md）",
        )
        return True

    @commands.register("/mcp", hint="MCP 服务器 · 列表/工具/重连", usage="/mcp [tools 名称|reconnect 名称]", source="builtin")
    def _mcp(ctx, args):
        arg = (args or "").strip()
        parts = arg.split(maxsplit=1)
        head = parts[0].lower() if parts else ""
        if head == "reconnect":
            ok, msg = mcp_mod.reconnect(parts[1].strip() if len(parts) > 1 else "")
            _echo(ctx, msg)
            return True
        if head == "tools":
            _echo(ctx, mcp_mod.tools_listing(parts[1].strip() if len(parts) > 1 else ""))
            return True
        _echo(ctx, mcp_mod.status_listing())
        return True

    @commands.register("/undo", hint="回滚回合文件改动 /undo [list|序号]", usage="/undo [list|序号]", source="builtin")
    def _undo(ctx, args):
        arg = (args or "").strip().lower()
        sess = ctx["session"]
        if arg == "list":
            turns = snapshot_mod.list_turns(ctx["config"], sess.project_id, sess.session_id)
            if not turns:
                _echo(ctx, "本会话暂无回合快照（文件改动回合结束时自动生成）")
                return True
            lines = ["回合快照:"]
            for t in turns:
                lines.append(
                    f"  #{t['seq']} · {t['time']} · {t['files']} 文件（可还原 {t['restorable']}）"
                    f" · {t['task'] or '（无任务摘要）'}"
                )
            lines.append("提示: /undo 回滚最近回合 · /undo <序号> 回滚指定回合 · 事后改过的文件会跳过")
            _echo(ctx, "\n".join(lines))
            return True
        result = snapshot_mod.undo(
            ctx["config"], sess.project_id, sess.session_id, int(arg) if arg.isdigit() else "last"
        )
        if result is None:
            _echo(ctx, "没有可回滚的回合快照（/undo list 查看）")
            return True
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
        ctx["app"].status = f"已回滚回合 #{result['seq']}"
        _echo(ctx, "\n".join(lines), mtype="help")
        return True

    @commands.register("/clear-trash", hint="清空项目回收站（真正不可逆删除）", source="builtin")
    def _clear_trash(ctx, args):
        count, size = snapshot_mod.clear_trash(ctx["config"], ctx["session"].project_id)
        if count:
            _echo(ctx, f"回收站已清空: {count} 项，释放 {size} 字节")
        else:
            _echo(ctx, "回收站已是空的")
        ctx["app"].status = "回收站已清空" if count else "回收站为空"
        return True

    @commands.register("/plan", hint="查看计划", source="builtin")
    def _plan(ctx, args):
        ctx["app"].status = f"计划 {ctx['session'].plan.get('status')}"
        return True

    @commands.register(
        "/workflow", hint="工作流 · 列表/切换/停用/编辑", usage="/workflow [名称|off|edit [名称]]", source="builtin"
    )
    def _workflow(ctx, args):
        arg = (args or "").strip()
        parts = arg.split(maxsplit=1)
        head = parts[0].lower() if parts else ""
        if head in ("edit", "gui", "编辑"):
            name = parts[1].strip() if len(parts) > 1 else workflow_mod.active_name(ctx["config"])
            _launch_workflow_editor(ctx, name)
            return True
        if arg in ("", "list", "列表"):
            active = workflow_mod.active_name(ctx["config"])
            names = workflow_mod.list_workflows(ctx["config"])
            state = "启用" if workflow_mod.workflow_enabled(ctx["config"]) else "未启用（直接对话）"
            lines = [f"工作流（{state} · active={active}）:"]
            for name in names:
                lines.append(("  * " if name == active else "    ") + name)
            lines.append("提示: /workflow <名称> 切换并启用 · /workflow off 直接对话 · /workflow edit [名称] 打开编辑器")
            _echo(ctx, "\n".join(lines))
            return True
        if arg in ("off", "off".upper(), "关闭", "直接对话"):
            wf_cfg = ctx["config"].data.setdefault("workflow", {})
            wf_cfg["enabled"] = False
            ctx["config"].save()
            ctx["app"].status = "直接对话"
            _echo(ctx, "工作流已停用：直接与 LLM 对话（下一回合生效）")
            return True
        if arg not in workflow_mod.list_workflows(ctx["config"]):
            available = ", ".join(workflow_mod.list_workflows(ctx["config"])) or "（无）"
            _echo(ctx, f"未找到工作流: {arg}\n可用: {available}（/workflow off 可停用工作流）")
            return True
        workflow_mod.set_active(ctx["config"], arg)
        ctx["config"].data.setdefault("workflow", {})["enabled"] = True  # 显式选择即启用
        ctx["config"].save()
        ctx["app"].status = f"工作流: {arg}"
        _echo(ctx, f"已启用工作流: {arg}（下一回合生效；/workflow off 可停用）")
        return True

    @commands.register("/steps", hint="查看步骤", source="builtin")
    def _steps(ctx, args):
        ctx["app"].status = f"步骤 {len(ctx['session'].steps)}"
        return True

    @commands.register("/refine", hint="完善输入后执行", source="builtin")
    def _refine(ctx, args):
        original = ctx["app"].read_line("待完善> ", config=ctx["config"])
        refined = ctx["llm"].refine_prompt(original)
        ctx["session"].add_message("assistant", f"[refine]\n{refined}", type="refine")
        edited = ctx["app"].read_line("确认/编辑> ", config=ctx["config"])
        final = edited.strip() or refined
        choice = ctx["app"].choose([("Y", "使用"), ("n", "放弃")], prompt="使用完善结果？").lower()
        if choice in ("", "y", "yes"):
            final = _before_turn(final)
            try:
                _agent_turn(ctx["llm"], ctx["session"], final, ctx["app"])
            finally:
                _after_turn(ctx["session"], final)
        return True

    @commands.register("/run", hint="执行任务", usage="/run <任务>", source="builtin")
    def _run(ctx, args):
        task = (args or "").strip()
        if not task:
            task = ctx["app"].read_line("任务> ", config=ctx["config"]).strip()
        if task:
            task = _before_turn(task)
            try:
                _agent_turn(ctx["llm"], ctx["session"], task, ctx["app"])
            finally:
                _after_turn(ctx["session"], task)
        return True

    @commands.register("/balance", hint="查询余额", source="builtin")
    def _balance(ctx, args):
        amount = ctx["llm"].query_balance()
        ctx["app"].token_meter = ctx["llm"].meter
        ctx["app"].status = "余额可用" if amount is not None else "当前接口未提供余额"
        return True

    @commands.register("/commands", hint="命令系统", source="builtin")
    def _cmds(ctx, args):
        _echo(ctx, commands.help_text())
        return True

    @commands.register("/plugin", hint="插件系统 · 查看/重载/启停", usage="/plugin [reload|enable <id>|disable <id>]", source="builtin")
    def _plugin(ctx, args):
        cfg = ctx["config"]
        parts = (args or "").split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if sub == "reload":
            counts = plugins_mod.reload(cfg)  # 复用启动时的 workspace
            _echo(
                ctx,
                f"插件已重载：装载 {counts['loaded']} · 禁用 {counts['disabled']} · 失败 {counts['failed']}\n"
                + plugins_mod.status_listing(),
            )
            return True
        if sub in ("enable", "disable"):
            pid = arg.split()[0] if arg else ""
            if not pid:
                _echo(ctx, f"用法: /plugin {sub} <插件id>（/plugin 查看 id 列表）")
                return True
            if plugins_mod.set_enabled(pid, sub == "enable", cfg):
                counts = plugins_mod.reload(cfg)
                _echo(
                    ctx,
                    f"已{('启用' if sub == 'enable' else '禁用')}插件 {pid}（写入 config.plugins.disable）· "
                    f"装载 {counts['loaded']} / 失败 {counts['failed']}",
                )
            else:
                _echo(ctx, f"未找到插件: {pid}")
            return True
        if sub and sub != "list":
            _echo(ctx, f"未知子命令: {sub}（可用: reload / enable / disable）")
        _echo(ctx, plugins_mod.status_listing())
        return True

    def _complete_plugin(config, arg: str):
        items = [{"name": "/plugin reload", "hint": "重新发现并装载全部插件", "source": "arg", "callable": True}]
        for row in plugins_mod.statuses():
            if row["status"] == "loaded":
                items.append(
                    {"name": f"/plugin disable {row['id']}", "hint": "禁用并持久化", "source": "arg", "callable": True}
                )
            else:
                items.append(
                    {"name": f"/plugin enable {row['id']}", "hint": row["error"][:36] or "启用并持久化", "source": "arg", "callable": True}
                )
        return commands._filter_arg_items(items, arg)

    commands.register_arg_completer("/plugin", _complete_plugin)

    commands.load_plugins(str(_ROOT / "data" / "commands"))


def _handle_tool_confirm(llm: LLM, app: "ui.TuiApp", session, call: dict, cancelled=None) -> dict:
    """工具确认：先经 tool_confirm 扩展点（插件/外部接口可代答），无处理时
    经 UI 请求桥在主线程弹面板（↑↓ 选择）；取消时按默认拒绝回注"""
    hooked = hooks.call_hook(
        "tool_confirm",
        {"name": call.get("name"), "arguments": call.get("arguments") or {}},
        default=None,
    )
    if isinstance(hooked, dict):
        action = str(hooked.get("action") or "").lower()
        if action in ("allow_once", "allow_always"):
            if action == "allow_always":
                policy.add_always_allow(call.get("name"), call.get("arguments") or {})
            return llm.execute_approved_tool(call)
        if action in ("deny", "denied", "reject"):
            reason = str(hooked.get("reason") or hooked.get("message") or "")
            return {
                "role": "tool",
                "tool_call_id": call.get("id"),
                "tool_name": call.get("name"),
                "content": policy.default_reject_message(reason or f"tool_confirm 扩展点拒绝（{hooked.get('source', 'plugin')}）"),
                "type": "tool",
            }
    app.pending_tool = {"name": call.get("name"), "arguments": call.get("arguments") or {}}
    try:
        req = app.request_ui(
            "confirm",
            {"name": str(call.get("name") or ""), "arguments": call.get("arguments") or {}},
        )
        result = app.wait_ui(req, cancelled)
    finally:
        app.pending_tool = None
    if result == CANCELLED or not isinstance(result, dict):
        return {
            "role": "tool",
            "tool_call_id": call.get("id"),
            "tool_name": call.get("name"),
            "content": policy.default_reject_message(""),
            "type": "tool",
        }
    if result.get("action") == "allow_once":
        return llm.execute_approved_tool(call)
    if result.get("action") == "allow_always":
        policy.add_always_allow(call.get("name"), call.get("arguments") or {})
        return llm.execute_approved_tool(call)
    # 拒绝：reason 为用户输入的原因（空则用默认文案）
    return {
        "role": "tool",
        "tool_call_id": call.get("id"),
        "tool_name": call.get("name"),
        "content": policy.default_reject_message(result.get("reason") or ""),
        "type": "tool",
    }


def _bridge_choose(app: "ui.TuiApp", options, prompt: str, cancelled=None):
    req = app.request_ui("choose", {"options": options, "prompt": prompt})
    return app.wait_ui(req, cancelled)


def _bridge_line(app: "ui.TuiApp", prompt: str, config, cancelled=None):
    req = app.request_ui("line", {"prompt": prompt, "config": config})
    return app.wait_ui(req, cancelled)


def _launch_workflow_editor(ctx, name: str = "") -> None:
    """子进程拉起 PySide6 工作流编辑器（独立事件循环，不阻塞 TUI）"""
    try:
        import PySide6  # noqa: F401
    except ImportError:
        _echo(ctx, "工作流编辑器需要 PySide6：pip install PySide6")
        return
    import subprocess

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = [sys.executable, "-m", "gui.workflow_editor"]
    if name:
        cmd.append(name)
    subprocess.Popen(cmd, cwd=str(_ROOT), **kwargs)
    _echo(ctx, f"工作流编辑器已启动: {name or '（新工作流）'}")


def _before_turn(text: str) -> str:
    """before_turn 变换链：插件可改写本轮输入（返回 {user_text: ...} 生效），不改写则原样返回"""
    before = hooks.call_hook("before_turn", {"user_text": text}, default=None)
    rewritten = before.get("user_text") if isinstance(before, dict) else None
    return rewritten if isinstance(rewritten, str) and rewritten.strip() else text


def _after_turn(session, user_text: str) -> None:
    """after_turn 观察链：回合结束通知（正常/中断/异常路径都触发），响应取最后一条 assistant 消息"""
    last = next((m for m in reversed(session.messages) if m.get("role") == "assistant"), None)
    hooks.collect_hook(
        "after_turn",
        {
            "user_text": user_text,
            "response": (last or {}).get("content", ""),
            "session_id": getattr(session, "session_id", ""),
        },
    )


def _agent_turn(llm: LLM, session, user_text: str, app: "ui.TuiApp", runner=None) -> None:
    """回合入口：无论正常结束/出错/中断，回合末执行上下文维护（剥离+预算外置），
    并清掉流式树尾消息（partial 丢弃，与在途回合丢弃语义一致）与子代理运行时/直播槽"""
    try:
        _agent_turn_impl(llm, session, user_text, app, runner=runner)
    finally:
        try:
            snapshot_mod.end_turn()  # 回合快照落盘（中断/异常路径同样收尾；空回合无副作用）
        except Exception as exc:
            log.error("回合快照收尾失败: %r", exc)
        try:
            session.finalize_turn()
        except Exception as exc:
            log.error("回合末上下文维护失败: %r", exc)
        subagent_mod.unbind_runtime()
        app.streaming_msg = None
        app.subagent_stream = None
        app.phase_hint = ""


def _agent_turn_impl(llm: LLM, session, user_text: str, app: "ui.TuiApp", runner=None) -> None:
    """一个 agent 回合；runner 存在时在后台线程运行——交互弹窗经 UI 请求桥
    移交主线程执行，取消检查贯穿各检查点（中断=丢弃在途回合）。"""

    def _cancelled() -> bool:
        return runner is not None and llm.cancelled()

    def _syncq(task=None, status=""):
        # 后台线程只改状态不渲染：主循环帧渲染统一完成
        _sync(app, session, task=task, status=status, render=False)

    def _set_status(text: str) -> None:
        _syncq(status=text)
        hooks.collect_hook("turn_status", {"status": text})

    def _set_phase(text: str) -> None:
        app.phase_hint = text
        hooks.collect_hook("turn_status", {"phase": text})

    def _aborted():
        session.add_message("system", "[本轮已被新消息中断]", type="help")
        _syncq(status="已中断")

    def _on_delta(kind: str, piece: str) -> None:
        # 流式回调（agent 线程）：只改 UI 状态不渲染——流式对象挂在 app.streaming_msg，
        # 不进 session.messages（半成品不落盘/不进 API），帧循环每帧读它画树尾直播
        msg = getattr(app, "streaming_msg", None)
        if msg is None:
            msg = {"role": "assistant", "content": "", "thinking": "", "type": "streaming"}
            app.streaming_msg = msg
        if kind == "reasoning":
            msg["thinking"] += piece
        else:
            msg["content"] += piece
        hooks.collect_hook("stream_delta", {"kind": kind, "piece": piece})

    log.info("任务开始: %s", user_text)
    session.add_message("user", user_text, type="task")
    # 回合快照：绑定上下文 + 轻量清单对账基线（写入工具经 capture_before 留底）；
    # msg_index=本回合用户消息绝对序号，树模式 Ctrl+Z 按它联动文件回滚
    snapshot_mod.begin_turn(
        llm.config, session.project_id, session.session_id, user_text,
        msg_index=len(session.messages) - 1,
    )
    mode = app.mode
    _syncq(task=user_text, status=f"{policy.MODE_LABELS.get(mode, mode)} · 分析")
    if _cancelled():
        _aborted()
        return

    # 外置工作流：按 data/workflows/<active>.json 的节点链驱动一轮回合
    # （系统提示词注入/分析/规划/编码等均为节点，增删改走编辑器或直接改 JSON）
    io = workflow_mod.TurnIO(
        status=_set_status,
        choose=lambda options, prompt: _bridge_choose(app, options, prompt, _cancelled),
        line=lambda prompt: _bridge_line(app, prompt, llm.config, _cancelled),
        cancelled=_cancelled,
        on_delta=_on_delta,
        clear_stream=lambda: setattr(app, "streaming_msg", None),
        tool_confirm=lambda call: _handle_tool_confirm(llm, app, session, call, _cancelled),
        phase=_set_phase,
    )
    wf = None
    if workflow_mod.workflow_enabled(llm.config):
        wf = workflow_mod.load_workflow(llm.config)
        log.info("工作流: %s（%d节点）", wf.get("name"), len(wf.get("nodes") or []))
    else:
        log.info("工作流未启用：直接对话（设置页\"系统\"标签可启用）")
    turn = workflow_mod.TurnContext(
        session=session, llm=llm, app=app, config=llm.config, user_text=user_text, io=io
    )
    # 子代理运行时随回合绑定/解绑：task 工具经此取得 llm/io/app/session
    subagent_mod.bind_runtime(llm=llm, config=llm.config, io=io, app=app, session=session)

    def _bridge_ask(payload: dict) -> dict:
        req = app.request_ui("ask", payload)
        return app.wait_ui(req, _cancelled)

    tools_mod.set_ask_user_bridge(_bridge_ask)  # ask_user 工具经此弹面板（回合内有效）
    try:
        workflow_mod.run_turn(wf, turn)
    except workflow_mod.TurnInterrupt:
        _aborted()
    except workflow_mod.TurnStop:
        pass
    finally:
        tools_mod.set_ask_user_bridge(None)


class _AgentRunner:
    """后台线程执行 agent 回合：等待响应期间 TUI 保持可交互。

    busy 期间用户消息按 ui.busy_send_mode 处理（设置页可改）：
      queue     — 入队，本轮结束后按顺序接力执行（默认）
      interrupt — 取消在途请求并丢弃本轮，强行以新消息开启新一轮
    """

    def __init__(self, llm: LLM, session, app: "ui.TuiApp", config: Config):
        self.llm = llm
        self.session = session
        self.app = app
        self.config = config
        self.busy = False
        self._queue: list = []
        self._lock = threading.Lock()

    def mode(self) -> str:
        ui_cfg = (self.config.data or {}).get("ui") or {}
        m = str(ui_cfg.get("busy_send_mode") or "queue").strip().lower()
        return m if m in ("queue", "interrupt") else "queue"

    def _set_busy(self, value: bool) -> None:
        """busy 双写：runner 自身判定 + app.busy 镜像（UI 键位如树回退据此拒绝，跨线程只读）"""
        self.busy = value
        try:
            self.app.busy = value
        except Exception:
            pass

    def start(self, text: str) -> bool:
        with self._lock:
            if self.busy:
                return False
            self._set_busy(True)
        t = threading.Thread(target=self._run, args=(text,), daemon=True, name="bawcode-agent")
        t.start()
        return True

    def notify(self, text: str) -> None:
        """后台事件通知入口（tools.set_background_notifier 注册）：
        idle 直接开新回合，busy 入队接力——不打断在途回合（区别于 submit 的 interrupt 语义）"""
        with self._lock:
            self._queue.append(text)
            if self.busy:
                return
            self._set_busy(True)
        threading.Thread(target=self._run, args=(None,), daemon=True, name="bawcode-agent").start()

    def submit(self, text: str) -> str:
        """busy 期间的发送语义；返回给用户看的状态说明"""
        if self.mode() == "interrupt":
            self.llm.cancel()  # 置标记 + 关闭在途连接，agent 回合在检查点丢弃
            with self._lock:
                self._queue = [text]  # 强行插入：清空队列仅保留新消息
            return "[已中断当前请求，新消息将立即开始]"
        with self._lock:
            self._queue.append(text)
        return f"[已排队 {len(self._queue)} 条 · 本轮结束后自动发送]"

    def pop_queue(self):
        with self._lock:
            return self._queue.pop(0) if self._queue else None

    def _run(self, first=None) -> None:
        # busy 生命周期全部在锁内决策：循环顶"取队续跑或退忙退出"，
        # 与 notify/start 的置忙互斥，杜绝双 agent 线程并发
        text = first
        while True:
            if text is None:
                with self._lock:
                    text = self._queue.pop(0) if self._queue else None
                    if text is None:
                        self._set_busy(False)
                        return
            try:
                text = _before_turn(text)
                self.llm.reset_cancel()
                _agent_turn(self.llm, self.session, text, self.app, runner=self)
            except Exception as exc:
                log.error("agent 回合异常: %r", exc)
                try:
                    self.session.add_message("system", f"回合异常: {exc}", type="help")
                except Exception:
                    pass
            finally:
                _after_turn(self.session, text)
                try:
                    self.session.save_session()
                except Exception:
                    pass
            text = None

    def cancel_and_join(self, timeout: float = 3.0) -> None:
        """退出前中止在途请求并等待线程收尾"""
        self.llm.cancel()
        with self._lock:
            self._queue.clear()


def main() -> None:
    # 尽早开启 Windows 输入/输出 VT，便于 Shift+Tab → ESC [ Z
    ui._enable_windows_ansi()
    config = Config()
    identity = project_identity.ensure_project_identity(Path.cwd())
    log.info(
        "BAWCode 启动 · 配置=%s · 模型=%s · project_id=%s",
        config.config_path,
        config.model_name,
        identity.get("project_id"),
    )
    plugins_mod.load(config, workspace=Path.cwd())  # 外部插件装载（先于会话初始化，插件可收到 session_start）
    session = memory_mod.init_session(config, project_identity_data=identity)
    tools_mod.webfetch_gc()  # 启动探测：闲置会话（1 天无更新）的 webfetch 图片目录回收，活跃会话图片 30 天封顶
    llm = LLM(config)
    session.set_llm_fn(lambda p: llm.chat([{"role": "user", "content": p}]).get("content", ""))
    hooks.set_external_apis((config.data or {}).get("external_apis") or {})
    subagent_mod.register_tools(config)
    mcp_mod.register_tools(config)  # MCP 服务器后台连接发现，工具随注册进度逐个可见
    app = ui.get_app()
    app.bind_config(config)
    app.token_meter = llm.meter
    _register_commands(llm, session, config, app)

    app.enter()
    try:
        runner = _AgentRunner(llm, session, app, config)
        tools_mod.set_background_notifier(runner.notify)  # 后台命令完成 → runner.notify 自动开新回合
        plugins_mod.bind_runtime(app=app, runner=runner)  # 插件类用户操作 API（submit_turn/notify/ctx.ui）载体
        llm.query_balance()
        app.token_meter = llm.meter
        _sync(app, session, status="就绪")
        while True:
            raw = app.read_line(config=config)
            app.token_meter = llm.meter
            text = (raw or "").strip()
            if not text:
                continue
            if text.startswith("/"):
                head = text.split()[0]
                if runner.busy and head in ("/clear", "/new", "/resume", "/undo", "/clear-trash"):
                    session.add_message(
                        "system",
                        "本轮对话进行中：等待完成，或直接发送新消息（按设置中断/排队）后再操作会话",
                        type="help",
                    )
                    _sync(app, session)
                    continue
                result = commands.execute(text, {"llm": llm, "session": session, "config": config, "app": app})
                app.bind_config(config)
                llm.config = config
                app.token_meter = llm.meter
                if result == "EXIT":
                    runner.cancel_and_join()
                    session.save_longterm()
                    session.save_session()
                    break
                if isinstance(result, dict) and result.get("reason") in ("unknown", "no_handler"):
                    session.add_message("system", f"命令问题: {result}", type="help")
                _sync(app, session)
                if not runner.busy:
                    session.save_session()
                continue
            if runner.busy:
                note = runner.submit(text)
                session.add_message("system", note, type="help")
                _sync(app, session)
                continue
            runner.start(text)
    except KeyboardInterrupt:
        log.info("用户中断（Ctrl+C）")
        session.save_longterm()
        session.save_session()
    finally:
        try:
            mcp_mod.shutdown()  # 先断 MCP（daemon 线程收尾），再恢复终端
        except Exception:
            pass
        try:
            n = snapshot_mod.trash_count(config, session.project_id)
            if n:
                print(f"\n提示: 项目回收站有 {n} 个文件待处理（/clear-trash 真正删除）")
        except Exception:
            pass
        app.leave()
        print("BAWCode 已退出。")
        log.info("BAWCode 已退出")


if __name__ == "__main__":
    main()
