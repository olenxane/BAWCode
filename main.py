"""main.py:程序核心入口"""
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import commands
from core import config as config_mod
from core import file as file_mod
from core import hooks
from core import loop as loop_mod
from core import mcp as mcp_mod
from core import memory as memory_mod
from core import pasteboard as pasteboard_mod
from core import plugins as plugins_mod
from core import policy
from core import project_identity
from core import register
from core import session_store
from core import snapshot as snapshot_mod
from core import statistics as statistics_mod
from core import subagent as subagent_mod
from core import tools as tools_mod  # noqa: F401
from core import ui
from core.config import Config
from core.llm import CANCELLED, LLM
from core.log import get_logger

log = get_logger("main")


def _init_usage_statistics():
    try:
        return statistics_mod.initialize()
    except Exception as exc:
        log.debug("使用统计初始化失败: %r", exc)
        return None


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
        runner = getattr(ctx["app"], "agent_runner", None)
        if runner is not None:
            runner.discard_pending_turn()
        sess.save_session()
        sess.switch_to(data)
        ctx["app"]._tree_follow_tail = True  # 切换后贴底显示恢复的消息
        ctx["app"].status = f"已切换: {sess.session_title or sess.session_id}"
        return True

    @commands.register("/new", hint="新建会话 · 当前会话已保存", source="builtin")
    def _new(ctx, args):
        sess = ctx["session"]
        runner = getattr(ctx["app"], "agent_runner", None)
        if runner is not None:
            runner.discard_pending_turn()
        sess.save_session()
        sess.start_new_session()
        ctx["app"].task = ""
        ctx["app"].status = "新会话已开启"
        return True

    @commands.register("/clear", hint="清空当前会话 · 恢复为空对话", source="builtin")
    def _clear(ctx, args):
        runner = getattr(ctx["app"], "agent_runner", None)
        if runner is not None:
            runner.discard_pending_turn()
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

    @commands.register("/effort", hint="设置当前模型思考强度 none|low|medium|high|xhigh|max", usage="/effort <none|low|medium|high|xhigh|max>", source="builtin")
    def _effort(ctx, args):
        choices = config_mod.THINKING_OPTIONS
        arg = (args or "").strip().lower()
        row = ctx["config"].find_model(ctx["config"].model_name)
        if row is None:
            _echo(ctx, "当前模型不可用，无法设置思考强度")
            return True
        if not arg:
            _echo(ctx, f"当前模型思考强度: {row.get('thinking_effort') or 'none'}")
            return True
        if arg not in choices:
            _echo(ctx, "用法: /effort <none|low|medium|high|xhigh|max>")
            return True
        updated = ctx["config"].update_model(
            row["provider_id"], row["model_id"],
            {"thinking_effort": arg, "thinking_effort_explicit": True},
        )
        if updated is None:
            _echo(ctx, "当前模型更新失败，思考强度未改变")
            return True
        ctx["config"].save()
        ctx["llm"].refresh_from_config(ctx["config"])
        ctx["app"].bind_config(ctx["config"])
        ctx["app"].status = f"当前模型思考强度: {arg}"
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

    @commands.register("/mode", hint="访问模式 confirm|edit|auto|full · 也可按 ~", usage="/mode [confirm|edit|auto|full]", source="builtin")
    def _mode(ctx, args):
        arg = (args or "").strip().lower()
        if arg == "manual":
            arg = policy.MODE_CONFIRM
        if not arg:
            arg = ctx["app"].cycle_mode()
        elif arg not in policy.MODES:
            _echo(ctx, "模式: confirm（手动确认）| edit（自动接收编辑）| auto（自动审批）| full（完全访问）")
            return True
        else:
            ctx["app"].mode = arg
        ctx["config"].data.setdefault("ui", {})["mode"] = ctx["app"].mode
        ctx["config"].mode = ctx["app"].mode
        ctx["config"].save()
        ctx["app"].status = policy.MODE_LABELS[ctx["app"].mode]
        return True

    @commands.register("/security", hint="关闭删除安全护栏 · /security [on|off]", usage="/security [on|off]", source="builtin")
    def _security(ctx, args):
        security_cfg = ctx["config"].data.setdefault("security", {})
        arg = (args or "").strip().lower()
        if arg not in ("", "on", "off"):
            _echo(ctx, "用法: /security [on|off]")
            return True
        if arg:
            security_cfg["delete_guard"] = arg == "on"
            ctx["config"].save()
        enabled = bool(security_cfg.get("delete_guard", True))
        state = "开启" if enabled else "关闭"
        ctx["app"].status = f"删除安全护栏{state}"
        _echo(ctx, f"删除安全护栏已{state}：" + ("删除命令会移入项目回收站" if enabled else "删除命令将直接执行"))
        return True

    @commands.register("/theme", hint="主题 /theme [名称]", usage="/theme [dark|ocean]", source="builtin")
    def _theme(ctx, args):
        name = (args or "").strip()
        themes = ctx["config"].list_themes()
        if not name:
            _echo(ctx, "主题: " + ", ".join(themes))
            return True
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
            live = subagent_mod.task_status(ctx["config"], sess, str(rec.get("id") or "")) or rec
            label = subagent_mod.status_label(str(live.get("status") or rec.get("status")))
            phase = str(live.get("phase") or "").strip()
            if phase and str(live.get("status") or "") in ("queued", "running", "cancelling"):
                label = f"{label} · {phase}"
            task_brief = " ".join(str(rec.get("task") or "").split())[:40]
            lines.append(
                f"  #{rec.get('id')} · {rec.get('role')} · {label}"
                f" · {live.get('rounds', rec.get('rounds', 0))}轮 · {task_brief}"
            )
        lines.append("角色:")
        lines.append(subagent_mod.roles_listing())
        lines.append(
            "提示: 子代理为异步派发——task 立即返回 id，wait_subagent 等待（超时返回进度），"
            "cancel_subagent 取消，query_subagent 看轨迹，完成时自动注入通知"
        )
        _echo(ctx, "\n".join(lines))
        return True

    @commands.register(
        "/skill", hint="技能系统 · 列表/查看/重载/生成", usage="/skill [名称|reload|summon [global|project]]", source="builtin"
    )
    def _skill(ctx, args):
        from core import skills as skills_mod

        arg = (args or "").strip()
        parts = arg.split(maxsplit=1)
        if parts and parts[0].lower() == "summon":
            runner = getattr(ctx["app"], "agent_runner", None)
            if runner is not None and runner.busy:
                _echo(ctx, "本轮对话进行中：请等待完成后再生成技能")
                return True
            scope = (parts[1].strip().lower() if len(parts) > 1 else "") or "global"
            sess = ctx["session"]
            origin_id = getattr(sess, "session_id", "")
            # 生成期间先立一个可见节点（type=help 不进 API，故不影响请求上下文与缓存前缀）
            node = sess.add_message(
                "system",
                f"[技能生成中] 正在按当前对话生成技能（落盘 {scope}）…本条完成后更新为结果",
                type="help",
            )
            ctx["app"].status = f"技能生成中（{scope}）"
            info = skills_mod.summon(sess, ctx["llm"], ctx["config"], scope=scope)
            if info.get("ok"):
                text = (
                    f"技能已生成: {info['name']}（落盘 {info['scope']}）\n{info['path']}\n"
                    f"重扫后可用技能 {info.get('total', 0)} 个 · 任务匹配时经 load_skill 加载"
                )
                if info.get("duplicate"):
                    text = f"重复提交，未再次请求 API：{text}"
                ctx["app"].status = f"技能已生成: {info['name']}"
            else:
                text = f"技能生成失败: {info.get('error')}"
                ctx["app"].status = "技能生成失败"
            if getattr(sess, "session_id", "") != origin_id:
                # 生成期间用户切换了会话：不把反馈写进新会话，结果经命令返回值回给调用方
                log.info("技能生成完成时会话已切换（%s → %s），反馈不写入会话", origin_id, sess.session_id)
                return text
            node["content"] = text
            return text
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
            lines.append("提示: /undo 回滚最近回合 · /undo <序号> 回滚指定回合 · 非本回合修改的文件会跳过")
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

    @commands.register("/clear-trash", hint="清空项目回收站（不可逆）", source="builtin")
    def _clear_trash(ctx, args):
        count, size = snapshot_mod.clear_trash(ctx["config"], ctx["session"].project_id)
        if count:
            _echo(ctx, f"回收站已清空: {count} 项，释放 {size} 字节")
        else:
            _echo(ctx, "回收站里没有东西")
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
        from core import workflow as workflow_mod
        #直接对话路径不加载工作流模块

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


