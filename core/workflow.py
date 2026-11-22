# -*- coding: utf-8 -*-
"""外置工作流引擎：data/workflows/<name>.json 声明式线性节点链，节点=Agent。

工作流是一套可自定义的执行流程，节点按列表顺序执行，每个节点代表一个 Agent
（不同 Agent 承担不同职责）。Agent 分内置与自定义两类：
  内置 Agent — understand/analyze/plan/execute/review/llm（原 LLM 类节点类型升级，
              保留各自专属交互：plan 确认菜单、analyze 复杂度路由、execute 步骤拆解）
  自定义 Agent — 引用 data/agents/<id>.json 智能体角色（id/系统提示词/模型/任务提示词），
              走通用执行器；system_prompt/skill 为非 Agent 注入节点，保留原语义

回合执行机制（UI 桥 TurnIO / 上下文 TurnContext / 工具调用循环 tool_loop /
系统提示词注入 / 直接对话 run_direct）归位回合运行时 core/loop.py，本模块
只负责节点链编排，经其执行（依赖单向：workflow → loop）。

执行模型（独立上下文交接）：每个 Agent 节点在独立隔离会话上运行（长期记忆只读、
不入全局台账），用户消息 = 工作流任务 + 节点任务提示词 + 前序节点产出摘要；
会话级工件（计划/步骤/会话状态工具）经线程绑定会话自然路由到显示会话
（主会话或后台实例骨架会话），workflow_node 节点标记也落显示会话——
链路推进在主会话时间线实时可见。

节点通用字段：agent（内置 id 或自定义角色 id，优先于旧 type 字段=内置 Agent）、
prompt（内置 Agent=阶段提示词覆盖；自定义 Agent=任务提示词覆盖，> 角色 task_prompt）、
prompt_files、model（> model_role > 角色模型 > 激活模型）、max_rounds、
override_system（true=节点提示词替换内置系统提示词，默认追加）、capture、enabled。

流程控制：引擎不依赖 UI——状态行/选择菜单/工具确认等经 TurnIO 回调移交；
取消/中断抛 TurnInterrupt（调用方 _aborted），回合正常终止（用户取消任务/
计划未确认/推理出错/轮次上限）抛 TurnStop。加载失败回退 DEFAULT_WORKFLOW。
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import memory as memory_mod
from core import policy
from core import prompt_loader
from core import register
from core import subagent as subagent_mod
from core.llm import CANCELLED
from core.log import get_logger
from core.loop import TurnContext, TurnInterrupt, TurnStop, check_cancel, ensure_system_prompt, tool_loop

log = get_logger("workflow")

_ROOT = Path(__file__).resolve().parent.parent

NODE_TYPES = ("system_prompt", "skill", "understand", "analyze", "plan", "execute", "review", "llm", "agent")

# 非 Agent 的注入节点：直接在主回合上下文执行，不建独立会话
_INJECTION_NODE_TYPES = ("system_prompt", "skill")

# 内置 Agent 注册表：id → 说明（执行器见 _EXECUTORS；旧格式 type 字段=内置 Agent 引用）
BUILTIN_AGENTS: Dict[str, str] = {
    "understand": "任务理解：解析任务意图，缺失信息经 ask_user 询问用户",
    "analyze": "复杂度判定：输出 high/low 写入 ^{complexity}^，供 plan 节点路由",
    "plan": "生成计划：plan 确认菜单交互，产物落显示会话",
    "execute": "编码执行：主工具调用循环（步骤拆解经 generate_steps）",
    "review": "审查与修正：审查本回合改动并反思修正",
    "llm": "通用 LLM 阶段：max_rounds>0 带工具循环，0 则单次问答",
}

# 节点类型 → 会话树显示名（workflow_node 标记消息用；未列出的类型原样显示）
NODE_LABELS = {
    "system_prompt": "系统提示词注入",
    "skill": "技能清单注入",
    "understand": "任务理解",
    "analyze": "复杂度判定",
    "plan": "生成计划",
    "execute": "执行编码",
    "review": "审查与修正",
    "llm": "LLM 阶段",
}

# 节点内置提示词兜底（默认引用 core/prompts/ 下同名 md，可被节点 prompt/prompt_files 覆盖）
JUDGE_PROMPT_BUILTIN = (
    "你是任务复杂度评审员。基于对话中的用户任务判断复杂度：\n"
    "- high：多文件/架构/系统级改动、需求模糊需要澄清、影响面大\n"
    "- low：单点小改动、问题清晰可直接执行\n\n"
    "只输出一行：high 或 low"
)
REVIEW_PROMPT_BUILTIN = (
    "你是代码审查节点。请对当前会话中的改动进行审查与修正：\n"
    "1. 梳理本回合的改动范围（读代码/查 git）\n"
    "2. 逐项审查：正确性、边界条件、风格一致性、遗漏的测试\n"
    "3. 发现问题直接修复（可使用 edit_file/write/run_command）\n"
    "4. 输出【审查结论】：发现的问题、已修正项、遗留风险\n\n"
    "只输出审查结论。"
)
UNDERSTAND_PROMPT_BUILTIN = (
    "你是任务理解节点。请解析用户当前任务的真实意图：\n"
    "1. 用一句话复述任务目标与验收标准\n"
    "2. 逐项检查执行所需信息是否完备（目标路径、范围、约束、偏好等）\n"
    "3. 发现缺失或歧义时，调用 ask_user 工具向用户提问（一次一个问题，问题具体、尽量给出候选项）\n"
    "4. 信息补齐后，输出【任务理解】：目标、约束、待办要点清单\n\n"
    "只输出任务理解本身，不要开始执行任务。"
)
# plan 节点 steps=true 时交付给 execute 的启动指令：步骤拆解由主 LLM 经 generate_steps
# 工具完成（结构化参数，出参受 schema 约束）
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


def list_workflows(config) -> List[str]:
    d = workflow_dir(config)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if p.is_file())


def node_agent_id(node: dict) -> str:
    """节点引用的 Agent id：agent 字段（内置 id 或自定义角色 id）优先；
    旧格式的 type 字段（非注入节点）视作内置 Agent 引用；注入节点返回空串"""
    agent = str(node.get("agent") or "").strip()
    if agent:
        return agent
    ntype = str(node.get("type") or "").strip()
    if ntype in BUILTIN_AGENTS:
        return ntype
    return ""


def validate_workflow(data: Any) -> List[str]:
    """结构校验：返回错误列表（空=通过）；不校验运行期语义（文件存在性、角色存在性等）"""
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
        agent = str(node.get("agent") or "").strip()
        if agent:
            pass  # agent 引用形态：type 可缺省（存在时不生效，agent 优先）
        elif ntype == "agent":
            errors.append(f"{label} type=agent 须同时提供非空 agent 字段（内置 id 或自定义角色 id）")
        elif ntype not in NODE_TYPES:
            errors.append(f"{label} 未知类型: {ntype!r}（或改用 agent 字段引用 Agent 角色）")
        nid = str(node.get("id") or "").strip()
        if not nid:
            errors.append(f"{label} 缺少 id")
        elif nid in ids:
            errors.append(f"{label} id 重复: {nid}")
        ids.add(nid)
    return errors


def _normalize_workflow(data: dict) -> dict:
    """规范化节点：补 id、enabled 缺省（宽松读取）"""
    normalized = []
    for index, node in enumerate(data["nodes"]):
        node = dict(node)
        node.setdefault("id", f"n{index + 1}")
        node.setdefault("enabled", True)
        normalized.append(node)
    data["nodes"] = normalized
    return data


def _agent_ref_errors(data: dict) -> List[str]:
    """Agent 引用存在性校验：内置 Agent 或已加载的自定义角色。
    角色表未加载（specs 为空，如测试/独立调用）时跳过校验、仅告警——
    防止 subagent.enabled=false 时自定义 Agent 节点静默降级成空角色执行"""
    specs = subagent_mod.get_specs()
    errors = []
    for index, node in enumerate(data.get("nodes") or []):
        if not isinstance(node, dict):
            continue  # 畸形元素由结构校验报告，这里不触碰其字段
        agent_id = node_agent_id(node)
        if not agent_id or agent_id in BUILTIN_AGENTS:
            continue
        if agent_id in specs:
            continue
        if specs:
            errors.append(f"节点#{index + 1} 引用的 Agent 不存在: {agent_id}")
        else:
            log.warn("节点#%d 引用自定义 Agent %s，但角色表未加载（subagent.enabled=false 或未初始化），"
                     "运行期将以空角色执行", index + 1, agent_id)
    return errors


def validate_workflow_full(data: Any) -> List[str]:
    """完整校验：结构 + Agent 引用存在性（/workflow run 命令与 WebUI 保存入口共用）。
    结构不合法时短路：畸形节点的形状无保证，引用校验不触碰其字段"""
    errors = validate_workflow(data)
    if errors:
        return errors
    return _agent_ref_errors(data)


def save_workflow(config, data: dict) -> Tuple[Optional[Path], str]:
    """保存预定义工作流（WebUI/命令入口共用）：完整校验 + 安全文件名 + 原子写。
    返回 (路径, 错误信息)；校验失败不落盘"""
    if not isinstance(data, dict):
        return None, "工作流定义须为对象"
    errors = validate_workflow_full(data)
    if errors:
        return None, "；".join(errors)
    name = str(data.get("name") or "").strip()
    if not name:
        return None, "缺少 name"
    data = copy.deepcopy(data)
    data["name"] = name
    _normalize_workflow(data)
    path = workflow_path(config, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        return None, f"写入失败: {e}"
    log.info("工作流已保存: %s（%d节点）", name, len(data.get("nodes") or []))
    return path, ""


def delete_workflow(config, name: str) -> Tuple[bool, str]:
    """删除预定义工作流文件；返回 (是否删除, 错误信息)"""
    name = str(name or "").strip()
    path = workflow_path(config, name)
    if not path.exists():
        return False, f"工作流不存在: {name}"
    try:
        path.unlink()
    except OSError as e:
        return False, f"删除失败: {e}"
    log.info("工作流已删除: %s", name)
    return True, ""


def builtin_agents() -> List[dict]:
    """内置 Agent 清单（WebUI 角色列表合并展示用，标记只读）"""
    return [{"id": aid, "description": desc, "builtin": True} for aid, desc in BUILTIN_AGENTS.items()]


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
    agent_errors = _agent_ref_errors(data)
    if agent_errors:
        for err in agent_errors:
            log.warn("工作流校验失败 %s: %s", path, err)
        log.warn("工作流 %s 引用了不存在的 Agent，使用内置默认工作流", name)
        fallback = copy.deepcopy(DEFAULT_WORKFLOW)
        fallback["name"] = name
        return fallback
    data["name"] = str(data.get("name") or name)
    _normalize_workflow(data)  # 规范化节点：补 id、enabled 缺省（宽松读取）
    return data


# ---------------------------------------------------------------------------
# 节点公共组装


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


def _ds(turn: TurnContext):
    """显示会话：节点标记与计划/步骤等会话级工件的落点（主会话或后台实例骨架会话）。

    Agent 节点在独立隔离会话上跑 LLM 上下文（turn.session），会话级工具经线程绑定
    会话（get_session）自然路由到这里；计划等工件必须落显示会话，落节点会话即死数据"""
    return getattr(turn, "display_session", None) or turn.session


def _stage_variables(turn: TurnContext) -> Dict[str, Any]:
    """节点提示词渲染变量：基础变量表 + 工作流任务 + 计划（显示会话）+ 回合捕获变量"""
    extra: Dict[str, Any] = {}
    extra.update(turn.captured)
    extra.setdefault("task", str(turn.captured.get("wf_task") or turn.user_text or ""))
    plan = getattr(_ds(turn), "plan", None) or {}
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
    ensure_system_prompt(turn, files)


def _exec_skill(node: dict, turn: TurnContext) -> None:
    """技能注入：只注入 list 指定的技能，list 为空则不注入任何技能"""
    from core import skills as skills_mod

    names = [str(n).strip() for n in (node.get("list") or []) if str(n).strip()]
    skills_mod.set_injection(enabled=bool(names), allow=names or None)


def _plan_confirm_loop(turn: TurnContext) -> None:
    """计划确认交互：0/1/2/3 菜单，修改后必须二次确认（计划工件落显示会话）"""
    io, session, llm = turn.io, _ds(turn), turn.llm
    confirmed = False
    for _edit_round in range(3):
        if io.choose is None:
            raise TurnStop()
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
            if io.line is None:
                raise TurnStop()
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
            if io.line is None:
                raise TurnStop()
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
        if io.choose is None:
            raise TurnStop()
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


def _plan_error(plan: dict) -> str:
    """计划结果是否失败：返回失败原因，空串表示有效计划"""
    if not isinstance(plan, dict):
        return "计划结果无效"
    error = str(plan.get("error") or "").strip()
    if error:
        return error
    content = str(plan.get("content") or "").strip()
    if not content or content == "暂无计划":
        return "计划内容为空"
    return ""


def _exec_plan(node: dict, turn: TurnContext) -> None:
    io, llm = turn.io, turn.llm
    session, ds = turn.session, _ds(turn)  # 计划生成读节点上下文；计划工件落显示会话
    if str(turn.captured.get("complexity") or "").strip().lower() == "low":
        # 复杂度路由：判定为 low 时跳过计划，直接进入后续节点
        log.info("复杂度 low，跳过计划节点")
        io.status("复杂度 low · 跳过计划")
        ds.add_message("system", "复杂度判定为 low，跳过计划生成。", type="help")
        return
    log.info("进入计划流程")
    io.status("生成计划")
    plan_model = _node_model(turn.config, node)
    if plan_model is None:
        plan_model = _role_model(turn.config, node.get("model_role") or "plan")
    try:
        retry_times = max(0, int(node.get("retry_times", 2)))
    except (TypeError, ValueError):
        retry_times = 2
    plan: dict = {}
    reason = ""
    for attempt in range(retry_times + 1):
        check_cancel(turn)
        plan = llm.generate_plan(
            turn.user_text, model=plan_model,
            messages=session.build_messages(extra_system=turn.system_prompt_text or None, model=plan_model),
        )
        check_cancel(turn)
        reason = _plan_error(plan)
        if not reason:
            break
        log.warn("计划生成失败(第%d/%d次): %s", attempt + 1, retry_times + 1, reason)
        if attempt < retry_times:
            io.status(f"计划生成失败 · 重试 {attempt + 2}/{retry_times + 1}")
    if reason:
        ds.add_message("assistant", f"计划生成失败（{reason}），任务中止。")
        io.status("计划生成失败")
        raise TurnStop()
    ds.set_plan(
        plan.get("title", "任务计划"),
        plan.get("content", ""),
        complexity=plan.get("complexity", "high"),
    )
    if node.get("confirm", True):
        _plan_confirm_loop(turn)
    else:
        # 免确认：直接置 confirmed，计划照常注入上下文
        ds.plan.update(llm.confirm_plan(ds.plan, "confirm"))
        ds.update_plan_status("confirmed")
        ds.save_longterm()
    if node.get("steps", True):
        # 步骤拆解归口主 LLM：execute 节点据该标记注入启动指令，经 generate_steps 工具落库
        turn.captured["steps_kickoff"] = "1"
    # 收尾消息附带 plan 快照（extras 不进 API）：会话树在该消息时间线位置渲染冻结的
    # 计划节点（core/ui.py._snapshot_nodes）；步骤不在此快照——由后续 generate_steps
    # 工具调用在时间线上以独立节点呈现
    ds.add_message(
        "assistant",
        "计划确认，开始执行。",
        type="plan",
        plan_snapshot=json.dumps(ds.plan, ensure_ascii=False),
    )


def _exec_analyze(node: dict, turn: TurnContext) -> None:
    """复杂度判定：独立一次 LLM 请求，输出 high/low；失败/不可解析回落 default_level"""
    io, session, llm = turn.io, turn.session, turn.llm
    default_level = str(node.get("default_level") or "low").strip().lower()
    if default_level not in ("high", "low"):
        default_level = "low"
    io.status("LLM 复杂度判定")
    io.phase("思考中")
    stage = _node_stage(turn, node, JUDGE_PROMPT_BUILTIN)
    model = _node_model(turn.config, node)
    if model is None:
        model = _role_model(turn.config, node.get("model_role") or "plan")
    payload = session.build_messages(extra_system=_stage_system(turn, node, stage), model=model)
    result = llm.chat(payload, model=model, on_delta=io.on_delta)
    io.clear_stream()
    check_cancel(turn)
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
    result = tool_loop(turn, _stage_system(turn, node, stage), max_rounds, model=model)
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
    _run_aux_loop(node, turn, UNDERSTAND_PROMPT_BUILTIN, 8, "plan")


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
        stage = _combine_system(stage, STEPS_KICKOFF_BUILTIN) or ""
    result = tool_loop(turn, _stage_system(turn, node, stage), max_rounds, model=model)
    if result["status"] != "complete":
        # 节点推理失败显式上浮为链路终止原因（回合级失败标志按 turn 隔离，不再经
        # 共享 captured 泄漏——实例收场状态依赖这里的专用键）
        if result["status"] == "error" and getattr(turn, "last_error", ""):
            turn.captured["node_error"] = str(turn.last_error)
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
        result = tool_loop(turn, _stage_system(turn, node, stage), max_rounds, model=model)
        content = result["content"]
        if result["status"] == "error":
            log.warn("工作流 llm 节点(id=%s)推理出错，继续后续节点", node.get("id"))
    else:
        # 单次问答：完整上下文（历史+记忆补充）+ 节点提示词，不带工具
        io.phase("思考中")
        payload = session.build_messages(extra_system=_stage_system(turn, node, stage), model=model)
        result = llm.chat(payload, model=model, on_delta=io.on_delta)
        io.clear_stream()
        check_cancel(turn)
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


# ---------------------------------------------------------------------------
# 节点=Agent：独立上下文执行机制

# Agent 节点不可用的工具：递归派发类（链内不许再派工作流/子代理）+ 项目级开关；
# 计划/步骤等会话状态工具保留——经线程绑定会话路由到显示会话（见 _run_node）
_NODE_EXCLUDED_TOOLS = {
    "task",
    "query_subagent",
    "wait_subagent",
    "cancel_subagent",
    "workflow",
    "set_rag",
}

# turn.captured 键：各 Agent 节点最终输出（交接用）与完整轨迹（记录/回看用）
_NODE_OUTPUTS_KEY = "node_outputs"
_NODE_TRANSCRIPTS_KEY = "node_transcripts"


def _node_tool_filter() -> "Callable[[str], bool]":
    excluded = _NODE_EXCLUDED_TOOLS

    def filter_fn(tool_name: str) -> bool:
        return tool_name not in excluded

    return filter_fn


def _handoff_cap(config) -> int:
    """交接摘要总字符上限（workflow.handoff_char_cap，默认 4000，下限 200）"""
    try:
        value = int(((getattr(config, "data", None) or {}).get("workflow") or {}).get("handoff_char_cap") or 4000)
    except (TypeError, ValueError):
        return 4000
    return max(200, value)


def _handoff_text(turn: TurnContext) -> str:
    """前序节点产出摘要：各 Agent 节点最终输出按序拼接；
    超 handoff_char_cap 时从最早的节点开始丢弃，保留节点仍超限则截其头部"""
    outputs = turn.captured.get(_NODE_OUTPUTS_KEY) or []
    segs = []
    for out in outputs:
        text = str(out.get("text") or "").strip()
        if text:
            segs.append(f"### 节点 {out.get('id')}（{out.get('agent')}）\n{text}")
    if not segs:
        return ""
    joined = "\n\n".join(segs)
    cap = _handoff_cap(turn.config)
    if len(joined) <= cap:
        return joined
    kept: List[str] = []
    used = 0
    for seg in reversed(segs):
        cost = len(seg) + 2
        if used + cost > cap:
            remain = cap - used
            if remain > 20:  # 残余空间足够时截头保留（cap=200 下限也不至于整段丢空）
                kept.append(seg[: remain - 20].rstrip() + "\n…（截断）")
            break
        kept.append(seg)
        used += cost
    return "\n\n".join(reversed(kept))


def _role_stage(turn: TurnContext, spec: dict) -> str:
    """自定义角色的系统提示词：prompt_file > prompt（渲染 ^{var}^，空角色回落空=仅基础系统提示词）"""
    variables = _stage_variables(turn)
    if spec.get("prompt_file"):
        segs = prompt_loader.load_prompt_bundle([str(spec["prompt_file"])], variables, config=turn.config)
        if segs:
            return prompt_loader.join_segments(segs)
        log.warn("角色提示词文件缺失，回退内联: %s", spec["prompt_file"])
    text = str(spec.get("prompt") or "").strip()
    return prompt_loader.render_text(text, variables) if text else ""


def _node_task_text(turn: TurnContext, node: dict, agent_id: str, spec: Optional[dict], is_builtin: bool) -> str:
    """Agent 节点的用户消息：工作流任务 + 节点任务提示词 + 前序节点产出。

    内置 Agent 的任务/阶段提示词走系统层（_node_stage 原语义），用户消息只带
    任务与交接；自定义 Agent 的节点提示词覆盖角色 task_prompt，作为任务指令。"""
    variables = _stage_variables(turn)
    wf_task = str(turn.captured.get("wf_task") or turn.user_text or "").strip()
    if is_builtin:
        task = ""
    else:
        task = str(node.get("prompt") or "").strip()
        if not task:
            task = prompt_loader.render_text(str((spec or {}).get("task_prompt") or ""), variables).strip()
    sections = []
    if wf_task:
        sections.append(f"[任务]\n{wf_task}")
    if task:
        sections.append(f"[节点指令]\n{task}")
    handoff = _handoff_text(turn)
    if handoff:
        sections.append(f"[前序节点产出]\n{handoff}")
    return "\n\n".join(sections) or "（无任务文本）"


def _exec_agent(node: dict, turn: TurnContext) -> None:
    """自定义 Agent 节点：角色系统提示词 + 角色/节点任务提示词，带工具循环；非致命
    （推理出错/轮次上限不阻断后续节点）"""
    agent_id = node_agent_id(node)
    spec = subagent_mod.get_role_spec(agent_id) or {}
    turn.io.status(f"Agent {agent_id}")
    model = _node_model(turn.config, node)
    if model is None:
        model = str(spec.get("model") or "").strip() or None
    default_rounds = subagent_mod.subagent_cfg(turn.config)["max_rounds"]
    try:
        max_rounds = int(node.get("max_rounds") or default_rounds)
    except (TypeError, ValueError):
        max_rounds = default_rounds
    wf_cfg = (getattr(turn.config, "data", None) or {}).get("workflow") or {}
    try:
        override = int(wf_cfg.get("max_rounds") or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        max_rounds = override
    stage = _role_stage(turn, spec)
    result = tool_loop(turn, _stage_system(turn, node, stage), max_rounds, model=model)
    if result["status"] == "error":
        log.warn("自定义 Agent 节点(id=%s agent=%s)推理出错，继续后续节点", node.get("id"), agent_id)
        turn.io.status("节点出错 · 继续")
    capture = str(node.get("capture") or "").strip()
    if capture and result.get("content"):
        turn.captured[capture] = result["content"].strip()


# 节点执行器注册表：注入节点（system_prompt/skill）+ 内置 Agent（旧 type 字段同键）
# + "agent"=自定义角色通用执行器；_run_node 按 node_agent_id 解析结果分发
_EXECUTORS: Dict[str, Callable[[dict, TurnContext], None]] = {
    "system_prompt": _exec_system_prompt,
    "skill": _exec_skill,
    "understand": _exec_understand,
    "analyze": _exec_analyze,
    "plan": _exec_plan,
    "execute": _exec_execute,
    "review": _exec_review,
    "llm": _exec_llm,
    "agent": _exec_agent,
}


def _run_node(node: dict, turn: TurnContext) -> None:
    """执行单个节点：注入节点在主回合上下文直跑；Agent 节点在独立隔离会话上执行，
    最终输出与轨迹写入 turn.captured 供交接与记录"""
    ntype = str(node.get("type") or "")
    agent_id = node_agent_id(node)
    ds = _ds(turn)
    # 节点标记落显示会话：树在该轮用户消息下按时间线显示节点推进
    # （type=workflow_node 不进 API、不参与压缩统计——memory 两处排除表同步）
    if agent_id in BUILTIN_AGENTS:
        label = NODE_LABELS.get(agent_id, agent_id)
    elif agent_id:
        label = f"Agent {agent_id}"
    else:
        label = NODE_LABELS.get(ntype, ntype or "节点")
    ds.add_message(
        "system",
        label,
        type="workflow_node",
        node_id=str(node.get("id") or ""),
        node_type=ntype,
        agent=agent_id or None,
    )
    log.info("工作流节点: %s（%s）", node.get("id"), agent_id or ntype)

    # 注入节点：非 Agent，直接在主回合上下文执行（原语义）；
    # type="agent" 但 agent 字段为空的畸形节点拒绝执行——绝不落主回合上下文
    # （会失去隔离会话与 _node_tool_filter，递归防线失效），校验层已拦截，此处兜底
    if not agent_id:
        if ntype == "agent":
            log.warn("节点 type=agent 缺少 agent 字段，跳过: id=%s", node.get("id"))
            return
        runner = _EXECUTORS.get(ntype)
        if runner is None:
            log.warn("未知节点类型，跳过: id=%s type=%r", node.get("id"), ntype)
        else:
            runner(node, turn)
        return

    # Agent 节点：独立隔离会话（长期记忆只读、不入全局台账），轨迹目录避开会话列表
    is_builtin = agent_id in BUILTIN_AGENTS
    spec = None if is_builtin else subagent_mod.get_role_spec(agent_id)
    task_text = _node_task_text(turn, node, agent_id, spec, is_builtin)
    session = memory_mod.Memory(turn.config, project_identity=getattr(turn.session, "project_identity", None), isolated=True)
    session.session_id = f"{getattr(turn.session, 'session_id', '') or 'wf'}-{node.get('id') or agent_id}"
    session.sessions_dir = _wf_runs_dir(turn.config) / "_nodes"
    session.add_message("user", task_text, type="task")
    node_turn = TurnContext(session=session, llm=turn.llm, app=turn.app, config=turn.config, user_text=task_text, io=turn.io)
    node_turn.display_session = turn.session
    node_turn.system_prompt_text = turn.system_prompt_text
    node_turn.system_prompt_parts = turn.system_prompt_parts
    node_turn.captured = turn.captured  # 共享引用：capture 变量与节点产出跨节点贯通
    node_turn.tool_filter = _node_tool_filter()
    try:
        runner = _EXECUTORS.get(agent_id if is_builtin else "agent")
        if runner is None:  # 角色表加载晚于校验的兜底（理论不发生）
            log.warn("Agent 角色缺失，跳过节点: id=%s agent=%s", node.get("id"), agent_id)
            return
        runner(node, node_turn)
    finally:
        messages = list(session.messages)
        final = _final_text(messages)
        turn.captured.setdefault(_NODE_TRANSCRIPTS_KEY, []).append(
            {"id": str(node.get("id") or ""), "agent": agent_id, "messages": messages}
        )
        turn.captured.setdefault(_NODE_OUTPUTS_KEY, []).append(
            {"id": str(node.get("id") or ""), "agent": agent_id, "text": final}
        )
        # 节点最终输出同步落显示会话：主循环/前台链路与旧共享模型同可见性。
        # 后台实例经 _run_workflow_turn 置 skip_standalone_final（final 已随节点轨迹
        # 并入记录，不重复落、防 rounds 虚增）；其余调用方（含无头桩）都落
        if final and not getattr(turn, "skip_standalone_final", False):
            ds.add_message("assistant", final)


def run_workflow(workflow: dict, turn: TurnContext) -> str:
    """按节点链顺序执行一轮回合；TurnInterrupt/TurnStop 冒泡给调用方收尾。

    节点=Agent：注入节点在主回合上下文直跑；Agent 节点在独立隔离会话执行，
    产出经 turn.captured 交接给后续节点，节点标记与计划等工件落显示会话。"""
    nodes = workflow.get("nodes") or []
    # skill 节点缺省时恢复默认注入（避免上一回合的过滤残留）
    from core import skills as skills_mod

    skills_mod.set_injection(enabled=None, allow=None)
    turn.captured.setdefault("wf_task", str(turn.user_text or ""))
    turn.captured.setdefault(_NODE_OUTPUTS_KEY, [])
    turn.captured.setdefault(_NODE_TRANSCRIPTS_KEY, [])
    turn.captured.pop("node_error", None)  # 链开始清残留：实例收场原因只反映本轮节点
    if not any(str(n.get("type") or "") == "system_prompt" for n in nodes):
        ensure_system_prompt(turn)  # 默认注入 config.prompt.system_files
    # 技能注入线程作用域：skill 节点的 set_injection 只改本回合/本实例，链结束自动还原
    with skills_mod.bind_thread_injection():
        for node in nodes:
            check_cancel(turn)
            if not node.get("enabled", True):
                log.debug("节点已停用，跳过: %s", node.get("id"))
                continue
            _run_node(node, turn)
    return "complete"


# ---------------------------------------------------------------------------
# 异步工作流实例：主代理经 workflow 工具派发，后台线程按节点链完整执行一轮。
# 任务注册表/编号记录/并发闸/直播槽/完成通知与子代理共用（wait/cancel/query 对
# 两族任务统一生效）；实例用隔离会话（不动全局台账、长期记忆只读、轨迹落独立目录），
# 交互节点（plan 确认菜单/understand 询问）经 UI 桥弹到主线程面板串行裁决。

_WF_RUNS_DIR = "data/workflows/runs"


def _wf_runs_dir(config) -> Path:
    rel = str(((getattr(config, "data", None) or {}).get("workflow") or {}).get("runs_dir") or _WF_RUNS_DIR)
    p = Path(rel)
    return p if p.is_absolute() else _ROOT / rel


class _WorkflowIO:
    """后台工作流实例的最小 IO 面：进度进直播槽，交互经主线程弹窗桥，取消来自实例事件。

    llm 由 worker 启动时注入（确认桥批准后的工具执行走实例的独立 LLM）。"""

    def __init__(self, task: dict):
        self.task = task
        self.llm = None
        self.choose = task.get("choose_bridge")
        self.line = task.get("line_bridge")
        # LLM 错误手动重试闸：后台实例无用户等待通道，恒 None（tool_loop 走错误收尾）
        self.wait_llm_retry = None

    def cancelled(self) -> bool:
        return self.task["cancel"].is_set()

    def status(self, text: str) -> None:
        self._phase(str(text))

    def phase(self, text: str) -> None:
        self._phase(str(text))

    def _phase(self, text: str) -> None:
        slot = subagent_mod.live_slot(self.task["runtime"], str(self.task["id"]))
        if slot is not None:
            slot["phase"] = text
        subagent_mod.update_task(self.task, phase=text)

    def on_delta(self, kind: str, piece: str) -> None:
        slot = subagent_mod.live_slot(self.task["runtime"], str(self.task["id"]))
        if slot is None:
            return
        if kind == "reset":
            slot["text"] = ""
        elif kind == "content" and piece:
            slot["text"] = (slot.get("text") or "")[-1200:] + piece

    def clear_stream(self) -> None:
        slot = subagent_mod.live_slot(self.task["runtime"], str(self.task["id"]))
        if slot is not None:
            slot["text"] = ""

    def tool_confirm(self, call: dict) -> dict:
        bridge = self.task.get("confirm_bridge")
        if bridge is None or self.llm is None or self.cancelled():
            return {"content": policy.default_reject_message("无确认通道或实例已取消")}
        return bridge(self.llm, call, self.cancelled)


@contextmanager
def _wf_scope(task: dict, session):
    """后台实例的线程绑定栈：隔离会话、技能注入过滤作用域、交互申请桥、文件快照上下文"""
    from core import skills as skills_mod
    from core import snapshot as snapshot_mod
    from core import tools as tools_mod

    with ExitStack() as stack:
        stack.enter_context(memory_mod.bind_session(session))
        # 技能注入过滤线程作用域：skill 节点的 set_injection 只改本线程，不污染主回合
        stack.enter_context(skills_mod.bind_thread_injection())
        # 交互申请桥（线程级）：主回合结束后实例仍可弹面板（DIALOG_LOCK 串行）
        ask_bridge = task.get("ask_bridge")
        if callable(ask_bridge):
            stack.enter_context(tools_mod.bind_thread_ask_bridge(ask_bridge))
        mode_bridge = task.get("mode_bridge")
        if callable(mode_bridge):
            cancelled = task["cancel"].is_set
            stack.enter_context(tools_mod.bind_thread_mode_bridge(lambda payload: mode_bridge(payload, cancelled)))
        stack.enter_context(
            snapshot_mod.bind_context(task["config"], task["project_id"], task["session_id"], task["id"])
        )
        yield


def _final_text(messages: List[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "assistant" and str(m.get("content") or "").strip():
            return str(m["content"])
    return ""


def _merge_node_transcripts(skeleton: List[dict], transcripts: List[dict]) -> List[dict]:
    """把各节点独立上下文的轨迹拼回骨架时间线：workflow_node 标记后接该节点消息，
    query_subagent 与会话树 trace 据此可见完整链路；无对应标记的轨迹兜底追加"""
    if not transcripts:
        return list(skeleton)
    by_id: Dict[str, List[dict]] = {}
    order: List[str] = []
    for tr in transcripts:
        nid = str(tr.get("id") or "")
        msgs = list(tr.get("messages") or [])
        if nid in by_id:
            by_id[nid].extend(msgs)
        else:
            by_id[nid] = msgs
            order.append(nid)
    merged: List[dict] = []
    consumed = set()
    for m in skeleton:
        merged.append(m)
        if m.get("type") == "workflow_node":
            nid = str(m.get("node_id") or "")
            if nid in by_id and nid not in consumed:
                merged.extend(by_id[nid])
                consumed.add(nid)
    for nid in order:
        if nid not in consumed:
            merged.extend(by_id[nid])
    return merged


def _run_workflow_turn(task: dict, record: dict, wf_llm) -> Tuple[str, str]:
    """在隔离骨架会话里按节点链执行一轮回合；Agent 节点各自独立上下文，返回 (status, final_text)"""
    config = task["config"]
    name = str(task.get("workflow") or "")
    wf = task.get("workflow_def") if isinstance(task.get("workflow_def"), dict) else None
    if wf is None:
        wf = load_workflow(config, name)
    project_identity = getattr(task.get("session"), "project_identity", None)
    session = memory_mod.Memory(config, project_identity=project_identity, isolated=True)
    session.session_id = f"wf-{task['id']}-{name}"
    session.sessions_dir = _wf_runs_dir(config) / str(task.get("project_id") or "_default")
    task["runtime"]["wf_session"] = session
    prompt = str(record.get("task") or "")
    session.add_message("user", prompt, type="task")
    turn = TurnContext(session=session, llm=wf_llm, app=None, config=config, user_text=prompt, io=task["runtime"]["io"])
    turn.skip_standalone_final = True  # 后台实例：节点产物不重复落骨架（rounds 统计防虚增）
    # 骨架会话自身的工具面兜底（Agent 节点各自带 _node_tool_filter 的独立上下文）
    turn.tool_filter = _node_tool_filter()
    with _wf_scope(task, session):
        try:
            run_workflow(wf, turn)
            outs = turn.captured.get(_NODE_OUTPUTS_KEY) or []
            final = str(outs[-1].get("text") or "").strip() if outs else _final_text(session.messages)
            return "done", final
        except TurnInterrupt:
            return "interrupted", "实例被取消"
        except TurnStop:
            err = str(turn.captured.get("node_error") or "").strip()
            if err:
                return "error", err
            outs = turn.captured.get(_NODE_OUTPUTS_KEY) or []
            final = str(outs[-1].get("text") or "").strip() if outs else ""
            return "stopped", final or "工作流提前终止（计划未确认或节点终止）"
        finally:
            task["runtime"]["node_transcripts"] = list(turn.captured.get(_NODE_TRANSCRIPTS_KEY) or [])


def _build_workflow_summary(task: dict, record: dict, status: str, final_text: str, record_path: Optional[Path]) -> str:
    label = subagent_mod.status_label(status)
    cap = subagent_mod.subagent_cfg(task["config"])["result_char_cap"]
    body = (final_text or "").strip()
    if len(body) > cap:
        body = body[:cap] + f"\n…（超 {cap} 字截断，过程细节可 query_subagent 查看）"
    parts = [
        f"[工作流实例 #{record.get('id')} · {task.get('workflow')} · {label}"
        f" · {record.get('rounds', 0)}轮 · {record.get('tools_used', 0)}次工具]"
    ]
    if status == "error" and final_text:
        parts.append(f"出错原因: {final_text}")
    else:
        parts.append(body or "（无文本输出）")
    arts = record.get("artifacts") or []
    if arts:
        parts.append("产物:\n" + "\n".join(f"- {a}" for a in arts))
    if record_path:
        parts.append(f"完整轨迹: {record_path}")
    parts.append(f"过程详情可用 query_subagent(id=\"{record.get('id')}\") 查询。")
    return "\n".join(parts)


def _workflow_worker(task: dict, record: dict, directory: Path, record_path: Optional[Path]) -> None:
    task_id = str(task["id"])
    status, final_text = "error", "后台工作流未启动"
    try:
        if task["cancel"].is_set():
            status, final_text = "interrupted", "实例在启动前被取消"
        else:
            with task["gate"]:
                if task["cancel"].is_set():
                    status, final_text = "interrupted", "实例在排队期间被取消"
                else:
                    from core.llm import LLM

                    wf_llm = LLM(task["config"])
                    task["runtime"]["llm"] = wf_llm
                    io = task["runtime"]["io"]
                    if io is not None:
                        io.llm = wf_llm
                    subagent_mod.update_task(task, status="running", phase="启动工作流")
                    record["status"] = "running"
                    record_path = subagent_mod.save_record(directory, record) or record_path
                    slot = subagent_mod.live_slot(task["runtime"], task_id)
                    if slot is not None:
                        slot.update({"id": task_id, "role": record.get("role"), "phase": "运行中", "text": "", "tools": []})
                    status, final_text = _run_workflow_turn(task, record, wf_llm)
    except Exception as exc:
        log.error("后台工作流实例 #%s 执行异常: %r", task_id, exc)
        status, final_text = "error", f"内部异常: {exc}"
    if task["cancel"].is_set() and status not in ("error",):
        status, final_text = "interrupted", "实例被取消"
    # 收尾：消息快照落记录（骨架时间线并入各节点轨迹，query_subagent 可回看）、
    # 产物提取、主会话树节点、摘要与完成通知。整段兜底：收尾崩溃不得让任务永久
    # 停在 running（done 不置位会令 wait_tasks 悬挂）——降级为 error 收场
    try:
        wf_session = (task.get("runtime") or {}).get("wf_session")
        skeleton = list(getattr(wf_session, "messages", []) or [])
        record["messages"] = _merge_node_transcripts(
            skeleton, (task.get("runtime") or {}).get("node_transcripts") or []
        )
        trace_messages = record["messages"]
        record["artifacts"] = subagent_mod.extract_artifacts(trace_messages)
        record["status"] = status
        record["rounds"] = sum(1 for m in trace_messages if m.get("role") == "assistant")
        record["tools_used"] = sum(1 for m in trace_messages if m.get("role") == "tool")
        record_path = subagent_mod.save_record(directory, record) or record_path
        record["record_path"] = str(record_path or "")
        main_session = task.get("session")
        if main_session is not None:
            try:
                main_session.add_message(
                    "assistant",
                    f"工作流实例 #{task_id} · {task.get('workflow')} · {subagent_mod.status_label(status)}"
                    f"（{record.get('rounds', 0)}轮/{record.get('tools_used', 0)}次工具）",
                    type="subagent",
                    subagent_id=task_id,
                    subagent_role=record.get("role"),
                    status=status,
                    trace=trace_messages,
                )
            except Exception as exc:
                log.warn("工作流实例 #%s 会话节点写入失败: %r", task_id, exc)
        summary = _build_workflow_summary(task, record, status, final_text, record_path)
        record["summary"] = summary
        subagent_mod.save_record(directory, record)
    except Exception as exc:
        log.error("后台工作流实例 #%s 收尾异常: %r", task_id, exc)
        status, final_text = "error", f"收尾异常: {exc}"
        record["status"] = status
        record["summary"] = f"[工作流实例 #{task_id} · 出错]\n{final_text}"
    subagent_mod.live_clear_slot(task["runtime"], task_id)
    subagent_mod.update_task(
        task,
        status=status,
        phase="完成" if status == "done" else subagent_mod.status_label(status),
        rounds=record.get("rounds", 0),
        tools_used=record.get("tools_used", 0),
        summary=record.get("summary") or "",
        error=final_text if status == "error" else "",
        record=record,
    )
    task["done"].set()
    # 完成通知：摘要经 runner.notify 注入主会话（用户主动取消的实例不打扰）
    notify = getattr(task.get("runner"), "notify", None)
    if callable(notify) and status != "interrupted":
        notify(
            f"[工作流完成通知 id={task_id}]\n{record.get('summary') or ''}\n",
            origin_turn_id=task.get("origin_turn_id"),
        )


def submit_workflow_instance(rt: dict, name: str, prompt: str, workflow_def: Optional[dict] = None) -> dict:
    """派发后台工作流实例：编号/记录/任务注册表/并发闸与子代理共用，立即可 wait/cancel/query。
    workflow_def 为内联临时定义（工具 nodes 参数），传入时 worker 不再按 name 读盘"""
    config = rt["config"]
    session = rt.get("session")
    directory = subagent_mod.records_dir(config, session)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    wf_def = workflow_def if isinstance(workflow_def, dict) else None

    def _build(record_id: str) -> dict:
        return {
            "id": record_id,
            "kind": "workflow",
            "role": f"workflow:{name}",
            "workflow": name,
            "task": prompt,
            "status": "queued",
            "rounds": 0,
            "tools_used": 0,
            "artifacts": [],
            "messages": [],
            "created_at": now,
        }

    record, record_path = subagent_mod.create_record(directory, _build)
    task_id = str(record["id"])
    cancel = threading.Event()
    app = rt.get("app")
    if app is not None:
        from core import ui as ui_mod

        choose_bridge = ui_mod.make_choose_bridge(app, cancel.is_set)
        line_bridge = ui_mod.make_line_bridge(app, config, cancel.is_set)
    else:
        choose_bridge, line_bridge = None, None
    task = {
        "id": task_id,
        "kind": "workflow",
        "workflow": name,
        "workflow_def": wf_def,
        "project_id": str(getattr(session, "project_id", "") or ""),
        "session_id": str(getattr(session, "session_id", "") or ""),
        "config": config,
        "session": session,
        "runner": rt.get("runner"),
        "origin_turn_id": rt.get("origin_turn_id"),
        "app": app,
        "confirm_bridge": rt.get("confirm_bridge"),
        "mode_bridge": rt.get("mode_bridge"),
        "ask_bridge": subagent_mod._make_task_ask_bridge(app, cancel),
        "choose_bridge": choose_bridge,
        "line_bridge": line_bridge,
        "cancel": cancel,
        "gate": subagent_mod.session_gate(config, session),
        "done": threading.Event(),
        "status": "queued",
        "phase": "排队中",
        "rounds": 0,
        "tools_used": 0,
        "updated_at": now,
        "summary": "",
        "record": record,
    }
    task_runtime = {**rt, "llm": None, "io": _WorkflowIO(task), "task_id": task_id, "task": task}
    task["runtime"] = task_runtime
    subagent_mod.register_background_task(task)
    thread = threading.Thread(
        target=_workflow_worker,
        args=(task, record, directory, record_path),
        daemon=True,
        name=f"bawcode-workflow-{task_id}",
    )
    task["thread"] = thread
    thread.start()
    return task


# ---------------------------------------------------------------------------
# workflow 工具：主代理经此后台派发完整工作流实例（多个实例天然并行），
# 或 mode=foreground 内联阻塞执行整条链（主会话时间线实时显示节点链路）

# 前台执行重入闸：链路内节点的工具面已剔除 workflow，这里防的是病态嵌套调用
_FG_ACTIVE = threading.local()


def _resolve_tool_workflow(rt: dict, name: str, nodes: Any) -> Tuple[Optional[dict], str]:
    """工具入参 → 工作流定义：nodes 内联优先（临时自定义），否则按 name 读盘。
    返回 (workflow, 错误信息)；出错时 workflow 为 None"""
    config = rt["config"]
    if nodes is not None:
        if not isinstance(nodes, list) or not nodes:
            return None, "nodes 必须是非空节点数组（每项含 id 与 agent/type）"
        wf = {"name": "adhoc", "description": "临时自定义（工具内联节点）", "nodes": nodes}
        errors = validate_workflow(wf) + _agent_ref_errors(wf)
        if errors:
            return None, "内联工作流校验失败: " + "；".join(errors)
        _normalize_workflow(wf)
        return wf, ""
    name_text = str(name or "").strip()
    available = list_workflows(config)
    if not name_text:
        return None, (
            "name 不能为空（或改传 nodes 内联定义节点链）。可用工作流: "
            f"{', '.join(available) or '（无）'}（/workflow 查看）"
        )
    if name_text not in available:
        return None, f"工作流不存在: {name_text}。可用: {', '.join(available) or '（无）'}"
    return load_workflow(config, name_text), ""


def _chain_final_output(turn: TurnContext, session) -> str:
    """链路最终输出：最后一个 Agent 节点的产出；无 Agent 节点时回落骨架会话末条 assistant"""
    outs = turn.captured.get(_NODE_OUTPUTS_KEY) or []
    if outs:
        return str(outs[-1].get("text") or "").strip()
    return _final_text(getattr(session, "messages", []) or [])


def _run_foreground(rt: dict, wf: dict, prompt_text: str) -> str:
    """前台执行：本次工具调用阻塞到整条链跑完才返回（期间主代理零输出），
    节点链路直接落主会话时间线；链路结束返回最终输出"""
    if getattr(_FG_ACTIVE, "on", False):
        return "已有前台工作流在执行中，不能嵌套派发"
    session, llm, io = rt.get("session"), rt.get("llm"), rt.get("io")
    if llm is None or io is None or session is None:
        return "前台执行需要主回合运行时（llm/io/session），当前环境不可用"
    turn = TurnContext(session=session, llm=llm, app=rt.get("app"), config=rt["config"], user_text=prompt_text, io=io)
    if not turn.system_prompt_text:
        ensure_system_prompt(turn)
    _FG_ACTIVE.on = True
    try:
        run_workflow(wf, turn)
        return f"[工作流前台执行完成 · {len(wf.get('nodes') or [])}节点]\n{_chain_final_output(turn, session) or '（无文本输出）'}"
    except TurnInterrupt:
        return "[工作流前台执行被取消]"
    except TurnStop:
        err = str(turn.captured.get("node_error") or getattr(turn, "last_error", "") or "").strip()
        suffix = f"（{err}）" if err else "（计划未确认或节点终止）"
        return f"[工作流前台执行提前终止{suffix}]\n{_chain_final_output(turn, session) or '（无文本输出）'}"
    finally:
        _FG_ACTIVE.on = False


def _tool_workflow(name: str = "", prompt: str = "", mode: str = "", nodes: Any = None) -> str:
    rt = subagent_mod.runtime_snapshot()
    if rt is None:
        return "工作流运行时未绑定（只能在回合内派发）"
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        return "prompt 不能为空：应为自足完整的任务提示词（节点在独立上下文运行，只看到任务文本与交接产出）"
    wf, err = _resolve_tool_workflow(rt, name, nodes)
    if err or wf is None:
        return err
    mode_text = str(mode or "").strip().lower() or "background"
    if mode_text == "foreground":
        log.info("前台执行工作流: %s（%d节点）", wf.get("name"), len(wf.get("nodes") or []))
        return _run_foreground(rt, wf, prompt_text)
    if mode_text != "background":
        return f"mode 仅支持 background（默认，后台并行实例）或 foreground（阻塞执行整链）: {mode_text!r}"
    task = submit_workflow_instance(rt, str(wf.get("name") or "adhoc"), prompt_text, workflow_def=wf)
    task_id = str(task["id"])
    log.info("异步派发工作流实例 #%s name=%s", task_id, wf.get("name"))
    return (
        f"[工作流实例已异步派发] id={task_id} workflow={wf.get('name')} status=queued\n"
        f"它在后台按节点链完整执行一轮：可继续别的工作，或在同一回复里并行派发更多工作流实例；"
        f"wait_subagent(id=\"{task_id}\", timeout=秒) 等待（超时返回进度），"
        f"过程细节用 query_subagent(id=\"{task_id}\")，需要中止用 cancel_subagent(id=\"{task_id}\")。"
        f"完成后结果摘要会自动注入会话。"
    )


def register_tools(config) -> bool:
    """注册 workflow 工具；依赖子代理后台任务基建（subagent.enabled=false 时不注册）"""
    if not subagent_mod.subagent_cfg(config)["enabled"]:
        log.info("子代理系统未启用，跳过 workflow 工具注册")
        return False
    register.register(
        name="workflow",
        description=(
            "Run a workflow: a customizable pipeline whose nodes are agents — builtin stages "
            "(understand/analyze/plan/execute/review/llm) or custom agent roles from data/agents "
            "(see /workflow). mode=background (default) dispatches an isolated background instance "
            "and returns an id immediately; several run in parallel, wait_subagent(id, timeout)/"
            "query_subagent(id)/cancel_subagent(id) apply, and the result summary is auto-injected. "
            "mode=foreground runs the whole chain synchronously inside this conversation: the call "
            "blocks until the chain finishes and returns its final output, with node progress shown "
            "in the session timeline. Pass nodes to define an ad-hoc chain inline instead of a saved "
            "workflow name. The prompt must be self-contained: every node runs in an isolated context "
            "and only receives the task prompt plus the previous nodes' outputs"
        ),
        usage="workflow <name> <prompt> [mode] | workflow <prompt> nodes=[...]",
        schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Saved workflow name, e.g. \"default\"; list via /workflow. Required unless nodes is given",
                },
                "prompt": {
                    "type": "string",
                    "description": "Self-contained task prompt stating goal, scope and expected output",
                },
                "mode": {
                    "type": "string",
                    "enum": ["background", "foreground"],
                    "description": "background = async instance (default, parallel); foreground = block this call until the whole chain completes",
                },
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Node id"},
                            "agent": {
                                "type": "string",
                                "description": "Builtin agent id (understand/analyze/plan/execute/review/llm) or custom role id from data/agents",
                            },
                            "prompt": {"type": "string", "description": "Optional node prompt override"},
                            "max_rounds": {"type": "integer", "description": "Optional tool-loop round cap"},
                        },
                        "required": ["id"],
                    },
                    "description": (
                        "Ad-hoc inline node chain, order = execution order; overrides name. "
                        "Example: [{\"id\":\"n1\",\"agent\":\"understand\"},{\"id\":\"n2\",\"agent\":\"execute\",\"max_rounds\":10}]"
                    ),
                },
            },
            "required": ["prompt"],
        },
    )(_tool_workflow)
    log.info("工作流工具已注册")
    return True
