# -*- coding: utf-8 -*-
"""回合运行时：一轮回合的执行底座，与工作流节点编排（core/workflow.py）分离。

本模块承载与"是否启用工作流"无关的回合执行机制：
  TurnIO / TurnContext  —— UI 桥回调集合与单回合上下文（引擎经此移交交互，不依赖 UI）
  tool_loop             —— 工具调用循环（直接对话主循环与工作流节点共用）：
                            权限判定/确认桥/轮次续期（防空转）/上下文压缩/token 计量
  ensure_system_prompt  —— 系统提示词注入（system_prompt 节点与直接对话共用）
  run_direct            —— 直接对话（工作流未启用）：注入系统提示词后进工具循环

取消/中断抛 TurnInterrupt（调用方按中断语义收尾 _aborted）；回合正常终止
（用户取消任务/计划未确认/推理出错/轮次上限）抛 TurnStop。依赖单向：
core.workflow → 本模块；本模块不得 import core.workflow。
"""
from __future__ import annotations

import uuid
from typing import Callable, Dict, Optional

from core import policy
from core import prompt_loader
from core import register
from core.llm import CANCELLED, get_system_prompt
from core.log import get_logger
from core.tools import SpinGuard

log = get_logger("loop")


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
    """单回合上下文：回合执行机制与各节点共享的运行态与捕获变量"""

    def __init__(self, session, llm, app, config, user_text: str, io: TurnIO):
        self.session = session
        self.llm = llm
        self.app = app
        self.config = config
        self.user_text = user_text
        self.io = io
        self.system_prompt_text = ""
        self.captured: Dict[str, str] = {}


def check_cancel(turn: TurnContext) -> None:
    if turn.io.cancelled():
        raise TurnInterrupt()


def ensure_system_prompt(turn: TurnContext, files=None) -> None:
    """注入系统提示词：写入 turn.system_prompt_text，会话无 system_prompt 记录时补一条"""
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


def tool_loop(turn: TurnContext, extra_system: Optional[str], max_rounds: int, model: Optional[str]) -> dict:
    """工具调用循环（自 main.py 迁入 workflow、再归位回合运行时）：返回 {"status", "content"}

    status: complete（模型不再调用工具，回合完成）/ error / max_rounds。
    取消/中断抛 TurnInterrupt；cancel 检查点与消息入库路径与迁移前一致。
    轮次用尽时不再硬中断、也无续期次数上限：空转计数器（SpinGuard——重复输出才
    开始计数，整轮全新结果清零）计满 config.workflow.spin_kill_count 才终止回合，
    正常推进的回合可以一直跑。"""
    io, session, llm = turn.io, turn.session, turn.llm
    wf_cfg = (getattr(turn.config, "data", None) or {}).get("workflow") or {}
    try:
        spin = SpinGuard(int(wf_cfg.get("spin_kill_count", 12)))
    except (TypeError, ValueError):
        spin = SpinGuard(12)
    round_no = 0
    while True:
        if round_no >= max(1, max_rounds):
            if spin.spun_out():
                reason = f"空转计数满 {spin.count} 次（重复输出过多），终止回合"
                session.add_message("assistant", f"{reason}。")
                log.warn("%s", reason)
                io.status("空转终止")
                return {"status": "max_rounds", "content": ""}
            log.info(
                "轮次用尽，空转计数 %d/%d（未满），续期", spin.count, spin.kill_count
            )
            io.status(f"轮次续期 · 空转计数 {spin.count}/{spin.kill_count}")
        round_no += 1
        check_cancel(turn)
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
            turn.captured["turn_error"] = response["error"]  # 无头 --json 的 error 字段来源
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
                # 逐工具阶段提示：树底转轮行带上当前执行的工具名
                io.phase(f"工具调用中 · {call.get('name')}")
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

        round_results = []
        for call, item in allowed_results:
            msg = session.add_tool_result(call, item["content"], item.get("images"))
            if msg:
                round_results.append(msg)
        io.status(f"工具 {len(allowed_results)} 完成 · 待确认 {len(pending)}")

        for call in pending:
            # 确认面板等待期同样有阶段文案（带工具名，与执行中区分）
            io.phase(f"待确认 · {call.get('name')}")
            if io.tool_confirm is None:
                result = {"content": policy.default_reject_message("无确认通道")}
            else:
                result = io.tool_confirm(call)
            msg = session.add_tool_result(call, result["content"], result.get("images"))
            if msg:
                round_results.append(msg)
            check_cancel(turn)
            io.status(f"确认完成 · {call.get('name')}")

        # 空转计数：本轮结果喂入计数器（重复才计，整轮全新清零）
        spin.feed(round_results)

        if turn.app is not None:
            turn.app.token_meter = llm.meter
        io.status("继续")
    # while 循环不可达出口：轮次终止统一在循环内 max_rounds 分支返回


_DIRECT_EXECUTE_NODE = {"id": "direct", "type": "execute", "max_rounds": 12, "model_role": "code"}


def run_direct(turn: TurnContext) -> str:
    """直接对话（工作流未启用）：注入系统提示词后进工具循环，不经节点链。

    行为与最简 default 工作流等价，但作为一等状态存在——工作流是显式启用的
    处理管线，而非"永远套着一层看不见的默认链"。"""
    from core import skills as skills_mod

    skills_mod.set_injection(enabled=None, allow=None)
    ensure_system_prompt(turn)
    turn.io.status("执行步骤")
    # config.workflow.max_rounds >0 时全局覆盖（与 execute 节点的"设置页最大工具轮数"语义一致）
    try:
        max_rounds = int(_DIRECT_EXECUTE_NODE["max_rounds"])
    except (TypeError, ValueError):
        max_rounds = 12
    wf_cfg = (getattr(turn.config, "data", None) or {}).get("workflow") or {}
    try:
        override = int(wf_cfg.get("max_rounds") or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        max_rounds = override
    model = None
    if hasattr(turn.config, "get_task_model"):
        model = turn.config.get_task_model(_DIRECT_EXECUTE_NODE["model_role"])
    result = tool_loop(turn, turn.system_prompt_text or None, max_rounds, model=model)
    if result["status"] != "complete":
        raise TurnStop()
    return "complete"
