# -*- coding: utf-8 -*-
"""AgentLoop核心代码

本模块承载与"是否启用工作流"无关的回合执行机制：
  TurnIO / TurnContext  —— UI 桥回调集合与单回合上下文（引擎经此移交交互，不依赖 UI）
  tool_loop             —— 工具调用循环（直接对话主循环与工作流节点共用）：
                            权限判定/确认桥/轮次续期（防空转）/上下文压缩/token 计量
  ensure_system_prompt  —— 系统提示词注入（system_prompt 节点与直接对话共用）
  run_direct            —— 直接对话（工作流未启用）：注入系统提示词后进工具循环

取消/中断抛 TurnInterrupt（调用方按中断语义收尾 _aborted）；回合正常终止
（用户取消任务/计划未确认/推理出错/轮次上限）抛 TurnStop。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, Optional

from core import memory as memory_mod
from core import policy
from core import prompt_loader
from core import register
from core import snapshot as snapshot_mod
from core import subagent as subagent_mod
from core.llm import CANCELLED
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
        # API 错误手动重试闸：错误等待用户操作，返回 True 重发同一请求
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
        self.system_prompt_parts: Optional[Dict[str, int]] = None
        self.captured: Dict[str, Any] = {}
        self._last_autosave = time.monotonic()


def check_cancel(turn: TurnContext) -> None:
    if turn.io.cancelled():
        raise TurnInterrupt()


def drain_injections(turn: TurnContext) -> int:
    """把回合进行中到达的插话并入当前会话（作为 user 消息），返回并入条数。

    用户插话与后台任务完成通知都由 runner 放入 app.inject_queue；每轮边界取走，
    下一轮 build_messages 即包含，模型在当前回合内看到并回应；收尾前再取一次，
    避免通知恰好落在回合尾部而无人处理。"""
    app = turn.app
    take = getattr(app, "take_injections", None)
    pending = take() if callable(take) else []
    if not isinstance(pending, list):
        pending = []
    pending.sort(key=lambda item: item.get("_submission_seq", 0) if isinstance(item, dict) else 0)
    for item in pending:
        text = str(item.get("text") or "") if isinstance(item, dict) else str(item or "")
        snapshot_mod.add_task_message(text, source="background" if isinstance(item, dict) and item.get("background_call") else "inject")
        turn.session.add_message(
            "user",
            text,
            type="subagent_notice" if text.startswith("[子代理完成通知") else "task",
            injected=True,
        )
    if pending:
        turn.io.status(f"新消息 {len(pending)} 条 · 并入当前回合")
        log.info("回合注入新消息: %d 条", len(pending))
    return len(pending)


def ensure_system_prompt(turn: TurnContext, files=None) -> None:
    """注入系统提示词：写入 turn.system_prompt_text，会话无 system_prompt 记录时补一条两段式（core + environment 目录动态拼装 + 记忆占位符），会话首次生成后按 files
    缓存于 session——记忆在会话初始化时读入并固定，本会话后续回合不再重读/刷新；新会话随新 session 对象自然重建。"""
    cache_key = tuple(files or [])
    cache = getattr(turn.session, "system_prompt_cache", None)
    parts_cache = getattr(turn.session, "system_prompt_parts_cache", None)
    if isinstance(cache, dict) and cache_key in cache:
        turn.system_prompt_text = cache[cache_key]
        turn.system_prompt_parts = (parts_cache or {}).get(cache_key)
        log.debug("系统提示词命中会话缓存: %s", cache_key or ("default",))
    else:
        extra_vars: Dict[str, str] = {}
        mem_vars_fn = getattr(turn.session, "memory_prompt_variables", None)
        if callable(mem_vars_fn):
            try:
                mem_vars = mem_vars_fn()
                if isinstance(mem_vars, dict):
                    extra_vars.update(mem_vars)
                else:
                    log.debug("记忆占位符类型异常（非 dict），忽略: %r", type(mem_vars))
            except Exception as e:
                log.debug("记忆占位符构建失败（空占位兜底）: %s", e)
        variables = prompt_loader.build_variable_context(config=turn.config, extra=extra_vars)
        parts = prompt_loader.get_system_prompt_parts(
            config=turn.config, files=files, variables=variables
        )
        turn.system_prompt_text = "\n\n".join(p for p in parts.values() if p)
        # 记忆段单独计量：分类预算里从系统提示词拆出 memory 分项
        mem_tokens = 0
        try:
            mem_text = prompt_loader.load_environment_sections(
                variables, config=turn.config, files=["memory.md"]
            )
            mem_tokens = turn.llm.meter.count_text(mem_text)
        except Exception as e:
            log.debug("记忆段计量失败（并入系统提示词计）: %s", e)
        turn.system_prompt_parts = {"memory": mem_tokens}
        if not isinstance(cache, dict):
            cache, parts_cache = {}, {}
            turn.session.system_prompt_cache = cache
            turn.session.system_prompt_parts_cache = parts_cache
        cache[cache_key] = turn.system_prompt_text
        if isinstance(parts_cache, dict):
            parts_cache[cache_key] = turn.system_prompt_parts
        log.info(
            "生成系统提示词: %s（core+environment 两段式，记忆已并入并固定）",
            ", ".join(files or prompt_loader.system_prompt_files(turn.config)),
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


def _remeasure(turn: TurnContext) -> None:
    """回合管线完成点补计量：无副作用重组包后分类计量，刷新 UI/远控的上下文数字

    每轮工具结果入库后与回合收尾（剥离/外置/压缩）后各调一次——空闲时数字反映
    管线处理后的最新上下文，而不是停留在最后一次请求前。失败静默（辅助信息）。"""
    try:
        msgs = turn.session.assemble_payload(turn.system_prompt_text or None)
        turn.llm.meter.measure_context(
            msgs,
            tools_grouped=register.get_tool_defs_grouped(),
            system_parts=turn.system_prompt_parts,
        )
    except Exception as e:
        log.debug("管线后计量刷新失败: %s", e)


def tool_loop(turn: TurnContext, extra_system: Optional[str], max_rounds: int, model: Optional[str]) -> dict:
    """工具调用循环：返回 {"status", "content"}

    status: complete（模型本轮不再调用工具）/ error / max_rounds。
    取消/中断抛 TurnInterrupt。轮次用尽且空转计数未满时续期——正常推进的回合不设次数上限，
    config.workflow.max_total_rounds 是可选的兜底保险丝（0/负数 = 不启用）：
    空转计数器（SpinGuard——重复/近似重复输出才开始计数，整轮全新结果清零）计满config.workflow.spin_kill_count 才终止回合"""
    io, session, llm = turn.io, turn.session, turn.llm
    wf_cfg = (getattr(turn.config, "data", None) or {}).get("workflow") or {}

    def _autosave_tick() -> None:
        """回合内检查点：memory.autosave_minutes 为距上次落盘的最小间隔（0/缺失=关闭），
        有未保存变更才写盘；失败仅记日志不炸回合"""
        cfg = (getattr(turn.config, "data", None) or {}).get("memory") or {}
        try:
            minutes = float(cfg.get("autosave_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        if minutes <= 0 or not getattr(session, "_dirty", True):
            return
        if time.monotonic() - turn._last_autosave < minutes * 60:
            return
        turn._last_autosave = time.monotonic()
        try:
            if session.save_session():
                log.info("回合内自动保存: %s", session.session_id)
        except Exception as exc:
            log.warn("回合内自动保存失败: %r", exc)
    try:
        spin = SpinGuard(int(wf_cfg.get("spin_kill_count", 12)))
    except (TypeError, ValueError):
        spin = SpinGuard(12)
    try:
        fuse_cfg = int(wf_cfg.get("max_total_rounds") or 0)
    except (TypeError, ValueError):
        fuse_cfg = 0
    # 兜底保险丝：默认不启用（0/负数 = 无限制），正常推进的回合可无限续期
    total_cap = fuse_cfg if fuse_cfg > 0 else float("inf")
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
                reason = f"空转计数满 {spin.count} 次，判定Agent可能进入错误循环，终止回合"
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
        io.status(f"推理 · 第{round_no}轮")
        empty_retries = 0
        content, reasoning, tool_calls = "", "", []
        while True:
            io.phase("思考中")
            drain_injections(turn)  # 回合中插话：并入本回合，下一轮模型即看到
            _autosave_tick()
            builtin_defs, mcp_defs = register.get_tool_defs_grouped()
            tool_defs = builtin_defs + mcp_defs
            payload = session.build_messages(extra_system=extra_system, tools=tool_defs, model=model)
            llm.meter.measure_context(
                payload,
                tools_grouped=(builtin_defs, mcp_defs),
                system_parts=turn.system_prompt_parts,
            )
            response = llm.chat(payload, tools=tool_defs, model=model, on_delta=io.on_delta)
            io.clear_stream()  # 树尾直播收口：正式消息按现有路径入库
            if turn.app is not None:
                turn.app.token_meter = llm.meter
            if response.get("error") == CANCELLED or io.cancelled():
                raise TurnInterrupt()
            if response.get("error") and io.wait_llm_retry is not None:
                # 自动重试耗尽或确定性错误均允许用户 Ctrl+Y 手动重发同一请求；
                # 不计轮次、不写消息；放弃（Esc/提交新消息）落入错误路径
                io.phase("")  # 停掉思考中转轮，状态行交给等待态提示
                retry_requested = io.wait_llm_retry(str(response["error"]))
                check_cancel(turn)
                if retry_requested:
                    io.status(f"重试 · 第{round_no}轮")
                    continue
            if response.get("error"):
                break
            content: str = response.get("content") or ""
            reasoning: str = response.get("reasoning") or ""
            tool_calls: list = response.get("tool_calls") or []
            if not content.strip() and not reasoning.strip() and not tool_calls:
                if empty_retries < 2:
                    empty_retries += 1
                    io.status(f"空回复补试 {empty_retries}/2")
                    log.warn("第%d轮模型空回复，补试", round_no)
                    continue
                reason = "[回合异常：模型未返回有效内容]"
                session.add_message("assistant", reason, type="error")
                turn.captured["turn_error"] = "empty_response"
                turn.captured["turn_failed"] = True
                io.status("出错")
                log.error("第%d轮模型空回复，放弃回合", round_no)
                return {"status": "error", "content": ""}
            break
        if response.get("error"):
            log.error("第%d轮推理返回错误: %s", round_no, response["error"])
            session.add_message("assistant", response["error"])
            turn.captured["turn_error"] = response["error"]  # 无头 --json 的 error 字段来源
            io.status("出错")
            return {"status": "error", "content": ""}
        if (content or reasoning) and not tool_calls:
            # thinking 与正文并存时以 thinking 字段随消息留档（回合内出站并回 content，回合末剥离）
            extra = {"thinking": reasoning} if reasoning else {}
            session.add_message("assistant", content, **extra)
        if not tool_calls:
            if drain_injections(turn):
                # 回合收尾前有用户插话或后台子代理完成通知：不结束，下一轮回应
                io.status("新消息并入 · 继续")
                continue
            io.phase("")  # 本轮结束：状态行空行占位
            log.info("模型本轮未请求更多工具，回合结束（共%d轮推理）", round_no)
            if not io.cancelled():
                # 先收敛工具记录，再压缩，避免摘要模型看到尚未外置的原始大输出
                finalize = getattr(session, "finalize_turn", None)
                if callable(finalize):
                    finalize()
                session.maybe_compress(llm_fn=memory_mod.summary_llm_fn(llm))
            _remeasure(turn)  # 回合收尾（剥离/外置/压缩）后刷新上下文数字
            io.status("就绪")
            return {"status": "complete", "content": content}

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
        # 同步到 UI 快照：工具确认弹窗期间主线程读 self.messages 渲染树，
        # 否则本轮 assistant(tool_call) 节点要等下轮 io.status 才出现
        if turn.app is not None:
            turn.app.refresh_from_session(session, render=False)
        registered = 0
        round_results = []
        log.debug("第%d轮返回 %d 个工具调用", round_no, len(tool_calls))
        #task工具自身已立即提交后台任务；同轮多个task自然连续返回，不在此join
        try:
            for call in tool_calls:
                check_cancel(turn)
                action, reason = llm.evaluate_tool(call.get("name"), call.get("arguments") or {})
                check_cancel(turn)
                if action == policy.ALLOW:
                    io.phase(f"工具调用中 · {call.get('name')}")
                    check_cancel(turn)
                    result = llm.execute_approved_tool(call)
                elif action == policy.LLM_APPROVE:
                    io.phase(f"LLM审批中 · {call.get('name')}")
                    check_cancel(turn)
                    if llm.approve_tool_call(call):
                        result = llm.execute_approved_tool(call)
                    else:
                        result = {"content": policy.approval_reject_message("LLM审批拒绝或失败")}
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
                _autosave_tick()
        finally:
            # 中断或异常时补齐尚未返回的调用，保持协议配对
            for call in tool_calls[registered:]:
                session.add_tool_result(call, "[回合中断，该调用未获得结果]")

        # 空转计数：本轮结果喂入计数器
        spin.feed(round_results)

        if turn.app is not None:
            turn.app.token_meter = llm.meter
        _remeasure(turn)  # 每轮管线完成后刷新上下文数字
        io.status("继续")
    # while 循环不可达出口：轮次终止统一在循环内 max_rounds 分支返回


# 非工作流直接对话的伪节点：只承载轮数，不设模型角色（模型跟随激活模型）
_DIRECT_EXECUTE_NODE = {"id": "direct", "type": "execute", "max_rounds": 12}


def run_direct(turn: TurnContext) -> str:
    """直接对话（工作流未启用）：注入系统提示词后进工具循环，不经节点链。

    作为一等状态存在——工作流是显式启用的处理管线，而非"永远套着一层看不见的默认链"。
    模型跟随激活（默认）模型，分角色模型只在工作流节点里生效。"""
    from core import skills as skills_mod

    skills_mod.set_injection(enabled=None, allow=None)
    ensure_system_prompt(turn)
    turn.io.status("执行步骤")
    # config.workflow.max_rounds >0 时全局覆盖
    max_rounds = _DIRECT_EXECUTE_NODE["max_rounds"]
    wf_cfg = (getattr(turn.config, "data", None) or {}).get("workflow") or {}
    try:
        override = int(wf_cfg.get("max_rounds") or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        max_rounds = override
    # 非工作流：主对话跟随激活（默认）模型，model=None 由 llm 解析为当前激活模型
    result = tool_loop(turn, turn.system_prompt_text or None, max_rounds, model=None)
    if result["status"] == "error":
        # 请求级失败：标记回合收尾整体回滚（main._agent_turn 消费），失败信息不留在上下文
        turn.captured["turn_failed"] = True
    if result["status"] != "complete":
        raise TurnStop()
    return "complete"
