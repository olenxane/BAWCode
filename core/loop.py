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
        wait_llm_retry: Optional[Callable[[str], bool]] = None,
    ):
        self.status = status or (lambda text: None)
        self.choose = choose
        self.line = line
        self.cancelled = cancelled or (lambda: False)
        self.on_delta = on_delta
        self.clear_stream = clear_stream or (lambda: None)
        self.tool_confirm = tool_confirm
        self.phase = phase or (lambda text: None)
        # API 错误手动重试闸：瞬态错误自动重试耗尽后调用，返回 True 重发同一请求
        self.wait_llm_retry = wait_llm_retry


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


def drain_injections(turn: TurnContext) -> int:
    """把回合进行中到达的插话并入当前会话（作为 user 消息），返回并入条数。

    插话由主线程入队（app.inject_queue），回合循环在每轮取走——下一轮
    build_messages 即包含它，模型在当前回合内看到并回应；回合收尾前再取一次，
    有插话则多跑一轮，避免插话落在末尾被丢弃。"""
    app = turn.app
    take = getattr(app, "take_injections", None)
    pending = take() if callable(take) else []
    for text in pending:
        turn.session.add_message("user", text, type="task")
    if pending:
        turn.io.status(f"插话 {len(pending)} 条 · 并入当前回合")
        log.info("插话并入当前回合: %d 条", len(pending))
    return len(pending)


def ensure_system_prompt(turn: TurnContext, files=None) -> None:
    """注入系统提示词：写入 turn.system_prompt_text，会话无 system_prompt 记录时补一条"""
    # get_system_prompt 内部已带兜底回落，不会向外抛错
    turn.system_prompt_text = get_system_prompt(config=turn.config, files=files)
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
    """工具调用循环：返回 {"status", "content"}

    status: complete（模型不再调用工具，回合完成）/ error / max_rounds。
    取消/中断抛 TurnInterrupt。轮次用尽且空转计数未满时续期（续期总量受
    config.workflow.max_total_rounds 兜底保险丝约束，默认 100，正常任务碰不到）：
    空转计数器（SpinGuard——重复/近似重复输出才开始计数，整轮全新结果清零）计满
    config.workflow.spin_kill_count 才终止回合，正常推进的回合可以一直跑到保险丝。"""
    io, session, llm = turn.io, turn.session, turn.llm
    wf_cfg = (getattr(turn.config, "data", None) or {}).get("workflow") or {}
    try:
        spin = SpinGuard(int(wf_cfg.get("spin_kill_count", 12)))
    except (TypeError, ValueError):
        spin = SpinGuard(12)
    try:
        fuse_cfg = int(wf_cfg.get("max_total_rounds") or 0)
    except (TypeError, ValueError):
        fuse_cfg = 0
    # 续期保险丝：只拦真失控，可调且不小于单轮 max_rounds
    total_cap = fuse_cfg if fuse_cfg > 0 else max(100, max(1, max_rounds))
    round_no = 0
    while True:
        if round_no >= max(1, max_rounds):
            if round_no >= total_cap:
                reason = f"回合计达兜底上限 {total_cap} 轮，终止回合（可调 config.workflow.max_total_rounds）"
                session.add_message("assistant", f"{reason}。")
                log.warn("%s", reason)
                io.status("回合终止")
                return {"status": "max_rounds", "content": ""}
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
        while True:
            io.phase("思考中")
            drain_injections(turn)  # 回合中插话：并入本回合，下一轮模型即看到
            payload = session.build_messages(extra_system=extra_system)
            llm.meter.measure_context(payload)
            response = llm.chat(payload, tools=register.get_tool_defs(), model=model, on_delta=io.on_delta)
            io.clear_stream()  # 树尾直播收口：正式消息按现有路径入库
            if turn.app is not None:
                turn.app.token_meter = llm.meter
            if response.get("error") == CANCELLED or io.cancelled():
                raise TurnInterrupt()
            if (
                response.get("error")
                and response.get("retryable")
                and io.wait_llm_retry is not None
            ):
                # 手动重试等待态：Ctrl+Y 重发同一请求（不计轮次、不写消息），放弃则走错误路径
                io.phase("")  # 停掉思考中转轮，状态行交给等待态提示
                retry_requested = io.wait_llm_retry(str(response["error"]))
                check_cancel(turn)
                if retry_requested:
                    io.status(f"重试 · 第{round_no + 1}轮")
                    continue
            break
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
            if drain_injections(turn):
                # 回合收尾前有插话：不结束，再跑一轮回应插话（仍属当前回合）
                io.status("插话并入 · 继续")
                continue
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
        for call in tool_calls:
            call["id"] = call.get("id") or f"call_{uuid.uuid4().hex[:12]}"
        thinking_extra = {"thinking": response["reasoning"]} if response.get("reasoning") else {}
        session.add_message(
            "assistant",
            response.get("content") or "",
            type="tool_call",
            tool_calls=[
                {
                    "id": c["id"],
                    "name": c.get("name"),
                    "arguments": c.get("arguments") or {},
                    "type": c.get("type") or "function",
                }
                for c in tool_calls
            ],
            **thinking_extra,
        )
        registered = 0
        round_results = []
        log.debug("第%d轮返回 %d 个工具调用", round_no + 1, len(tool_calls))
        try:
            for call in tool_calls:
                check_cancel(turn)
                action, reason = llm.evaluate_tool(call.get("name"), call.get("arguments") or {})
                check_cancel(turn)
                if action == policy.ALLOW:
                    io.phase(f"工具调用中 · {call.get('name')}")
                    check_cancel(turn)
                    result = llm.execute_approved_tool(call)
                elif action == policy.CONFIRM:
                    io.phase(f"待确认 · {call.get('name')}")
                    check_cancel(turn)
                    if io.tool_confirm is None:
                        result = {"content": policy.default_reject_message("无确认通道")}
                    else:
                        # 确认桥负责确认后的取消门禁与执行，返回工具结果
                        result = io.tool_confirm(call)
                else:
                    result = {"content": policy.default_reject_message(reason)}
                msg = session.add_tool_result(call, result["content"], result.get("images"))
                registered += 1
                if msg:
                    round_results.append(msg)
                check_cancel(turn)
                io.status(f"工具 {registered}/{len(tool_calls)} 完成 · {call.get('name')}")
        finally:
            # 中断或异常时补齐尚未返回的调用，保持协议配对
            for call in tool_calls[registered:]:
                session.add_tool_result(call, "[回合中断，该调用未获得结果]")

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
    max_rounds = _DIRECT_EXECUTE_NODE["max_rounds"]
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
