#该部分为程序的主逻辑，调用各个模块实现完整功能
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import commands
from core import hooks
from core import memory as memory_mod
from core import policy
from core import project_identity
from core import prompt_loader
from core import register
from core import tools as tools_mod  # noqa: F401
from core import ui
from core.config import Config
from core.llm import get_system_prompt, LLM
from core.log import get_logger

log = get_logger("main")


def _sync(app: "ui.TuiApp", session, task: str = "", status: str = "") -> None:
    if status:
        app.status = status
    app.mode = getattr(app, "mode", policy.MODE_AUTO)
    app.refresh_from_session(session, task=task or None)
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

    @commands.register("/plan", hint="查看计划", source="builtin")
    def _plan(ctx, args):
        ctx["app"].status = f"计划 {ctx['session'].plan.get('status')}"
        return True

    @commands.register("/steps", hint="查看步骤", source="builtin")
    def _steps(ctx, args):
        ctx["app"].status = f"步骤 {len(ctx['session'].steps)}"
        return True

    @commands.register("/clear", hint="清空会话", source="builtin")
    def _clear(ctx, args):
        ctx["session"].messages.clear()
        ctx["session"].plan = {"title": "", "complexity": "low", "content": "", "status": "empty"}
        ctx["session"].steps = []
        ctx["app"].task = ""
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


def _handle_tool_confirm(llm: LLM, app: "ui.TuiApp", session, call: dict) -> dict:
    """确认框占用输入区；处理三种选择"""
    app.pending_tool = {"name": call.get("name"), "arguments": call.get("arguments") or {}}
    app.confirm_index = 0
    app.confirm_reject_edit = False
    app.reject_buffer = []
    result = app.read_line(config=None)
    app.pending_tool = None
    app.confirm_index = 0
    app.confirm_reject_edit = False
    if result == "__ALLOW_ONCE__":
        return llm.execute_approved_tool(call)
    if result == "__ALLOW_ALWAYS__":
        policy.add_always_allow(call.get("name"), call.get("arguments") or {})
        return llm.execute_approved_tool(call)
    # 拒绝：result 为 error 文案
    return {
        "role": "tool",
        "tool_call_id": call.get("id"),
        "tool_name": call.get("name"),
        "content": result or policy.default_reject_message(""),
        "type": "tool",
    }