def _handle_mode_switch(app: "ui.TuiApp", config, payload: dict, cancelled=None) -> dict:
    """模式切换申请：ask 面板裁决；批准后切换（单一事实源 config.mode）并落盘。

    返回 {"approved": bool, "detail": str}，由 request_mode_switch 工具转成给模型的回执。"""
    target = str((payload or {}).get("mode") or "").strip().lower()
    if target not in policy.MODES:
        return {"approved": False, "detail": f"未知模式: {target}"}
    label = policy.MODE_LABELS.get(target, target)
    current_label = policy.MODE_LABELS.get(config.mode, config.mode)
    reason = str((payload or {}).get("reason") or "").strip()
    question = f"Agent 申请切换到「{label}」模式（当前：{current_label}）"
    if reason:
        question += f" · 理由: {reason}"
    with ui.DIALOG_LOCK:
        req = app.request_ui(
            "ask",
            {
                "question": question,
                "options": [
                    {"title": f"允许切换到{label}", "description": "立即切换并写入配置"},
                    {"title": f"保持{current_label}", "description": "拒绝本次申请，模式不变"},
                ],
            },
        )
        result = app.wait_ui(req, cancelled)
    if not isinstance(result, dict):
        return {"approved": False, "detail": "交互中断"}
    status = str(result.get("status") or "")
    if status == "cancelled":
        return {"approved": False, "detail": "回合已中断"}
    if status == "timeout":
        return {"approved": False, "detail": "等待超时，按拒绝处理"}
    if status == "declined":
        return {"approved": False, "detail": "用户跳过了本次申请"}
    if str(result.get("answer") or "").strip() != f"允许切换到{label}":
        return {"approved": False, "detail": "用户选择了拒绝"}
    app.mode = target
    config.mode = target  # 单一事实源 data.ui.mode（Config.mode property）
    config.save()
    app.status = f"已切换到{label}"
    log.info("模式切换申请获批: %s -> %s", current_label, target)
    return {"approved": True, "detail": "配置已保存。"}


def _handle_tool_confirm(llm: LLM, app: "ui.TuiApp", session, call: dict, cancelled=None) -> dict:
    """工具确认：先经 tool_confirm 扩展点（插件/外部接口可代答），无处理时
    经 UI 请求桥在主线程弹面板（↑↓ 选择）；取消时按默认拒绝回注"""
    def rejected():
        return {"role": "tool", "tool_call_id": call.get("id"), "tool_name": call.get("name"),
                "content": policy.default_reject_message(""), "type": "tool"}

    if callable(cancelled) and cancelled():
        return rejected()
    confirm_payload = {"name": call.get("name"), "arguments": call.get("arguments") or {},
                       "request_id": uuid.uuid4().hex, "cancelled": cancelled}
    hooked = hooks.call_hook("tool_confirm", confirm_payload, default=None)
    if callable(cancelled) and cancelled():
        return rejected()
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
        # 对话框串行：并发子代理同时请求确认时逐个弹面板
        with ui.DIALOG_LOCK:
            req = app.request_ui(
                "confirm",
                {"name": str(call.get("name") or ""), "arguments": call.get("arguments") or {}},
            )
            result = app.wait_ui(req, cancelled)
    finally:
        app.pending_tool = None
        closed_payload = {key: value for key, value in confirm_payload.items() if key != "cancelled"}
        hooks.collect_hook("tool_confirm_closed", dict(closed_payload, phase="closed"))
    if callable(cancelled) and cancelled():
        return rejected()
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
    with ui.DIALOG_LOCK:
        req = app.request_ui("choose", {"options": options, "prompt": prompt})
        return app.wait_ui(req, cancelled)


def _bridge_line(app: "ui.TuiApp", prompt: str, config, cancelled=None):
    with ui.DIALOG_LOCK:
        req = app.request_ui("line", {"prompt": prompt, "config": config})
        return app.wait_ui(req, cancelled)


def _launch_workflow_editor(ctx, name: str = "") -> None:
    """子进程拉起 PySide6 工作流编辑器（独立事件循环，不阻塞 TUI）"""
    import PySide6#此处不进行错误处理，pyside6作为必须依赖库不会发生不存在的情况，不要过度防御
    import subprocess

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = [sys.executable, "-m", "gui.workflow_editor"]
    if name:
        cmd.append(name)
    subprocess.Popen(cmd, cwd=str(_ROOT), **kwargs)
    _echo(ctx, f"工作流编辑器已启动: {name or '（新工作流）'}")


def _headless_task_wait_seconds(config) -> float:
    """无头模式等待后台子代理的上限秒数（subagent.headless_wait_timeout，默认 600）"""
    raw = ((getattr(config, "data", None) or {}).get("subagent") or {}).get("headless_wait_timeout", 600)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 600.0


def _before_turn(text: str) -> str:
    """before_turn 变换链：插件可改写本轮输入（返回 {user_text: ...} 生效），不改写则原样返回"""
    before = hooks.call_hook("before_turn", {"user_text": text}, default=None)
    rewritten = before.get("user_text") if isinstance(before, dict) else None
    return rewritten if isinstance(rewritten, str) and rewritten.strip() else text


