# -*- coding: utf-8 -*-
"""外置工作流引擎：data/workflows/<name>.json 声明式线性节点链。

一轮 agent 回合的处理管线不再硬编码于 main.py，而是按配置文件的节点列表
顺序执行；"不想要计划/测试"等取舍由用户在编辑器里增删节点表达。
按复杂度路由节点的机制（analyze 节点 + gate 门槛）已于 2026-09-30 移除，
引擎为无条件线性链——备份见 _recycle/gate-routing-20260930/。

节点类型（内置执行器）：
  system_prompt — 指定初始化注入哪些系统提示词文件（缺省走 config.prompt.system_files）
  skill         — 是否注入技能清单 / 白名单过滤（经 core/skills.set_injection）
  understand    — 任务理解：带工具循环解析意图，缺失信息经 ask_user 询问用户
  analyze       — 复杂度判定：独立一次 LLM 请求输出 high/low，写入 ^{complexity}^
  plan          — 生成计划（plan.md）；confirm 需用户确认；steps 确认后生成步骤
  execute       — 主工具调用循环（编码执行）
  review        — 审查节点：审查本回合改动并反思修正（带工具循环）
  llm           — 通用 LLM 阶段：prompt_files 渲染为补充系统提示词，max_rounds>0 时
                  带工具循环（分析调研/测试类节点），0 则单次问答；capture 把输出
                  存为 ^{变量}^ 供后续节点提示词渲染

通用字段（LLM 类节点）：model（完整 model_name，> model_role > 激活模型）、
prompt（内联提示词，> prompt_files）、prompt_files、override_system（true=节点
提示词替换内置系统提示词，默认追加）、capture。

流程控制：引擎不依赖 UI——状态行/选择菜单/工具确认等经 TurnIO 回调移交；
取消/中断抛 TurnInterrupt（调用方 _aborted），回合正常终止（用户取消任务/
计划未确认/推理出错/轮次上限）抛 TurnStop。加载失败回退 DEFAULT_WORKFLOW。
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core import policy
from core import prompt_loader
from core import register
from core.llm import CANCELLED, get_system_prompt
from core.log import get_logger
from core.tools import has_recent_progress

log = get_logger("workflow")

_ROOT = Path(__file__).resolve().parent.parent

NODE_TYPES = ("system_prompt", "skill", "understand", "analyze", "plan", "execute", "review", "llm")

# 节点内置提示词兜底（默认引用 core/prompts/ 下同名 md，可被节点 prompt/prompt_files 覆盖）
JUDGE_PROMPT_BUILTIN = (
    "你是任务复杂度评审员。基于对话中的用户任务判断复杂度：\n"
    "- high：多文件/架构/系统级改动、需求模糊需要澄清、影响面大\n"
    "- low：单点小改动、问题清晰可直接执行\n\n"
    "只输出一行：high 或 low"
)
REVIEW_PROMPT_BUILTIN = (
    "你是代码审查节点。对当前会话中的改动进行审查与修正：\n"
    "1. 梳理本回合的改动范围（读代码/查 git）\n"
    "2. 逐项审查：正确性、边界条件、风格一致性、遗漏的测试\n"
    "3. 发现问题直接修复（可使用 edit_file/write/run_command）\n"
    "4. 输出【审查结论】：发现的问题、已修正项、遗留风险\n\n"
    "只输出审查结论。"
)
UNDERSTAND_PROMPT_BUILTIN = (
    "你是任务理解节点。解析用户当前任务的真实意图：\n"
    "1. 用一句话复述任务目标与验收标准\n"
    "2. 逐项检查执行所需信息是否完备（目标路径、范围、约束、偏好等）\n"
    "3. 发现缺失或歧义时，调用 ask_user 工具向用户提问（一次一个问题，问题具体、尽量给出候选项）\n"
    "4. 信息补齐后，输出【任务理解】：目标、约束、待办要点清单\n\n"
    "只输出任务理解本身，不要开始执行任务。"
)
# plan 节点 steps=true 时交付给 execute 的启动指令：步骤拆解由主 LLM 经 generate_steps
# 工具完成（结构化参数），不再走节点旁路 LLM 请求（旁路输出无 schema 约束，按行解析
# 会把模型的跑题长文整体误收为步骤，2026-10-04 1379 步事故）
STEPS_KICKOFF_BUILTIN = (
    "开始执行前，先调用 generate_steps 工具把当前计划拆解为可执行步骤"
    "（3-8 条，动词开头、每步具体可验证），之后再按步骤推进；"
    "每步开始/结束时用 update_step_status 同步状态。"
)

# 未知/损坏配置的最后兜底：与 data/workflows/default.json 保持一致（最简通用管线）
DEFAULT_WORKFLOW: dict = {
    "name": "default",
    "description": "最简通用管线：系统提示词注入 → 编码执行（通用角色直接干活）",
    "nodes": [
        {"id": "system_prompt", "type": "system_prompt", "files": ["system_prompt.md"]},
        {"id": "execute", "type": "execute", "max_rounds": 12, "model_role": "code"},
    ],
}


class TurnInterrupt(Exception):
    """回合被取消/中断：调用方按中断语义收尾（_aborted）"""


class TurnStop(Exception):
    """回合正常终止：处理器已写入消息与状态（用户取消/计划未确认/出错/轮次上限）"""


class TurnIO:
    """UI 桥回调集合：引擎经此移交交互，不反向依赖 UI"""

    def __init__(
        self,
        status: Optional[Callable[[str], None]] = None,
        choose: Optional[Callable] = None,
        line: Optional[Callable] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        on_delta: Optional[Callable] = None,
        clear_stream: Optional[Callable[[], None]] = None,
        tool_confirm: Optional[Callable[[dict], dict]] = None,
        phase: Optional[Callable[[str], None]] = None,
    ):
        self.status = status or (lambda text: None)
        self.choose = choose
        self.line = line
        self.cancelled = cancelled or (lambda: False)
        self.on_delta = on_delta
        self.clear_stream = clear_stream or (lambda: None)
        self.tool_confirm = tool_confirm
        self.phase = phase or (lambda text: None)


class TurnContext:
    """单回合上下文：引擎各节点共享的运行态与捕获变量"""

    def __init__(self, session, llm, app, config, user_text: str, io: TurnIO):
        self.session = session
        self.llm = llm
        self.app = app
        self.config = config
        self.user_text = user_text
        self.io = io
        self.system_prompt_text = ""
        self.captured: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# 加载与校验


def workflow_dir(config=None) -> Path:
    rel = ((getattr(config, "data", None) or {}).get("workflow") or {}).get("dir") or "data/workflows"
    p = Path(rel)
    return p if p.is_absolute() else _ROOT / rel


def workflow_path(config, name: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", str(name or "").strip())
    return workflow_dir(config) / f"{safe or 'default'}.json"


def active_name(config) -> str:
    return str(((getattr(config, "data", None) or {}).get("workflow") or {}).get("active") or "default").strip()


def set_active(config, name: str) -> None:
    (config.data.setdefault("workflow", {}))["active"] = str(name or "default").strip()


def list_workflows(config) -> List[str]:
    d = workflow_dir(config)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if p.is_file())


def validate_workflow(data: Any) -> List[str]:
    """结构校验：返回错误列表（空=通过）；不校验运行期语义（文件存在性等）"""
    if not isinstance(data, dict):
        return ["根节点必须是对象"]
    errors: List[str] = []
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["nodes 必须是非空数组"]
    ids = set()
    for index, node in enumerate(nodes):
        label = f"节点#{index + 1}"
        if not isinstance(node, dict):
            errors.append(f"{label} 不是对象")
            continue
        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            errors.append(f"{label} 未知类型: {ntype!r}")
        nid = str(node.get("id") or "").strip()
        if not nid:
            errors.append(f"{label} 缺少 id")
        elif nid in ids:
            errors.append(f"{label} id 重复: {nid}")
        ids.add(nid)
    return errors


def load_workflow(config, name: Optional[str] = None) -> dict:
    """读取工作流 JSON；缺失/损坏 → 告警并回退内置默认（保证回合可跑）"""
    name = str(name or active_name(config)).strip() or "default"
    path = workflow_path(config, name)
    if not path.exists():
        log.warn("工作流文件不存在: %s，使用内置默认工作流", path)
        data = copy.deepcopy(DEFAULT_WORKFLOW)
        data["name"] = name
        return data
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warn("工作流读取/解析失败 %s: %s，使用内置默认工作流", path, e)
        data = copy.deepcopy(DEFAULT_WORKFLOW)
        data["name"] = name
        return data
    errors = validate_workflow(data)
    if errors:
        for err in errors:
            log.warn("工作流校验失败 %s: %s", path, err)
        log.warn("工作流 %s 不合法，使用内置默认工作流", name)
        fallback = copy.deepcopy(DEFAULT_WORKFLOW)
        fallback["name"] = name
        return fallback
    data["name"] = str(data.get("name") or name)
    # 规范化节点：补 id、剔除多余字段不强制（宽松读取）
    normalized = []
    for index, node in enumerate(data["nodes"]):
        node = dict(node)
        node.setdefault("id", f"n{index + 1}")
        node.setdefault("gate", "always")
        node.setdefault("enabled", True)
        normalized.append(node)
    data["nodes"] = normalized
    return data


# ---------------------------------------------------------------------------
# 运行器


def _check_cancel(turn: TurnContext) -> None:
    if turn.io.cancelled():
        raise TurnInterrupt()


def _role_model(config, role: Any) -> Optional[str]:
    """模型角色 → model_name；空/main/未知角色 → None（用激活模型）"""
    text = str(role or "").strip().lower()
    if text in ("", "main", "active", "none"):
        return None
    if hasattr(config, "get_task_model"):
        return config.get_task_model(text)
    return None


def _node_model(config, node: dict) -> Optional[str]:
    """节点级模型：model（完整 model_name）> model_role > None（激活模型）。

    model 不在可用列表时告警回落，避免拼错模型名静默打到错误端点。"""
    name = str(node.get("model") or "").strip()
    if name:
        rows = config.list_models() if hasattr(config, "list_models") else []
        if any(str(r.get("model_name")) == name for r in rows):
            return name
        log.warn("节点模型 %r 不在可用模型列表，回落 model_role/激活模型", name)
    return _role_model(config, node.get("model_role"))


def _node_stage(turn: TurnContext, node: dict, builtin: str = "") -> str:
    """节点提示词：内联 prompt > prompt_files > builtin 兜底（均做 ^{var}^ 渲染）"""
    inline = str(node.get("prompt") or "").strip()
    if inline:
        return prompt_loader.render_text(inline, _stage_variables(turn))
    files = node.get("prompt_files") or []
    if files:
        return render_stage_prompt(turn, files)
    return prompt_loader.render_text(builtin, _stage_variables(turn)) if builtin else ""


def _stage_system(turn: TurnContext, node: dict, stage: str) -> Optional[str]:
    """节点 extra_system 组装：override_system=true 时替换内置系统提示词，否则追加"""
    if str(node.get("override_system") or "").strip().lower() in ("1", "true", "yes"):
        return stage.strip() or None
    return _combine_system(turn.system_prompt_text, stage)


def _stage_variables(turn: TurnContext) -> Dict[str, Any]:
    """节点提示词渲染变量：基础变量表 + 计划 + 回合捕获变量"""
    extra: Dict[str, Any] = {}
    extra.update(turn.captured)
    plan = getattr(turn.session, "plan", None) or {}
    extra.setdefault("plan_title", plan.get("title", ""))
    extra.setdefault("plan_complexity", plan.get("complexity", ""))
    extra.setdefault("plan_content", plan.get("content", ""))
    return prompt_loader.build_variable_context(
        config=turn.config,
        session=turn.session,
        project_identity=getattr(turn.session, "project_identity", None),
        extra=extra,
    )


def render_stage_prompt(turn: TurnContext, names: Any) -> str:
    """渲染节点 prompt_files（^{var}^ 占位符替换，多文件拼接）"""
    if not names:
        return ""
    variables = _stage_variables(turn)
    segs = prompt_loader.load_prompt_bundle([str(n) for n in names], variables, config=turn.config)
    return prompt_loader.join_segments(segs)


def _combine_system(*parts: str) -> Optional[str]:
    text = "\n\n".join(p for p in parts if p and p.strip())
    return text or None


# --- 节点执行器 -------------------------------------------------------------


def _exec_system_prompt(node: dict, turn: TurnContext) -> None:
    files = [str(f) for f in (node.get("files") or [])] or None
    try:
        turn.system_prompt_text = get_system_prompt(config=turn.config, files=files)
    except Exception as e:
        log.warn("系统提示词加载失败，使用兜底: %s", e)
        turn.system_prompt_text = (
            "你是 BAWCode 编码 Agent。根据用户任务与记忆/计划/步骤工作。回复使用简洁中文。"
        )
    if not any(m.get("type") == "system_prompt" for m in turn.session.messages):
        names = files or list(prompt_loader.system_prompt_files(turn.config))
        turn.session.add_message(
            "system",
            "注入系统提示词",
            type="system_prompt",
            files=names or ["system_prompt.md"],
        )
        log.info("注入系统提示词: %s", ", ".join(names or ["system_prompt.md"]))
        turn.io.status("注入系统提示词")


def _exec_skill(node: dict, turn: TurnContext) -> None:
    from core import skills as skills_mod

    skills_mod.set_injection(
        enabled=bool(node.get("enabled", True)),
        allow=[str(n) for n in (node.get("list") or [])] or None,
    )


def _plan_confirm_loop(turn: TurnContext) -> None:
    """计划确认交互：0/1/2/3 菜单，修改后必须二次确认（自 main.py 迁入）"""
    io, session, llm = turn.io, turn.session, turn.llm
    confirmed = False
    for _edit_round in range(3):
        choice = str(io.choose(
            [("1", "确认计划"), ("2", "直接改计划"), ("3", "反馈修改"), ("0", "取消")],
            "计划已生成",
        ) or "").strip()
        if choice == CANCELLED or io.cancelled():
            raise TurnInterrupt()
        if choice == "0":
            session.add_message("assistant", "用户取消任务。")
            io.status("已取消")
            raise TurnStop()
        if choice == "2":
            lines = []
            while True:
                line = str(io.line("计划> ") or "")
                if line == CANCELLED or io.cancelled():
                    raise TurnInterrupt()
                if line.strip() == "END":
                    break
                lines.append(line)
            session.plan.update(llm.confirm_plan(session.plan, "manual_edit", feedback="\n".join(lines)))
        elif choice == "3":
            fb = str(io.line("反馈> ") or "")
            if fb == CANCELLED or io.cancelled():
                raise TurnInterrupt()
            session.plan.update(llm.confirm_plan(session.plan, "llm_modify", feedback=fb))
        elif choice == "1":
            session.plan.update(llm.confirm_plan(session.plan, "confirm"))
            session.update_plan_status("confirmed")
            session.save_longterm()
            confirmed = True
            break
        else:
            session.add_message("system", "请输入 0/1/2/3", type="help")
            io.status("请重新选择计划操作")
            continue
        # 2/3 修改后必须二次确认，禁止直接执行
        again = str(io.choose([("1", "确认并执行"), ("2", "继续修改"), ("0", "取消")], "计划已修改") or "").strip()
        if again == CANCELLED or io.cancelled():
            raise TurnInterrupt()
        if again == "0":
            session.add_message("assistant", "用户取消任务。")
            io.status("已取消")
            raise TurnStop()
        if again == "1":
            session.plan.update(llm.confirm_plan(session.plan, "confirm"))
            session.update_plan_status("confirmed")
            session.save_longterm()
            confirmed = True
            break
        # again == "2" 或非法 → 回到 1/2/3 菜单继续改
    if not confirmed:
        session.add_message("assistant", "计划未确认，任务中止。")
        io.status("计划未确认")
        raise TurnStop()


def _exec_plan(node: dict, turn: TurnContext) -> None:
    io, session, llm = turn.io, turn.session, turn.llm
    log.info("进入计划流程")
    io.status("生成计划")
    plan_model = _node_model(turn.config, node)
    if plan_model is None:
        plan_model = _role_model(turn.config, node.get("model_role") or "plan")
    plan = llm.generate_plan(turn.user_text, model=plan_model)
    _check_cancel(turn)
    session.set_plan(
        plan.get("title", "任务计划"),
        plan.get("content", ""),
        complexity=plan.get("complexity", "high"),
    )
    if node.get("confirm", True):
        _plan_confirm_loop(turn)
    else:
        # 免确认：直接置 confirmed，计划照常注入上下文
        session.plan.update(llm.confirm_plan(session.plan, "confirm"))
        session.update_plan_status("confirmed")
        session.save_longterm()
    if node.get("steps", True):
        # 步骤拆解归口主 LLM：execute 节点据该标记注入启动指令，经 generate_steps 工具落库
        turn.captured["steps_kickoff"] = "1"
    # 收尾消息附带 plan 快照（extras 不进 API）：会话树在该消息时间线位置渲染冻结的
    # 计划节点（core/ui.py._snapshot_nodes）；步骤不在此快照——由后续 generate_steps
    # 工具调用在时间线上以独立节点呈现
    session.add_message(
        "assistant",
        "计划确认，开始执行。",
        type="plan",
        plan_snapshot=json.dumps(session.plan, ensure_ascii=False),
    )


def _has_recent_progress(session, window: int = 4) -> bool:
    """最近工具结果仍有进展（判据见 core.tools.has_recent_progress）"""
    return has_recent_progress(getattr(session, "messages", None) or [], window=window)


def _tool_loop(turn: TurnContext, extra_system: Optional[str], max_rounds: int, model: Optional[str]) -> dict:
    """工具调用循环（自 main.py 迁入）：返回 {"status", "content"}

    status: complete（模型不再调用工具，回合完成）/ error / max_rounds。
    取消/中断抛 TurnInterrupt；cancel 检查点与消息入库路径与迁移前一致。
    轮次用尽时不再硬中断：若最近工具调用仍有进展（成功/非空/互不相同/非全失败）
    则自动续期（上限 config.workflow.max_rounds_extensions），直到无进展或续期用尽。
    """
    io, session, llm = turn.io, turn.session, turn.llm
    wf_cfg = (getattr(turn.config, "data", None) or {}).get("workflow") or {}
    try:
        extensions_limit = max(0, int(wf_cfg.get("max_rounds_extensions", 5)))
    except (TypeError, ValueError):
        extensions_limit = 5
    extensions = 0
    round_no = 0
    while True:
        if round_no >= max(1, max_rounds):
            if extensions < extensions_limit and _has_recent_progress(session):
                extensions += 1
                log.info(
                    "轮次用尽但最近工具调用仍有进展，续期 %d/%d", extensions, extensions_limit
                )
                io.status(f"轮次续期 {extensions}/{extensions_limit}")
            else:
                reason = f"达到最大工具轮次（{max_rounds}轮）"
                if extensions:
                    reason += f"及 {extensions} 次续期"
                if extensions_limit <= 0 or not _has_recent_progress(session):
                    reason += "，最近工具调用无进展"
                session.add_message("assistant", f"{reason}。")
                log.warn("%s", reason)
                io.status("轮次上限")
                return {"status": "max_rounds", "content": ""}
        round_no += 1
        _check_cancel(turn)
        io.status(f"推理 · 第{round_no + 1}轮")
        io.phase("思考中")
        payload = session.build_messages(extra_system=extra_system)
        llm.meter.measure_context(payload)
        response = llm.chat(payload, tools=register.get_tool_defs(), model=model, on_delta=io.on_delta)
        io.clear_stream()  # 树尾直播收口：正式消息按现有路径入库
        if turn.app is not None:
            turn.app.token_meter = llm.meter
        if response.get("error") == CANCELLED or io.cancelled():
            raise TurnInterrupt()
        if response.get("error"):
            log.error("第%d轮推理返回错误: %s", round_no + 1, response["error"])
            session.add_message("assistant", response["error"])
            io.status("出错")
            return {"status": "error", "content": ""}
        if (response.get("content") or response.get("reasoning")) and not response.get("tool_calls"):
            # thinking 与正文并存时以 thinking 字段随消息留档（回合内出站并回 content，回合末剥离）
            extra = {"thinking": response["reasoning"]} if response.get("reasoning") else {}
            session.add_message("assistant", response["content"], **extra)
        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            io.phase("")  # 回合完成：状态行空行占位
            log.info("任务完成（共%d轮推理）", round_no + 1)
            if not io.cancelled():
                session.maybe_compress(llm_fn=lambda p: llm.chat([{"role": "user", "content": p}]).get("content", ""))
            io.status("就绪")
            return {"status": "complete", "content": response.get("content") or ""}

        # description 是调用意图的简明说明（回合末剥离后的留档记录）：
        # 提取后从 arguments 剔除，策略判定/指纹白名单/确认面板/工具执行均不可见
        for call in tool_calls:
            args = call.get("arguments")
            if isinstance(args, dict) and "description" in args:
                call["description"] = str(args.pop("description") or "")

        io.phase("工具调用中")
        pending = []
        allowed_results = []
        log.debug("第%d轮返回 %d 个工具调用", round_no + 1, len(tool_calls))
        for call in tool_calls:
            action, reason = llm.evaluate_tool(call.get("name"), call.get("arguments") or {})
            if action == policy.ALLOW:
                allowed_results.append((call, llm.execute_approved_tool(call)))
            elif action == policy.CONFIRM:
                pending.append(call)
            else:
                allowed_results.append(
                    (
                        call,
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "tool_name": call.get("name"),
                            "content": policy.default_reject_message(reason),
                            "type": "tool",
                        },
                    )
                )

        # DeepSeek Tool Calls：保留 assistant 工具轮（含 content + tool_calls）；
        # thinking 与正文并存时以 thinking 字段留档（出站时并回 content）
        thinking_extra = {"thinking": response["reasoning"]} if response.get("reasoning") else {}
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
            **thinking_extra,
        )

        for call, item in allowed_results:
            session.add_tool_result(call, item["content"], item.get("images"))
        io.status(f"工具 {len(allowed_results)} 完成 · 待确认 {len(pending)}")

        for call in pending:
            if io.tool_confirm is None:
                result = {"content": policy.default_reject_message("无确认通道")}
            else:
                result = io.tool_confirm(call)
            session.add_tool_result(call, result["content"], result.get("images"))
            _check_cancel(turn)
            io.status(f"确认完成 · {call.get('name')}")

        if turn.app is not None:
            turn.app.token_meter = llm.meter
        io.status("继续")
    # while 循环不可达出口：轮次终止统一在循环内 max_rounds 分支返回


def _exec_analyze(node: dict, turn: TurnContext) -> None:
    """复杂度判定：独立一次 LLM 请求，输出 high/low；失败/不可解析回落 default_level"""
    io, session, llm = turn.io, turn.session, turn.llm
    default_level = str(node.get("default_level") or "low").strip().lower()
    if default_level not in ("high", "low"):
        default_level = "low"
    io.status("LLM 复杂度判定")
    io.phase("思考中")
    stage = _node_stage(turn, node, JUDGE_PROMPT_BUILTIN)
    payload = session.build_messages(extra_system=_stage_system(turn, node, stage))
    result = llm.chat(payload, model=_node_model(turn.config, node), on_delta=io.on_delta)
    io.clear_stream()
    _check_cancel(turn)
    reply = ""
    if result.get("error"):
        log.warn("复杂度判定请求失败: %s，回落 %s", result["error"], default_level)
    else:
        reply = (result.get("content") or "").strip()
    last = reply.splitlines()[-1].strip().lower().strip("*。.！!") if reply else ""
    m = re.match(r"^(high|low)\b", last)
    if m:
        turn.captured["complexity"] = m.group(1)
    else:
        if reply:
            log.warn("复杂度判定输出不可解析，回落 %s: %r", default_level, reply[:120])
        turn.captured["complexity"] = default_level
    log.info("复杂度判定(LLM): %s", turn.captured["complexity"])
    io.status(f"分析完成 · {turn.captured['complexity']}")


def _run_aux_loop(node: dict, turn: TurnContext, builtin: str, default_rounds: int, fallback_role: Optional[str]) -> None:
    """审查/理解类节点：带工具循环，非致命（error/轮次上限不阻断后续节点）"""
    stage = _node_stage(turn, node, builtin)
    model = _node_model(turn.config, node)
    if model is None:
        model = _role_model(turn.config, node.get("model_role") or fallback_role)
    try:
        max_rounds = int(node.get("max_rounds") or default_rounds)
    except (TypeError, ValueError):
        max_rounds = default_rounds
    result = _tool_loop(turn, _stage_system(turn, node, stage), max_rounds, model=model)
    capture = str(node.get("capture") or "").strip()
    if capture and result.get("content"):
        turn.captured[capture] = result["content"].strip()
    if result["status"] == "error":
        log.warn("节点(id=%s)推理出错，继续后续节点", node.get("id"))


def _exec_review(node: dict, turn: TurnContext) -> None:
    turn.io.status("审查与修正")
    _run_aux_loop(node, turn, REVIEW_PROMPT_BUILTIN, 8, "review")


def _exec_understand(node: dict, turn: TurnContext) -> None:
    turn.io.status("任务理解")
    _run_aux_loop(node, turn, UNDERSTAND_PROMPT_BUILTIN, 8, None)


def _exec_execute(node: dict, turn: TurnContext) -> None:
    turn.io.status("执行步骤")
    try:
        max_rounds = int(node.get("max_rounds") or 12)
    except (TypeError, ValueError):
        max_rounds = 12
    # config.workflow.max_rounds >0 时全局覆盖工作流节点值（设置页"最大工具轮数"）
    wf_cfg = (getattr(turn.config, "data", None) or {}).get("workflow") or {}
    try:
        override = int(wf_cfg.get("max_rounds") or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        max_rounds = override
    model = _node_model(turn.config, node)
    if model is None:
        model = _role_model(turn.config, node.get("model_role") or "code")
    stage = _node_stage(turn, node)
    if turn.captured.pop("steps_kickoff", None):
        stage = _combine_system(stage, STEPS_KICKOFF_BUILTIN)
    result = _tool_loop(turn, _stage_system(turn, node, stage), max_rounds, model=model)
    if result["status"] != "complete":
        raise TurnStop()
    capture = str(node.get("capture") or "").strip()
    if capture:
        turn.captured[capture] = (result["content"] or "").strip()


def _exec_llm(node: dict, turn: TurnContext) -> None:
    io, session, llm = turn.io, turn.session, turn.llm
    stage = _node_stage(turn, node)
    model = _node_model(turn.config, node)
    if model is None:
        model = _role_model(turn.config, node.get("model_role"))
    try:
        max_rounds = int(node.get("max_rounds") or 0)
    except (TypeError, ValueError):
        max_rounds = 0

    if max_rounds > 0:
        # 带工具的分析/测试类节点：非致命（error/轮次上限不阻断后续节点）
        result = _tool_loop(turn, _stage_system(turn, node, stage), max_rounds, model=model)
        content = result["content"]
        if result["status"] == "error":
            log.warn("工作流 llm 节点(id=%s)推理出错，继续后续节点", node.get("id"))
    else:
        # 单次问答：完整上下文（历史+记忆补充）+ 节点提示词，不带工具
        io.phase("思考中")
        payload = session.build_messages(extra_system=_stage_system(turn, node, stage))
        result = llm.chat(payload, model=model, on_delta=io.on_delta)
        io.clear_stream()
        _check_cancel(turn)
        if result.get("error"):
            log.warn("工作流 llm 节点(id=%s)出错: %s，继续后续节点", node.get("id"), result["error"])
            io.status("节点出错 · 继续")
            return
        content = result.get("content") or ""
        if content:
            session.add_message("assistant", content)

    capture = str(node.get("capture") or "").strip()
    if capture and content:
        turn.captured[capture] = content.strip()


_EXECUTORS: Dict[str, Callable[[dict, TurnContext], None]] = {
    "system_prompt": _exec_system_prompt,
    "skill": _exec_skill,
    "understand": _exec_understand,
    "analyze": _exec_analyze,
    "plan": _exec_plan,
    "execute": _exec_execute,
    "review": _exec_review,
    "llm": _exec_llm,
}


def run_workflow(workflow: dict, turn: TurnContext) -> str:
    """按节点链顺序执行一轮回合；TurnInterrupt/TurnStop 冒泡给调用方收尾"""
    nodes = workflow.get("nodes") or []
    # skill 节点缺省时恢复默认注入（避免上一回合的过滤残留）
    from core import skills as skills_mod

    skills_mod.set_injection(enabled=None, allow=None)
    if not any(n.get("type") == "system_prompt" for n in nodes):
        _exec_system_prompt({}, turn)  # 保持旧行为：默认注入 config.prompt.system_files
    for node in nodes:
        _check_cancel(turn)
        if not node.get("enabled", True):
            log.debug("节点已停用，跳过: %s", node.get("id"))
            continue
        runner = _EXECUTORS.get(str(node.get("type") or ""))
        if runner is None:
            log.warn("未知节点类型，跳过: id=%s type=%r", node.get("id"), node.get("type"))
            continue
        log.info("工作流节点: %s（%s）", node.get("id"), node.get("type"))
        runner(node, turn)
    return "complete"


def workflow_enabled(config) -> bool:
    """工作流总开关（config.workflow.enabled）：默认关闭=直接对话，设置页显式启用"""
    return bool(((getattr(config, "data", None) or {}).get("workflow") or {}).get("enabled", False))


_DIRECT_EXECUTE_NODE = {"id": "direct", "type": "execute", "max_rounds": 12, "model_role": "code"}


def run_turn(workflow: Optional[dict], turn: TurnContext) -> str:
    """回合入口：workflow=None（未启用工作流）直接对话，否则按节点链运行"""
    if workflow is None:
        return _run_direct(turn)
    return run_workflow(workflow, turn)


def _run_direct(turn: TurnContext) -> str:
    """直接对话（工作流未启用）：注入系统提示词后进工具循环，不经节点链。

    行为与最简 default 工作流等价，但作为一等状态存在——工作流是显式启用的
    处理管线，而非"永远套着一层看不见的默认链"。"""
    from core import skills as skills_mod

    skills_mod.set_injection(enabled=None, allow=None)
    _exec_system_prompt({}, turn)
    _exec_execute(dict(_DIRECT_EXECUTE_NODE), turn)
    return "complete"
