#该部分为程序的主逻辑，调用各个模块实现完整功能
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import commands
from core import memory as memory_mod
from core import policy
from core import register
from core import tools as tools_mod  # noqa: F401
from core import ui
from core.config import Config
from core.llm import SYSTEM_PROMPT, LLM
from core.log import get_logger

log = get_logger("main")


def _sync(app: "ui.TuiApp", session, task: str = "", status: str = "") -> None:
    if status:
        app.status = status
    app.mode = getattr(app, "mode", policy.MODE_AUTO)
    app.refresh_from_session(session, task=task or None)
    app.tool_count = len(register.list_tools())


def _register_commands(llm: LLM, session, config: Config, app: "ui.TuiApp") -> None:
    def _ctx():
        return {"llm": llm, "session": session, "config": config, "app": app}

    @commands.register("/help", hint="帮助", source="builtin")
    def _help(ctx, args):
        ctx["app"].messages.append({"role": "system", "content": commands.help_text(), "type": "help"})
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
            ctx["app"].messages.append({"role": "system", "content": "\n".join(lines), "type": "help"})
            return True
        row = ctx["config"].switch_model(name)
        if not row:
            ctx["app"].messages.append({"role": "system", "content": f"未找到模型: {name}", "type": "help"})
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
            ctx["app"].messages.append({"role": "system", "content": "模式: auto | manual | full", "type": "help"})
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
            ctx["app"].messages.append({"role": "system", "content": "主题: " + ", ".join(themes), "type": "help"})
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
        ctx["app"].messages.append({"role": "system", "content": "工具:\n" + "\n".join(lines), "type": "help"})
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
        ctx["app"].messages.append({"role": "system", "content": commands.help_text(), "type": "help"})
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

    complexity = llm.judge_complexity(user_text)
    if complexity == "high":
        log.info("复杂任务，进入计划流程")
        _sync(app, session, task=user_text, status="生成计划")
        plan = llm.generate_plan(user_text)
        session.set_plan(plan.get("title", "任务计划"), plan.get("content", ""), complexity=plan.get("complexity", "high"))
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
        else:
            session.plan.update(llm.confirm_plan(session.plan, "confirm"))
            session.update_plan_status("confirmed")
            session.save_longterm()
        steps = llm.generate_steps(user_text, session.plan.get("content", ""))
        session.set_steps(steps)
        session.add_message("assistant", "计划确认，开始执行。", type="plan")
        _sync(app, session, task=user_text, status="执行步骤")

    for round_no in range(12):
        _sync(app, session, task=user_text, status=f"推理 · 第{round_no + 1}轮")
        payload = session.build_messages(extra_system=SYSTEM_PROMPT)
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
        if response.get("content"):
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

        for item in allowed_results:
            session.add_message("tool", item["content"], type="tool", tool_name=item.get("tool_name"))
        _sync(app, session, task=user_text, status=f"工具 {len(allowed_results)} 完成 · 待确认 {len(pending)}")

        for call in pending:
            result = _handle_tool_confirm(llm, app, session, call)
            session.add_message("tool", result["content"], type="tool", tool_name=result.get("tool_name"))
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
    log.info("BAWCode 启动 · 配置=%s · 模型=%s", config.config_path, config.model_name)
    session = memory_mod.init_session(config)
    llm = LLM(config)
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
                    session.add_message("system", f"命令问题: {result}")
                    app.messages.append({"role": "system", "content": str(result), "type": "help"})
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