def _after_turn(session, user_text: str, failed: bool = False) -> None:
    """after_turn 观察链：回合结束通知（正常/中断/异常路径都触发），响应取最后一条 assistant 消息；
    请求失败时响应置空，避免插件（qbridge 等）误把错误答复当成正常完成内容"""
    last = None
    if not failed:
        last = next((m for m in reversed(session.messages) if m.get("role") == "assistant"), None)
    hooks.collect_hook(
        "after_turn",
        {
            "user_text": user_text,
            "response": (last or {}).get("content", "") if not failed else "",
            "session_id": getattr(session, "session_id", ""),
        },
    )


def _agent_turn(llm: LLM, session, user_text: str, app: "ui.TuiApp", runner=None) -> None:
    """回合入口：无论正常结束/出错/中断，回合末执行上下文维护（剥离+预算外置），
    并清掉流式树尾消息（partial 丢弃，与在途回合丢弃语义一致）与子代理运行时/直播槽"""
    snap = None
    stats = getattr(app, "usage_statistics", None)
    meter = getattr(llm, "meter", None)
    try:
        stats_handle = stats.begin_turn(session.session_id, llm.config.model_name) if stats else None
    except Exception as exc:
        log.debug("使用统计回合初始化失败: %r", exc)
        stats_handle = None
    api_tokens_before = getattr(meter, "api_total_tokens", 0)
    try:
        _agent_turn_impl(llm, session, user_text, app, runner=runner)
    except Exception as exc:
        app.last_turn_error = str(exc)
        log.error("回合执行异常: %r", exc)
        if runner is not None:
            runner.last_error = str(exc)
            runner._failed_rollback = True
        else:
            raise
    finally:
        try:
            snap = snapshot_mod.end_turn()  # 回合快照落盘（中断/异常路径同样收尾；空回合无副作用）
        except Exception as exc:
            log.error("回合快照收尾失败: %r", exc)
        # Esc 打断保留本轮上下文；用户新消息丢弃挂起上下文；请求失败仅挂起，不立即清理或还原文件。
        pending_to_discard = None
        failed_pending = False
        interrupted = False
        if runner is not None:
            with runner._lock:
                pending = runner.pending_failed_turn
                interrupted = runner._esc_interrupt
                abandon = runner._abandon_pending
                failed = runner._failed_rollback
                runner._esc_interrupt = False
                runner._abandon_pending = False
                runner._failed_rollback = False

                if interrupted:
                    # Esc 打断：本轮消息全部留在上下文，此前挂起的失败轮次随之收束为普通历史
                    runner.pending_failed_turn = None
                elif abandon and pending:
                    if snap:
                        pending["snapshots"].append(snap)
                    pending_to_discard = pending
                    runner._discarded_turn_ids.update(pending.get("turn_ids") or [pending.get("turn_id")])
                    runner.pending_failed_turn = None
                elif abandon:
                    # 用户在错误重试等待窗口提交新消息：当前错误回合尚未进入挂起态，但仍按原始回合起点一并丢弃。
                    pending_to_discard = {
                        "start_index": runner.turn_start_index,
                        "snapshots": [snap] if snap else [],
                    }
                    if runner.active_turn_id:
                        runner._discarded_turn_ids.add(runner.active_turn_id)
                elif (
                    runner._continuing_pending
                    and pending
                    and runner._active_same_turn_call
                    and getattr(app, "_turn_injected_users", None)
                ):
                    if snap:
                        pending["snapshots"].append(snap)
                    pending_to_discard = pending
                    runner._discarded_turn_ids.update(pending.get("turn_ids") or [pending.get("turn_id")])
                    runner.pending_failed_turn = None
                elif failed:
                    runner.last_turn_failed = True
                    failed_pending = True
                    app.last_failed_text = str(getattr(app, "turn_user_text", "") or user_text)
                    if pending:
                        turn_ids = pending.setdefault("turn_ids", [])
                        if runner.active_turn_id and runner.active_turn_id not in turn_ids:
                            turn_ids.append(runner.active_turn_id)
                        if snap:
                            pending["snapshots"].append(snap)
                    else:
                        pending = {
                            "start_index": runner.turn_start_index,
                            "turn_ids": [runner.active_turn_id] if runner.active_turn_id else [],
                            "snapshots": [snap] if snap else [],
                            "turn_id": runner.active_turn_id,
                        }
                        runner.pending_failed_turn = pending
            if interrupted:
                try:
                    _keep_interrupted_turn(session, app, runner)
                except Exception as exc:
                    log.error("回合打断收尾失败: %r", exc)
            elif pending_to_discard is not None:
                try:
                    _rollback_failed_turn(session, app, runner, pending=pending_to_discard)
                except Exception as exc:
                    log.error("挂起回合丢弃失败: %r", exc)
            elif failed_pending:
                app.last_failed_text = None
                try:
                    _hold_failed_turn(session, app, runner)
                except Exception as exc:
                    log.error("失败回合挂起失败: %r", exc)
            else:
                app.last_failed_text = None
        if runner is not None:
            with runner._lock:
                runner.active_turn_id = None
                runner._active_dispatch_origin = None
                runner._active_is_background_call = False
                runner._active_same_turn_call = False
                runner._continuing_pending = False

        # 挂起时不整理上下文：保留完整失败轮次，供命令/模型切换与后台通知后重用。
        if not failed_pending and pending_to_discard is None:
            try:
                session.finalize_turn()
            except Exception as exc:
                log.error("回合末上下文维护失败: %r", exc)
        if stats and stats_handle:
            try:
                stats.end_turn(
                    stats_handle,
                    llm.config.model_name,
                    max(0, getattr(meter, "api_total_tokens", 0) - api_tokens_before),
                    getattr(meter, "turn_peak_context_tokens", getattr(meter, "context_tokens", 0)),
                )
            except Exception as exc:
                log.debug("使用统计更新失败: %r", exc)
        subagent_mod.unbind_runtime()
        app.streaming_msg = None
        # 回合结束只保留仍在跑的后台子代理直播槽（异步任务跨回合存活），其余清掉
        subagent_mod.retain_running_slots(app, session)
        app.phase_hint = ""
        app.turn_start_time = 0.0
        if runner is not None:
            with runner._lock:
                runner._turn_active = False


def _keep_interrupted_turn(session, app, runner) -> None:
    """Esc 打断收尾：本轮已记录的消息留在上下文，不还原文件、不回填输入框。

    文件快照保持可用（/undo 与树模式 Ctrl+Z 仍可回退本轮改动）；用户输入不回填，
    避免与上下文里的同一条消息重复发送。"""
    start = int(getattr(runner, "turn_start_index", 0))
    kept = max(0, len(session.messages) - start)
    app.last_failed_text = None
    try:
        _sync(app, session, status=f"已打断 · 保留本轮上下文（{kept} 条消息）", render=False)
    except Exception:
        pass
    log.info("Esc 打断：保留本轮消息 %d 条", kept)