def _agent_turn(llm: LLM, session, user_text: str, app: "ui.TuiApp") -> None:
    log.info("任务开始: %s", user_text)
    session.add_message("user", user_text, type="task")
    mode = app.mode
    _sync(app, session, task=user_text, status=f"{policy.MODE_LABELS.get(mode, mode)} · 分析")

    # 系统提示词：从 core/prompts 渲染；chat 窗口可见注入消息
    system_prompt_text = get_system_prompt(
        config=llm.config,
        session=session,
        project_identity=getattr(session, "project_identity", None),
    )
    seg_names = list(prompt_loader.system_prompt_files(llm.config))
    if not any(m.get("type") == "system_prompt" for m in session.messages):
        session.add_message(
            "system",
            "注入系统提示词",
            type="system_prompt",
            files=seg_names or ["system_prompt.md"],
        )
        log.info("注入系统提示词: %s", ", ".join(seg_names or ["system_prompt.md"]))
        _sync(app, session, task=user_text, status="注入系统提示词")

    complexity = llm.judge_complexity(user_text)
    if complexity == "high":
        log.info("复杂任务，进入计划流程")
        _sync(app, session, task=user_text, status="生成计划")
        plan = llm.generate_plan(user_text)
        session.set_plan(plan.get("title", "任务计划"), plan.get("content", ""), complexity=plan.get("complexity", "high"))
        confirmed = False
        for _edit_round in range(3):
            choice = app.choose(
                [("1", "确认计划"), ("2", "直接改计划"), ("3", "反馈修改"), ("0", "取消")],
                prompt="计划已生成",
            ).strip()
            if choice == "0":
                session.add_message("assistant", "用户取消任务。")
                _sync(app, session, task=user_text, status="已取消")
                return
            if choice == "2":
                lines = []
                while True:
                    line = app.read_line("计划> ", config=llm.config)
                    if line.strip() == "END":
                        break
                    lines.append(line)
                session.plan.update(llm.confirm_plan(session.plan, "manual_edit", feedback="\n".join(lines)))
            elif choice == "3":
                fb = app.read_line("反馈> ", config=llm.config)
                session.plan.update(llm.confirm_plan(session.plan, "llm_modify", feedback=fb))
            elif choice == "1":
                session.plan.update(llm.confirm_plan(session.plan, "confirm"))
                session.update_plan_status("confirmed")
                session.save_longterm()
                confirmed = True
                break
            else:
                session.add_message("system", "请输入 0/1/2/3", type="help")
                _sync(app, session, task=user_text, status="请重新选择计划操作")
                continue
            # 2/3 修改后必须二次确认，禁止直接执行
            again = app.choose(
                [("1", "确认并执行"), ("2", "继续修改"), ("0", "取消")],
                prompt="计划已修改",
            ).strip()
            if again == "0":
                session.add_message("assistant", "用户取消任务。")
                _sync(app, session, task=user_text, status="已取消")
                return
            if again == "1":
                session.plan.update(llm.confirm_plan(session.plan, "confirm"))
                session.update_plan_status("confirmed")
                session.save_longterm()
                confirmed = True
                break
            # again == "2" 或非法 → 回到 1/2/3 菜单继续改
        if not confirmed:
            session.add_message("assistant", "计划未确认，任务中止。")
            _sync(app, session, task=user_text, status="计划未确认")
            return
        steps = llm.generate_steps(user_text, session.plan.get("content", ""))
        session.set_steps(steps)
        session.add_message("assistant", "计划确认，开始执行。", type="plan")
        _sync(app, session, task=user_text, status="执行步骤")

    for round_no in range(12):
        _sync(app, session, task=user_text, status=f"推理 · 第{round_no + 1}轮")
        payload = session.build_messages(extra_system=system_prompt_text)
        llm.meter.measure_context(payload)
        # 代码编写默认模型（设置页可指定）
        code_model = llm.config.get_task_model("code") if hasattr(llm.config, "get_task_model") else None
        response = llm.chat(payload, tools=register.get_tool_defs(), model=code_model)
        app.token_meter = llm.meter
        if response.get("error"):
            log.error("第%d轮推理返回错误: %s", round_no + 1, response["error"])
            session.add_message("assistant", response["error"])
            _sync(app, session, task=user_text, status="出错")
            return
        if response.get("content") and not response.get("tool_calls"):
            session.add_message("assistant", response["content"])
        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            log.info("任务完成（共%d轮推理）", round_no + 1)
            session.maybe_compress(llm_fn=lambda p: llm.chat([{"role": "user", "content": p}]).get("content", ""))
            _sync(app, session, task=user_text, status="就绪")
            return

        pending = []
        allowed_results = []
        log.debug("第%d轮返回 %d 个工具调用", round_no + 1, len(tool_calls))
        for call in tool_calls:
            action, reason = llm.evaluate_tool(call.get("name"), call.get("arguments") or {})
            if action == policy.ALLOW:
                allowed_results.append(llm.execute_approved_tool(call))
            elif action == policy.CONFIRM:
                pending.append(call)
            else:
                allowed_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "tool_name": call.get("name"),
                        "content": policy.default_reject_message(reason),
                        "type": "tool",
                    }
                )

        if tool_calls:
            # DeepSeek Tool Calls：保留 assistant 工具轮（含 content + tool_calls）
            session.add_message(
                "assistant",
                response.get("content") or "",
                type="tool_call",
                tool_calls=[
                    {
                        "id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                        "name": c.get("name"),
                        "arguments": c.get("arguments") or {},
                        "type": c.get("type") or "function",
                    }
                    for c in tool_calls
                ],
            )

        for item in allowed_results:
            session.add_message(
                "tool",
                item["content"],
                type="tool",
                tool_name=item.get("tool_name"),
                tool_call_id=item.get("tool_call_id"),
            )
        _sync(app, session, task=user_text, status=f"工具 {len(allowed_results)} 完成 · 待确认 {len(pending)}")

        for call in pending:
            result = _handle_tool_confirm(llm, app, session, call)
            session.add_message(
                "tool",
                result["content"],
                type="tool",
                tool_name=result.get("tool_name"),
                tool_call_id=result.get("tool_call_id") or call.get("id"),
            )
            _sync(app, session, task=user_text, status=f"确认完成 · {call.get('name')}")

        app.token_meter = llm.meter
        _sync(app, session, task=user_text, status="继续")

    session.add_message("assistant", "达到最大工具轮次。")
    log.warn("达到最大工具轮次（12轮），任务中止")
    _sync(app, session, task=user_text, status="轮次上限")


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
    _ = tools_mod
    _register_commands(llm, session, config, app)

    app.enter()
    try:
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
                result = commands.execute(text, {"llm": llm, "session": session, "config": config, "app": app})
                app.bind_config(config)
                llm.config = config
                app.token_meter = llm.meter
                if result == "EXIT":
                    session.save_longterm()
                    break
                if isinstance(result, dict) and result.get("reason") in ("unknown", "no_handler"):
                    session.add_message("system", f"命令问题: {result}", type="help")
                _sync(app, session)
                continue
            _agent_turn(llm, session, text, app)
    except KeyboardInterrupt:
        log.info("用户中断（Ctrl+C）")
        session.save_longterm()
    finally:
        app.leave()
        print("BAWCode 已退出。")
        log.info("BAWCode 已退出")


if __name__ == "__main__":
    main()
