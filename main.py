#该部分为程序的主逻辑，调用各个模块实现完整功能
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import commands
from core import hooks
from core import memory as memory_mod
from core import policy
from core import project_identity
from core import register
from core import session_store
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

    @commands.register("/plan", hint="查看计划", source="builtin")
    def _plan(ctx, args):
        ctx["app"].status = f"计划 {ctx['session'].plan.get('status')}"
        return True

    @commands.register(
        "/workflow", hint="工作流 · 列表/切换/编辑", usage="/workflow [名称|edit [名称]]", source="builtin"
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
            lines = [f"工作流（active={active}）:"]
            for name in names:
                lines.append(("  * " if name == active else "    ") + name)
            lines.append("提示: /workflow <名称> 切换 · /workflow edit [名称] 打开编辑器")
            _echo(ctx, "\n".join(lines))
            return True
        if arg not in workflow_mod.list_workflows(ctx["config"]):
            available = ", ".join(workflow_mod.list_workflows(ctx["config"])) or "（无）"
            _echo(ctx, f"未找到工作流: {arg}\n可用: {available}")
            return True
        workflow_mod.set_active(ctx["config"], arg)
        ctx["config"].save()
        ctx["app"].status = f"工作流: {arg}"
        _echo(ctx, f"已切换工作流: {arg}（下一回合生效）")
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
            _agent_turn(ctx["llm"], ctx["session"], final, ctx["app"])
        return True

    @commands.register("/run", hint="执行任务", usage="/run <任务>", source="builtin")
    def _run(ctx, args):
        task = (args or "").strip()
        if not task:
            task = ctx["app"].read_line("任务> ", config=ctx["config"]).strip()
        if task:
            _agent_turn(ctx["llm"], ctx["session"], task, ctx["app"])
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

    commands.load_plugins(str(_ROOT / "data" / "commands"))


def _handle_tool_confirm(llm: LLM, app: "ui.TuiApp", session, call: dict, cancelled=None) -> dict:
    """工具确认：经 UI 请求桥在主线程弹面板（↑↓ 选择）；取消时按默认拒绝回注"""
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


def _agent_turn(llm: LLM, session, user_text: str, app: "ui.TuiApp", runner=None) -> None:
    """回合入口：无论正常结束/出错/中断，回合末执行上下文维护（剥离+预算外置），
    并清掉流式树尾消息（partial 丢弃，与在途回合丢弃语义一致）"""
    try:
        _agent_turn_impl(llm, session, user_text, app, runner=runner)
    finally:
        try:
            session.finalize_turn()
        except Exception as exc:
            log.error("回合末上下文维护失败: %r", exc)
        app.streaming_msg = None
        app.phase_hint = ""


def _agent_turn_impl(llm: LLM, session, user_text: str, app: "ui.TuiApp", runner=None) -> None:
    """一个 agent 回合；runner 存在时在后台线程运行——交互弹窗经 UI 请求桥
    移交主线程执行，取消检查贯穿各检查点（中断=丢弃在途回合）。"""

    def _cancelled() -> bool:
        return runner is not None and llm.cancelled()

    def _syncq(task=None, status=""):
        # 后台线程只改状态不渲染：主循环帧渲染统一完成
        _sync(app, session, task=task, status=status, render=False)

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

    log.info("任务开始: %s", user_text)
    session.add_message("user", user_text, type="task")
    mode = app.mode
    _syncq(task=user_text, status=f"{policy.MODE_LABELS.get(mode, mode)} · 分析")
    if _cancelled():
        _aborted()
        return

    # 外置工作流：按 data/workflows/<active>.json 的节点链驱动一轮回合
    # （系统提示词注入/分析/规划/编码等均为节点，增删改走编辑器或直接改 JSON）
    io = workflow_mod.TurnIO(
        status=lambda text: _syncq(status=text),
        choose=lambda options, prompt: _bridge_choose(app, options, prompt, _cancelled),
        line=lambda prompt: _bridge_line(app, prompt, llm.config, _cancelled),
        cancelled=_cancelled,
        on_delta=_on_delta,
        clear_stream=lambda: setattr(app, "streaming_msg", None),
        tool_confirm=lambda call: _handle_tool_confirm(llm, app, session, call, _cancelled),
        phase=lambda text: setattr(app, "phase_hint", text),
    )
    wf = workflow_mod.load_workflow(llm.config)
    log.info("工作流: %s（%d节点）", wf.get("name"), len(wf.get("nodes") or []))
    turn = workflow_mod.TurnContext(
        session=session, llm=llm, app=app, config=llm.config, user_text=user_text, io=io
    )
    try:
        workflow_mod.run_workflow(wf, turn)
    except workflow_mod.TurnInterrupt:
        _aborted()
    except workflow_mod.TurnStop:
        pass


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

    def start(self, text: str) -> bool:
        if self.busy:
            return False
        t = threading.Thread(target=self._run, args=(text,), daemon=True, name="bawcode-agent")
        t.start()
        return True

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

    def _run(self, first: str) -> None:
        text = first
        while text:
            self.busy = True
            try:
                self.llm.reset_cancel()
                _agent_turn(self.llm, self.session, text, self.app, runner=self)
            except Exception as exc:
                log.error("agent 回合异常: %r", exc)
                try:
                    self.session.add_message("system", f"回合异常: {exc}", type="help")
                except Exception:
                    pass
            finally:
                self.busy = False
                try:
                    self.session.save_session()
                except Exception:
                    pass
            text = self.pop_queue()

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
    session = memory_mod.init_session(config, project_identity_data=identity)
    llm = LLM(config)
    session.set_llm_fn(lambda p: llm.chat([{"role": "user", "content": p}]).get("content", ""))
    hooks.set_external_apis((config.data or {}).get("external_apis") or {})
    app = ui.get_app()
    app.bind_config(config)
    app.token_meter = llm.meter
    _register_commands(llm, session, config, app)

    app.enter()
    try:
        runner = _AgentRunner(llm, session, app, config)
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
                if runner.busy and head in ("/clear", "/new", "/resume"):
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
        app.leave()
        print("BAWCode 已退出。")
        log.info("BAWCode 已退出")


if __name__ == "__main__":
    main()