def _hold_failed_turn(session, app, runner) -> None:
    """请求失败时保留本轮消息、文件状态与快照，等待用户操作或后台通知唤醒。"""
    state = runner.pending_failed_turn
    count = len(session.messages) - int((state or {}).get("start_index", runner.turn_start_index))
    try:
        _sync(
            app,
            session,
            status=f"请求失败 · 已挂起本轮上下文（保留 {max(0, count)} 条消息）· Ctrl+Y 重试；新消息将保留上下文续跑，Esc 收束",
            render=False,
        )
    except Exception:
        pass
    log.info("请求失败回合已挂起：保留消息 %d 条", max(0, count))


def _rollback_failed_turn(session, app, runner, snap=None, pending=None) -> None:
    """显式丢弃已挂起的失败回合：截断会话并按逆序还原挂起期间的文件快照，不回填输入。"""
    state = pending or getattr(runner, "pending_failed_turn", None)
    start = int((state or {}).get("start_index", getattr(runner, "turn_start_index", -1)))
    removed = 0
    if 0 <= start <= len(session.messages):
        removed = len(session.messages) - start
        del session.messages[start:]
    file_note = ""
    snapshots = list((state or {}).get("snapshots") or [])
    if snap:
        snapshots.append(snap)
    for item in reversed(snapshots):
        if not isinstance(item, dict) or not item.get("seq"):
            continue
        try:
            res = snapshot_mod.undo(session.config, session.project_id, session.session_id, item["seq"])
            if res:
                file_note += f" · 文件还原{len(res['restored'])} 删除{len(res['deleted'])} 跳过{len(res['skipped'])}"
        except Exception as exc:
            log.warn("挂起回合文件还原出错: %r", exc)
    app.last_failed_text = None
    try:
        _sync(app, session, status=f"已丢弃挂起回合（移除 {removed} 条消息{file_note}）", render=False)
    except Exception:
        pass
    log.info("挂起回合已丢弃：移除消息 %d 条%s", removed, file_note)


def _keep_pending_turn(session, app, pending=None) -> None:
    """Esc 收束挂起失败回合：消息留在上下文，仅清挂起态；不还原文件、不回填输入。"""
    state = pending or {}
    start = int(state.get("start_index", 0))
    kept = max(0, len(session.messages) - start)
    app.last_failed_text = None
    try:
        _sync(app, session, status=f"已收束挂起回合 · 保留本轮上下文（{kept} 条消息）", render=False)
    except Exception:
        pass
    log.info("挂起回合已收束：保留消息 %d 条", kept)


def _agent_turn_impl(llm: LLM, session, user_text: str, app: "ui.TuiApp", runner=None) -> None:
    """一个 agent 回合；runner 存在时在后台线程运行——交互弹窗经 UI 请求桥
    移交主线程执行，取消检查贯穿各检查点（中断=结束在途回合并保留其已记录消息）。"""

    def _cancelled() -> bool:
        return runner is not None and llm.cancelled()

    resolved_files = file_mod.resolve_references(user_text, getattr(session, "workspace", None))
    expanded_user_text = resolved_files["text"]
    file_refs = resolved_files["references"]
    failed_refs = [item for item in file_refs if item.get("status") not in ("loaded", "duplicate")]
    if failed_refs:
        names = ", ".join(str(item.get("path") or item.get("token") or "") for item in failed_refs[:3])
        app.status = f"文件引用未加载: {names}"

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
        session.add_message("system", "[本轮已被用户打断]", type="help")
        _syncq(status="已中断")

    def _on_delta(kind: str, piece: str) -> None:
        # 流式回调（agent 线程）：只改 UI 状态不渲染——流式对象挂在 app.streaming_msg，不进 session.messages，帧循环每帧读它画树尾直播
        if kind == "reset":
            app.streaming_msg = None
            hooks.collect_hook("stream_delta", {"kind": kind, "piece": piece})
            return
        if kind not in {"reasoning", "content"}:
            return
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
    # 回合起点：消息序号供 Esc 打断时统计保留条数；时间与 token 基线供提示行显示用时/本轮消耗
    if runner is not None:
        runner.turn_start_index = len(session.messages)
        if runner.pending_failed_turn is not None and runner._continuing_pending:
            runner.active_turn_id = runner.pending_failed_turn.get("turn_id")
        else:
            runner.active_turn_id = uuid.uuid4().hex
        tools_mod.set_background_origin(runner.active_turn_id)
        runner._esc_interrupt = False
    app.turn_start_time = time.monotonic()
    app.turn_user_text = user_text
    if app.token_meter is not None:
        app.token_meter.begin_turn()  # 复位本轮本地分词累计（提示行实时 token）
    # 粘贴附图（输入框占位符=图片路径，提交时入队）：与正文一并落为多模态数组；
    # 非视觉模型不编码图片块（路径作为普通文本保留在正文里）
    pending = getattr(runner, "turn_images", None) if runner is not None else None
    if pending and getattr(llm.config, "supports_vision", True):
        parts, notes = memory_mod.build_image_parts(pending)
        content: list = [{"type": "text", "text": expanded_user_text}]
        if notes:
            content[0]["text"] += "\n[图片说明] " + "；".join(notes)
        content.extend(parts)
        session.add_message("user", content, type="task", file_refs=file_refs)
    else:
        session.add_message("user", expanded_user_text, type="task", file_refs=file_refs)
    # 回合快照：绑定上下文 + 轻量清单对账基线（写入工具经 capture_before 留底）；
    # msg_index=本回合用户消息绝对序号，树模式 Ctrl+Z 按它联动文件回滚
    snapshot_mod.begin_turn(
        llm.config, session.project_id, session.session_id, user_text,
        msg_index=len(session.messages) - 1,
    )
    mode = app.mode
    _syncq(task=user_text, status=f"{policy.MODE_LABELS.get(mode, mode)} · 分析")
    # 阶段提示自用户消息发出即常驻（首轮思考），直至回合结束由 finally 清空
    _set_phase("思考中")
    if _cancelled():
        _aborted()
        return

    # 回合驱动：未启用工作流=直接对话（core/loop 回合运行时）；启用时按data/workflows/<active>.json 的节点链运行
    retry_wait = getattr(app, "wait_llm_retry", None)  # 无头桩无此方法：不提供手动重试
    io = loop_mod.TurnIO(
        status=_set_status,
        choose=lambda options, prompt: _bridge_choose(app, options, prompt, _cancelled),
        line=lambda prompt: _bridge_line(app, prompt, llm.config, _cancelled),
        cancelled=_cancelled,
        on_delta=_on_delta,
        clear_stream=lambda: setattr(app, "streaming_msg", None),
        tool_confirm=lambda call: _handle_tool_confirm(llm, app, session, call, _cancelled),
        phase=_set_phase,
        wait_llm_retry=(lambda text: retry_wait(text, _cancelled)) if retry_wait else None,
    )
    turn = loop_mod.TurnContext(
        session=session, llm=llm, app=app, config=llm.config, user_text=expanded_user_text, io=io
    )
    # 子代理异步派发运行时由工具调用线程捕获为快照，回合末解绑不影响后台任务
    subagent_mod.bind_runtime(
        llm=llm,
        config=llm.config,
        io=io,
        app=app,
        session=session,
        runner=runner,
        origin_turn_id=getattr(runner, "active_turn_id", None),
        # 后台任务的提权确认：交给主线程确认面板，批准后用任务的 LLM 执行
        confirm_bridge=(
            (lambda task_llm, call, cancelled: _handle_tool_confirm(task_llm, app, session, call, cancelled))
            if runner is not None else None
        ),
        # 后台子代理申请切换访问模式：同一用户面板裁决；批准后模式立即生效，其权限随之提升
        mode_bridge=lambda payload, cancelled: _handle_mode_switch(app, llm.config, payload, cancelled),
    )


    def _bridge_ask(payload: dict) -> dict:
        req = app.request_ui("ask", payload)
        return app.wait_ui(req, _cancelled)

    tools_mod.set_ask_user_bridge(_bridge_ask)  # ask_user 工具经此弹面板（回合内有效）
    tools_mod.set_mode_bridge(
        lambda payload: _handle_mode_switch(app, llm.config, payload, _cancelled)
    )
    try:
        if ((getattr(llm.config, "data", None) or {}).get("workflow") or {}).get("enabled", False):
            from core import workflow as workflow_mod  # lazy：仅启用工作流时加载编排层

            wf = workflow_mod.load_workflow(llm.config)
            log.info("工作流: %s（%d节点）", wf.get("name"), len(wf.get("nodes") or []))
            workflow_mod.run_workflow(wf, turn)
        else:
            log.info("工作流未启用：直接对话（设置页\"系统\"标签可启用）")
            loop_mod.run_direct(turn)
    except loop_mod.TurnInterrupt:
        _aborted()
    except loop_mod.TurnStop:
        pass
    finally:
        tools_mod.set_ask_user_bridge(None)
        tools_mod.set_mode_bridge(None)
        tools_mod.set_background_origin(None)
        app.last_turn_error = turn.captured.pop("turn_error", None)  # 无头 --json 消费
        if runner is not None and turn.captured.pop("turn_failed", None):
            runner._failed_rollback = True  # 请求级失败：回合收尾整体回滚（_agent_turn 执行）


class _AgentRunner:
    """后台线程执行 agent 回合：等待响应期间 TUI 保持可交互。

    busy 期间用户消息分两条路径：
      普通 Enter — 插话：并入当前回合（回合循环取走作为 user 消息，不新开回合）
      Ctrl+Q     — 排队：当前回合结束后作为新回合发送
    """

    def __init__(self, llm: LLM, session, app: "ui.TuiApp", config: Config):
        self.llm = llm
        self.session = session
        self.app = app
        self.config = config
        self.busy = False
        self.last_error = None  # 最近回合的异常文本（无头 --json 消费；交互模式仅诊断用）
        # Esc 打断：标记本轮由 agent 线程收尾（保留已记录上下文）+ 回合起点消息序号
        self._esc_interrupt = False
        self._turn_active = False
        self.active_turn_id = None
        self._active_dispatch_origin = None
        self._active_is_background_call = False
        self._active_same_turn_call = False
        self._discarded_turn_ids: set = set()
        self._continuing_pending = False
        # 请求失败待用户决策
        self.pending_failed_turn = None
        self._abandon_pending = False
        self._failed_rollback = False
        self.last_turn_failed = False
        self.turn_start_index = 0
        self._queue: list = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def run_when_idle(self, callback):
        """与提交仲裁共用锁，空闲时执行会话修改。"""
        with self._lock:
            if self.busy:
                return False, None
            return True, callback()

    def _set_busy(self, value: bool) -> None:
        """busy 双写：runner 自身判定 + app.busy 镜像（UI 键位如树回退据此拒绝，跨线程只读）"""
        self.busy = value
        try:
            self.app.busy = value
        except Exception:
            pass

    def interrupt_turn(self) -> bool:
        """Esc 打断：活动回合由 agent 线程收尾（保留已记录上下文）；挂起失败回合就此收束。"""
        with self._lock:
            if self.busy and self._turn_active:
                self._esc_interrupt = True
                if getattr(self.app, "_llm_retry_gate", None) is not None:
                    self._abandon_pending = True
                self.llm.cancel()
                return True
            pending = self.pending_failed_turn
            if pending is None:
                return False
            self.pending_failed_turn = None
            try:
                _keep_pending_turn(self.session, self.app, pending)
            except Exception as exc:
                log.error("Esc 收束挂起回合失败: %r", exc)
        return True

    def discard_pending_turn(self) -> bool:
        """显式清理挂起回合（例如 /clear、/new、/resume 替换会话前）。"""
        with self._lock:
            pending = self.pending_failed_turn
            self.pending_failed_turn = None
            if pending is None:
                return False
            try:
                _rollback_failed_turn(self.session, self.app, self, pending=pending)
            except Exception as exc:
                log.error("清理挂起回合失败: %r", exc)
            return True

    def _discard_pending_for_user_message(self) -> bool:
        """新用户请求开始前丢弃失败挂起回合；必须在 agent 接管 session 前执行。"""
        with self._lock:
            pending = self.pending_failed_turn
            self.pending_failed_turn = None
            if pending is None:
                return False
            self._discarded_turn_ids.update(pending.get("turn_ids") or [pending.get("turn_id")])
            try:
                _rollback_failed_turn(self.session, self.app, self, pending=pending)
            except Exception as exc:
                log.error("新消息丢弃挂起回合失败: %r", exc)
            return True

    def _drop_injected_calls_for_turn(self, turn_id: str) -> None:
        with self.app.inject_lock:
            self.app.inject_queue = [
                item for item in self.app.inject_queue
                if not (
                    isinstance(item, dict)
                    and item.get("background_call")
                    and item.get("origin_turn_id") == turn_id
                )
            ]

    def _clear_pending_for_recovery(self) -> None:
        """挂起轮被外部调用成功续跑后仅清状态，不回滚其上下文/文件。"""
        with self._lock:
            self.pending_failed_turn = None

    def start(self, text: str, images=None) -> bool:
        with self._lock:
            if self.busy:
                return False
            self._set_busy(True)
            item = {"text": text, "images": list(images or [])}
            if self.pending_failed_turn is not None:
                item["_user_submission"] = True
                self._queue.insert(0, item)
                first = None
            else:
                first = item
        t = threading.Thread(target=self._run, args=(first,), daemon=True, name="bawcode-agent")
        self._thread = t
        t.start()
        return True

    def notify_subagent_completion(self, task_id: str, summary: str, origin_turn_id: str = None) -> None:
        """后台子代理完成：忙时排入当前回合注入队列，空闲时排入新回合。"""
        text = (
            f"[子代理完成通知 id={task_id}]\n{summary}\n"
            f"如需过程细节，请调用 query_subagent(id=\"{task_id}\")。"
        )
        self.notify(text, origin_turn_id=origin_turn_id)

    def notify(self, text: str, origin_turn_id: str = None) -> None:
        """后台事件通知入口（tools.set_background_notifier 注册）：
        idle 开新请求，busy 时并入当前请求；origin_turn_id 标识产生调用的回合。"""
        item = {
            "text": text,
            "images": [],
            "_background_call": True,
            "_origin_turn_id": origin_turn_id,
            "background_call": True,
            "origin_turn_id": origin_turn_id,
        }
        with self._lock:
            if origin_turn_id and origin_turn_id in self._discarded_turn_ids:
                log.info("忽略已丢弃回合的后台调用通知: origin=%s", origin_turn_id)
                return
            if self.busy:
                if self.pending_failed_turn is not None and not self._turn_active:
                    self._queue.append(item)
                    return
                if self._turn_active:
                    if (
                        self.pending_failed_turn is not None
                        and origin_turn_id in set(
                            self.pending_failed_turn.get("turn_ids") or [self.pending_failed_turn.get("turn_id")]
                        )
                    ):
                        self._active_same_turn_call = True
                        # 用户已经注入本请求时，本回合派生调用与用户输入冲突：
                        # 把用户输入留作独立新回合，并丢弃整个挂起回合。
                        with self.app.inject_lock:
                            queued_users = [
                                entry for entry in self.app.inject_queue
                                if not isinstance(entry, dict) or entry.get("user_submission")
                            ]
                            if queued_users:
                                self.app.inject_queue = [
                                    entry for entry in self.app.inject_queue
                                    if entry not in queued_users
                                ]
                                self._queue[0:0] = [
                                    {
                                        "text": entry.get("text", "") if isinstance(entry, dict) else str(entry),
                                        "images": [],
                                        "_user_submission": True,
                                    }
                                    for entry in queued_users
                                ]
                                self._abandon_pending = True
                                self.llm.cancel()
                                return
                    with self.app.inject_lock:
                        self.app.inject_queue.append(item)
                else:
                    self._queue.append(item)
                return
            self._queue.append(item)
            self._set_busy(True)
        t = threading.Thread(target=self._run, args=(None,), daemon=True, name="bawcode-agent")
        self._thread = t
        t.start()

    @staticmethod
    def _join_dispatch_text(user_text: str, calls: list) -> str:
        parts = [str(user_text or "").strip()]
        parts.extend(str(item.get("text") or "").strip() for item in calls)
        return "\n\n".join(part for part in parts if part)

    def _prepare_pending_dispatch(self, item: dict) -> dict:
        """队列尚未启动时仲裁挂起请求、用户输入与后台调用。"""
        with self._lock:
            pending = self.pending_failed_turn
            if pending is None:
                return item
            queued = [
                entry for entry in self._queue
                if isinstance(entry, dict) and (entry.get("_user_submission") or entry.get("_background_call"))
            ]
            candidates = [item, *queued]
            users = [entry for entry in candidates if entry.get("_user_submission")]
            calls = [entry for entry in candidates if entry.get("_background_call")]
            if not users:
                # 仅后台调用唤醒：无论来源如何均保留挂起上下文。
                if calls:
                    self._queue = [entry for entry in self._queue if entry not in queued]
                    item = dict(calls[0])
                    item["text"] = self._join_dispatch_text("", calls)
                    item["_continuing_pending"] = True
                    pending_ids = set(pending.get("turn_ids") or [pending.get("turn_id")])
                    item["_has_same_turn_call"] = any(
                        call.get("_origin_turn_id") in pending_ids for call in calls
                    )
                return item

            pending_ids = set(pending.get("turn_ids") or [pending.get("turn_id")])
            own_calls = [call for call in calls if call.get("_origin_turn_id") in pending_ids]
            if own_calls:
                self._queue = [entry for entry in self._queue if entry not in queued]
                return {
                    "text": self._join_dispatch_text("", users),
                    "images": list(users[0].get("images") or []),
                    "_user_submission": True,
                    "_discard_pending": True,
                }

            # 外部/其他回合调用与用户输入合并，挂起的失败轮次继续保留在 payload 中。
            if not calls:
                self._queue = [entry for entry in self._queue if entry not in queued]
                return {
                    "text": self._join_dispatch_text("", users),
                    "images": list(users[0].get("images") or []),
                    "_user_submission": True,
                    "_preserve_pending": True,
                    "_continuing_pending": True,
                    "_has_same_turn_call": False,
                }
            self._queue = [entry for entry in self._queue if entry not in queued]
            return {
                "text": self._join_dispatch_text("", users + calls),
                "images": list(users[0].get("images") or []),
                "_user_submission": True,
                "_preserve_pending": True,
                "_continuing_pending": True,
                "_has_same_turn_call": False,
            }
    def _queue_user_message(self, text: str, images=None) -> None:
        """排入明确由用户提交的新任务。"""
        user_item = {"text": text, "images": list(images or []), "_user_submission": True}
        with self._lock:
            self._queue.append(user_item)
            if self.busy:
                if self.pending_failed_turn is not None and self._turn_active:
                    pending_ids = set(self.pending_failed_turn.get("turn_ids") or [self.pending_failed_turn.get("turn_id")])
                    if self._active_same_turn_call or self._active_dispatch_origin in pending_ids:
                        self._abandon_pending = True
                        self.llm.cancel()
                return
            self._set_busy(True)
        t = threading.Thread(target=self._run, args=(None,), daemon=True, name="bawcode-agent")
        self._thread = t
        t.start()

    def inject_turn(self, text: str) -> str:
        """普通 Enter 插话；挂起失败轮时按调用来源与用户输入仲裁。"""
        take = getattr(self.app, "take_injections", None)
        with self._lock:
            if take is not None and self.busy:
                if self.pending_failed_turn is not None and not self._turn_active:
                    self._queue.append({"text": text, "images": [], "_user_submission": True})
                    return "[已排队，等待与后台调用仲裁]"
                if self.pending_failed_turn is not None and self._turn_active:
                    pending_ids = set(self.pending_failed_turn.get("turn_ids") or [self.pending_failed_turn.get("turn_id")])
                    if self._active_dispatch_origin in pending_ids:
                        self._drop_injected_calls_for_turn(self.pending_failed_turn.get("turn_id"))
                        self._queue.insert(0, {"text": text, "images": [], "_user_submission": True})
                        self._abandon_pending = True
                        self.llm.cancel()
                        return "[已丢弃本回合调用及挂起上下文，用户消息开始新回合]"
                with self.app.inject_lock:
                    self.app.inject_queue.append({
                        "text": text,
                        "_user_submission": True,
                        "user_submission": True,
                    })
                return "[插话已并入当前请求]"
        return self.queue_turn(text)

    def queue_turn(self, text: str, images=None) -> str:
        """排队消息与附件一起进入调度器。"""
        self.notify_message(text, images)
        with self._lock:
            n = len(self._queue)
        return f"[已排队 {n} 条 · 当前回合结束后作为新回合发送]"

    def submit(self, text: str, images=None) -> str:
        """外部提交在空闲时启动，忙碌时排队。"""
        if self.start(text, images):
            return "[已提交，新回合开始]"
        self.notify_message(text, images)
        return "[已排队 · 当前回合结束后作为新回合发送]"

    def notify_message(self, text: str, images=None) -> None:
        """以通知入口原子接力，避免回合退忙时留下孤立队列。"""
        self._queue_user_message(text, images)

    submit_message = submit

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
                    # 插话残余兜底：回合结束后才到达的插话，作为新回合接力（极窄竞态）
                    take = getattr(self.app, "take_injections", None)
                    leftover = take() if callable(take) else []
                    if leftover:
                        self._queue.extend(
                            item if isinstance(item, dict) else {"text": item, "images": []}
                            for item in leftover
                            if not (
                                isinstance(item, dict)
                                and item.get("background_call")
                                and item.get("origin_turn_id") in self._discarded_turn_ids
                            )
                        )
                    text = self._queue.pop(0) if self._queue else None
                    if text is None:
                        self._set_busy(False)
                        return
                    self._turn_active = True
            else:
                with self._lock:
                    self._turn_active = True
            try:
                item = text if isinstance(text, dict) else {"text": text, "images": []}
                item = self._prepare_pending_dispatch(item)
                if item.get("_skip_dispatch"):
                    with self._lock:
                        self._turn_active = False
                    text = None
                    continue
                with self._lock:
                    self._active_dispatch_origin = item.get("_origin_turn_id")
                    self._active_is_background_call = bool(item.get("_background_call"))
                    pending = self.pending_failed_turn
                    pending_ids = set((pending or {}).get("turn_ids") or [(pending or {}).get("turn_id")])
                    self._active_same_turn_call = bool(item.get("_has_same_turn_call")) or (
                        item.get("_origin_turn_id") in pending_ids
                    )
                    self._continuing_pending = bool(item.get("_continuing_pending"))
                discard_pending = item.pop("_discard_pending", False)
                is_user_submission = item.pop("_user_submission", False)
                preserve_pending = item.pop("_preserve_pending", False)
                if discard_pending or (is_user_submission and not preserve_pending):
                    self._discard_pending_for_user_message()
                self.turn_images = list(item.get("images") or [])
                self.app._turn_injected_users = []
                self.app._turn_injected_background_calls = []
                text = _before_turn(item["text"])
                self.llm.reset_cancel()
                self.last_error = None
                _agent_turn(self.llm, self.session, text, self.app, runner=self)
                if not self.last_turn_failed and item.pop("_continuing_pending", False):
                    self._clear_pending_for_recovery()
            except Exception as exc:
                log.error("agent 回合异常: %r", exc)
                self.last_error = str(exc)
                try:
                    self.session.add_message("system", f"回合异常: {exc}", type="help")
                except Exception:
                    pass
            finally:
                failed = getattr(self, "last_turn_failed", False)
                self.last_turn_failed = False
                _after_turn(self.session, text, failed=failed)
                try:
                    self.session.save_session()
                except Exception:
                    pass
                with self._lock:
                    self._turn_active = False
                    self._active_is_background_call = False
            text = None

    def cancel_and_join(self, timeout: float = 3.0) -> None:
        """退出时中止主请求，并取消/限时等待当前进程内的后台子代理。"""
        subagent_mod.stop_all_tasks(timeout=max(0.0, timeout * 0.5))
        self.llm.cancel()
        with self._lock:
            self._queue.clear()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)


class _HeadlessApp:
    """无头 UI 桩：与 TuiApp 同接口面（_agent_turn/_AgentRunner 所需），但无渲染无真人交互。

    无人值守语义：工具确认默认拒绝（--full 时 full 模式下 confirm 本就不触发）；
    choose 自动选首项（计划确认流等"确认"场景可无人推进）；ask_user 视作用户跳过
    （工具侧收到"基于现有信息继续"提示）；line 输入返回取消哨兵。"""

    def __init__(self, mode: str):
        self.mode = mode
        self.status = ""
        self.phase_hint = ""
        self.token_meter = None
        self.busy = False
        self.turn_start_time = 0.0
        self.turn_user_text = ""
        self.restore_input = None
        self.last_failed_text = None
        self.interrupt_turn_handler = None
        self.inject_queue = []
        self.inject_lock = threading.Lock()
        self._submit_queued = False
        self.pending_tool = None
        self.streaming_msg = None
        self.subagent_stream = None
        self._tree_follow_tail = True
        self.tool_count = 0
        self.ui_requests = []
        self.config = None

    def bind_config(self, config) -> None:
        self.config = config

    def refresh_from_session(self, session, task=None, render=True) -> None:
        pass

    def take_injections(self) -> list:
        with self.inject_lock:
            if not self.inject_queue:
                return []
            pending = list(self.inject_queue)
            self.inject_queue.clear()
            return pending

    def show_sessions_form(self, *args, **kwargs):
        return None  # 无头模式无会话选择面板

    def request_ui(self, kind: str, payload: dict) -> dict:
        self.ui_requests.append((kind, payload or {}))
        return {
            "kind": kind,
            "payload": payload or {},
            "result": self._auto_answer(kind, payload or {}),
            "event": threading.Event(),
            "served": True,
        }

    @staticmethod
    def _auto_answer(kind: str, payload: dict):
        if kind == "confirm":
            return {"action": "deny", "reason": "无头模式无确认通道（放行用 --full）"}
        if kind == "choose":
            options = payload.get("options") or []
            first = options[0] if options else ""
            return first[0] if isinstance(first, (tuple, list)) and first else first
        if kind == "ask":
            return {"status": "declined"}
        return CANCELLED

    def wait_ui(self, req: dict, cancelled=None, poll: float = 0.05):
        return req.get("result")


def _parse_args(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="bawcode",
        description="BAWCode 编码代理；不带 -p 进入交互式 TUI",
    )
    parser.add_argument("-p", "--prompt", metavar="任务",
                        help="一次性非交互任务（值为 - 时从 stdin 读取）；缺省进入交互模式")
    parser.add_argument("--cd", metavar="目录", help="工作区目录（缺省当前目录）")
    parser.add_argument("--config", metavar="路径", help="指定配置文件路径")
    parser.add_argument("--full", action="store_true",
                        help="一次性模式使用完全访问（工具确认自动放行；缺省确认类工具自动拒绝）")
    parser.add_argument("--json", action="store_true", help="一次性模式输出 JSON 结果（stdout，供脚本消费）")
    return parser.parse_args(argv)


def main_headless(args) -> int:
    """一次性模式：-p 任务 → 同线程跑完一整回合（复用 _AgentRunner 的完整路径：
    before_turn/agent_turn/回合快照/上下文维护/会话保存）→ 最终回复打 stdout → 退出"""
    if args.cd:
        os.chdir(args.cd)
    config = Config(config_path=args.config) if args.config else Config()
    identity = project_identity.ensure_project_identity(Path.cwd())
    log.info(
        "无头运行ing · 配置=%s · 模型=%s · project_id=%s",
        config.config_path,
        config.model_name,
        identity.get("project_id"),
    )
    plugins_mod.load(config, workspace=Path.cwd())
    session = memory_mod.init_session(config, project_identity_data=identity)
    tools_mod.webfetch_gc()
    pasteboard_mod.gc_temp_images()
    llm = LLM(config)
    session.set_llm_fn(lambda p: llm.chat([{"role": "user", "content": p}]).get("content", ""))
    hooks.set_external_apis((config.data or {}).get("external_apis") or {})
    subagent_mod.register_tools(config)
    mcp_mod.register_tools(config)  # MCP 后台连接发现；退出时统一 shutdown
    if args.full:
        config.mode = policy.MODE_FULL  # --full：完全访问（策略判定读 config.mode）
    app = _HeadlessApp(mode=config.mode or policy.MODE_AUTO)
    app.bind_config(config)
    app.usage_statistics = _init_usage_statistics()
    app.token_meter = llm.meter
    _register_commands(llm, session, config, app)

    user_text = args.prompt
    if user_text == "-":
        user_text = sys.stdin.read()
    text = (user_text or "").strip()
    if not text:
        print("任务为空：-p \"任务\"，或 -p - 从 stdin 读取", file=sys.stderr)
        return 2

    started = time.monotonic()
    try:
        runner = _AgentRunner(llm, session, app, config)
        tools_mod.set_background_notifier(runner.notify)
        plugins_mod.bind_runtime(app=app, runner=runner)
        # busy 置位：无头下同步跑回合，若不置位，后台子代理完成通知会另起线程并发跑回合
        runner._set_busy(True)
        try:
            runner._run(text)  # 同线程同步执行：内部含 before_turn/回合/保存收尾
        finally:
            runner._set_busy(False)
        # 异步子代理收尾：等后台任务结束，再跑通知回合把结果并入最终答复
        deadline = time.monotonic() + _headless_task_wait_seconds(config)
        runner._set_busy(True)
        try:
            while subagent_mod.pending_task_count() and time.monotonic() < deadline:
                subagent_mod.wait_tasks(timeout=min(1.0, max(0.1, deadline - time.monotonic())))
            if time.monotonic() >= deadline and subagent_mod.pending_task_count():
                log.warn("无头等待后台子代理超时，仍有 %d 个任务在跑", subagent_mod.pending_task_count())
            runner._run(None)
        finally:
            runner._set_busy(False)
    finally:
        try:
            mcp_mod.shutdown()
        except Exception:
            pass
        try:
            session.save_longterm()
        except Exception:
            pass

    elapsed = time.monotonic() - started
    last = next((m for m in reversed(session.messages) if m.get("role") == "assistant"), None)
    response = str((last or {}).get("content") or "").strip()
    # 双错误来源：回合崩溃（runner.last_error）> 工具循环 LLM 错误（app.last_turn_error）
    error = getattr(runner, "last_error", None) or getattr(app, "last_turn_error", None)
    ok = bool(response) and not error
    log.info("无头运行结束 · %.2fs · 回复 %d 字 · error=%r", elapsed, len(response), error)
    if args.json:
        print(json.dumps(
            {
                "ok": ok,
                "response": response,
                "error": error,
                "session_id": session.session_id,
                "project_id": session.project_id,
                "elapsed_s": round(elapsed, 2),
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        if response:
            print(response)
        else:
            print("（本轮未产生文本回复；详情见 data/log）", file=sys.stderr)
        if error:
            print(f"回合错误: {error}", file=sys.stderr)
    return 0 if ok else 1


def main() -> None:
    #开启windowsVT以便捕获输入事件
    ui._enable_windows_ansi()
    config = Config()
    if config.load_error:
        print(f"[配置] {config.load_error}", file=sys.stderr)
    identity = project_identity.ensure_project_identity(Path.cwd())
    log.info(
        "BAWCode 启动 · 配置=%s · 模型=%s · project_id=%s",
        config.config_path,
        config.model_name,
        identity.get("project_id"),
    )
    plugins_mod.load(config, workspace=Path.cwd())  # 外部插件装载（先于会话初始化，插件可收到 session_start）
    session = memory_mod.init_session(config, project_identity_data=identity)
    tools_mod.webfetch_gc()
    pasteboard_mod.gc_temp_images()  #启动回收data/temp过期剪贴板图片（7 天封顶）
    llm = LLM(config)
    session.set_llm_fn(lambda p: llm.chat([{"role": "user", "content": p}]).get("content", ""))
    hooks.set_external_apis((config.data or {}).get("external_apis") or {})
    subagent_mod.register_tools(config)
    mcp_mod.register_tools(config)  # MCP 服务器后台连接发现，工具随注册进度逐个可见
    app = ui.get_app()
    app.bind_config(config)
    app.usage_statistics = _init_usage_statistics()
    app.token_meter = llm.meter
    _register_commands(llm, session, config, app)

    app.enter()
    try:
        runner = _AgentRunner(llm, session, app, config)
        app.agent_runner = runner
        tools_mod.set_background_notifier(runner.notify)  # 后台命令完成 → runner.notify 自动开新回合
        plugins_mod.bind_runtime(app=app, runner=runner)  # 插件类用户操作 API（submit_turn/notify/ctx.ui）载体
        app.interrupt_turn_handler = runner.interrupt_turn  # Esc 打断当前回合并保留其已记录上下文
        llm.query_balance()
        app.token_meter = llm.meter
        _sync(app, session, status="就绪")
        while True:
            raw = app.read_line(config=config)
            queued = app.consume_queued_submit()  # Ctrl+Q 排队标记
            has_images = app.consume_submit_images()  # 含图片的提交不插话
            images = app.take_submit_images()
            app.token_meter = llm.meter
            text = (raw or "").strip()
            if not text:
                continue
            if text.startswith("/"):
                head = text.split()[0]
                if runner.busy and head in ("/clear", "/new", "/resume", "/undo", "/clear-trash"):
                    session.add_message(
                        "system",
                        "本轮对话进行中：等待完成，或直接发送新消息（普通发送=插话并入本轮 · Ctrl+Q=排队新回合）后再操作会话",
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
                # 普通发送=插话并入当前回合；Ctrl+Q 或含图片=排队为当前回合结束后的新回合
                if queued or has_images:
                    note = runner.submit(text, images)
                else:
                    note = runner.inject_turn(text)
                session.add_message("system", note, type="help")
                _sync(app, session)
                continue
            if not runner.start(text, images):
                # busy 检查与 start 之间后台通知可能已置忙：排队接力，不丢输入
                note = runner.submit(text, images)
                session.add_message("system", note, type="help")
                _sync(app, session)
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


def cli() -> None:
    """命令行入口：-p 走无头模式，缺省进入交互 TUI"""
    args = _parse_args()
    if args.prompt is not None:
        sys.exit(main_headless(args))
    main()


if __name__ == "__main__":
    cli()
