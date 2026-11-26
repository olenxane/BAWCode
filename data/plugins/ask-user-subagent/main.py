"""ask-user-subagent —— 用户子代理插件 v0.1.0（把任务委派给用户）。

主 Agent 调用 ask_user_as_subagent(task, context) 即把一个任务委派给"子代理"，
弹出任务卡片等待作答；用户提交的内容作为调用结果返回，在 Agent 看来与普通子代理
的回复一致——模型可见的文案始终以子代理相称，不暴露对面是人。

通道（除 safe 声明外零宿主改动）：
  - 卡片复用宿主 ask 交互面板（app.request_ui("ask")）：空选项直达自由输入态，
    自带倒计时与 Esc 交回；WebUI 在线时由 remote-control 的 ui_request 钩子推给
    浏览器代答（confirm_timeout 内未响应交回终端面板）
  - 免确认经 SDK register_tool(safe=True) 声明（policy.is_safe_call 采纳注册表
    safe 标志）：纯交互无副作用，与内置 ask_user 同待遇，委派前不再弹"能否调用"
  - 委派倾向 + 子代理说明经 user_message_supplement 焊入每条新用户消息（同
    emotion-detector 通道）；隔离会话/子代理线程的用户消息不触发焊接，
    引导不会漏进子代理上下文

委派倾向 delegation_bias 0-100 四档引导文案随设置实时生效（处理函数内读实时视图）。
子代理禁用：后台子代理任务线程（ctx.execution_scope() 判定）调用一律拒绝，
要求把未决问题写进其最终结果，由主代理决定是否委派。

命令 /asksub：status | on | off。
"""
from __future__ import annotations

from bawcode_sdk import PluginContext

import re
import time
from typing import Optional

# 子代理说明上限字符数；说明为自由文本，压平单行防伪造成段/标题结构
BRIEF_LIMIT = 2000

# 任务卡片问题文本上限（面板按宽度裁剪，浏览器端可见全文）
QUESTION_LIMIT = 4000

_TOOL_NAME = "ask_user_as_subagent"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def bias_guidance(bias: int) -> str:
    """委派倾向 → 模型引导文案（四档，语义对齐参考实现）"""
    shared = (
        "ask_user_as_subagent 对应一个子代理：调用即交给它一个任务，等待其完成，"
        "答复作为本次调用的结果返回。它掌握本环境不具备的知识、权限与材料，"
        "一次只处理一个任务；等待是委派的常态。"
    )
    if bias < 25:
        return (
            f"{shared}委派倾向 {bias}/100（低）：自行完成工作，仅当确实缺少该子代理掌握的"
            "信息或材料而被卡住时才委派；不作为捷径，也不用于并行赶工。"
        )
    if bias < 60:
        return (
            f"{shared}委派倾向 {bias}/100（均衡）：自行完成工作，把需要该子代理所掌握内容"
            "的部分交给它——本环境之外的知识或权限、只有它能做的判断、或它必须先提供的材料。"
        )
    if bias < 85:
        return (
            f"{shared}委派倾向 {bias}/100（高）：给出回答前，先检查是否有部分工作交给该子代理"
            "更合适，用 ask_user_as_subagent 交出去，而不是自己勉强完成。当该步骤依赖它掌握的"
            "材料、权限或判断时，委派就是正确做法。"
        )
    return " ".join((
        f"{shared}委派倾向 {bias}/100（最高）：子代理负责完成工作，你负责协调。",
        "在回答或调用任何工具前，先把这个请求拆成步骤，并把第一步交给子代理——包括你自己能完成的步骤。",
        "这会反转默认分工：连你自己能完成的任务也优先交给它，只保留没有本环境就无法发生的事——"
        "执行命令、读写文件、检查本仓库。其余一律先交给子代理。",
        "其中包括琐碎小事：算术（问它 17×23 等于多少）、你将要写入的文件内容、要发出的消息或"
        "文档草稿、命名、取值、记忆中的事实、以及下一步做什么的决定。",
        "两条硬规则：",
        "规则一——先要后写：当请求最终要落到文件时，先通过委派取得确切内容，再原样写入，"
        "不要自己编造内容。",
        "规则二——一次一调用：调用一次 ask_user_as_subagent，等结果、用结果，下一步确实需要时"
        "再调用；不要把多个任务合并进一次调用，也不要在回复里自己完成本应委派的任务。",
        "唯一例外是问候、纯确认、或凭眼前信息就能回答的问题——这些无需委派。",
    ))


def subagent_brief(description: str) -> Optional[str]:
    """子代理说明 → 注入文本；空说明返回 None（不占预算），超长截断"""
    brief = _one_line(description)[:BRIEF_LIMIT]
    if not brief:
        return None
    return (
        f"该子代理的职责说明：{brief}。把它当作其职责与专长：它描述了该子代理擅长什么，"
        "请优先把匹配的工作交给它，并据此措辞每个委派任务。"
    )


def _fmt_elapsed(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    if total >= 60:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total}s"


def setup(ctx: PluginContext) -> None:
    ctx.log.info("ask-user-subagent 装载，配置=%s", ctx.settings)

    def _bias() -> int:
        # 0 是合法最低档：先判 None/空串再转 int，不能走 `or 50`（0 会被吞掉）
        raw = ctx.settings.get("delegation_bias")
        if raw is None or raw == "":
            raw = 50
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            raw = 50
        return max(0, min(raw, 100))

    def _timeout() -> int:
        try:
            raw = int(ctx.settings.get("timeout_seconds") or 0)
        except (TypeError, ValueError):
            raw = 0
        return max(0, min(raw, 86400))

    def _enabled() -> bool:
        return bool(ctx.settings.get("enabled"))

    # ---- 委派引导注入（写入时焊入用户消息；关闭时不注入）----
    def _supplement(payload) -> Optional[str]:
        if not _enabled():
            return None
        parts = [bias_guidance(_bias())]
        brief = subagent_brief(str(ctx.settings.get("subagent_description") or ""))
        if brief:
            parts.append(brief)
        return "\n".join(parts)

    ctx.register_user_message_supplement(_supplement)

    # ---- 委派工具：阻塞等卡片作答，答案即结果 ----
    @ctx.register_tool(
        name=_TOOL_NAME,
        description=(
            "Delegate a piece of the current work to a subagent and wait for its result. "
            "The call returns once that subagent has finished the task, and its answer becomes "
            "this call's result. This subagent works independently of your tools: it holds "
            "knowledge, access, and material that this environment does not, so hand it the part "
            "it is better placed to answer and keep the part that must happen here. It works one "
            "task at a time (it is a human-like answerer, not a shortcut for tool access1), "
            "so keep each call to a single task and state exactly what you need back."
        ),
        usage="ask_user_as_subagent <task> [context]",
        schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task to hand to the subagent, phrased so it stands alone: what to produce and what the result should contain.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional background that helps the subagent answer: constraints, what was already tried, or what the answer will be used for.",
                },
            },
            "required": ["task"],
        },
        safe=True,  # 纯交互无副作用：委派前先确认"能委派"纯属打扰
    )
    def _tool(task: str, context: str = "") -> str:
        if ctx.execution_scope() == "subagent":
            return (
                "ask_user_as_subagent 不可由子代理调用；请把未决问题写进你的最终结果，"
                "由主代理决定是否委派。"
            )
        if not _enabled():
            return "该子代理已停用（/asksub on 可开启），请自行完成该部分工作。"
        task_text = str(task or "").strip()
        if not task_text:
            return "参数错误: task 不能为空"
        background = str(context or "").strip()
        question = "【委派任务】" + task_text + (f"\n背景：{background}" if background else "")
        if len(question) > QUESTION_LIMIT:
            question = question[:QUESTION_LIMIT].rstrip() + "…（已截断）"
        timeout = _timeout()
        if not ctx.ui.available:
            return "当前环境无交互通道，无法委派任务；请自行完成该部分工作。"
        started = time.monotonic()
        try:
            # ctx.ui.ask 内部走请求槽（持 DIALOG_LOCK 串行、自动接回合取消探针）；
            # 槽位被顶掉/取消命中/通道未就绪统一返回 None
            result = ctx.ui.ask(question, timeout=timeout, multiline=True)
        except Exception as exc:
            ctx.log.error("委派卡片交互异常: %r", exc)
            return f"委派任务失败: {exc}"
        elapsed = _fmt_elapsed(time.monotonic() - started)
        status = str((result or {}).get("status") or "") if isinstance(result, dict) else ""
        ctx.log.info("委派任务收场 status=%s 用时=%s", status or "?", elapsed)
        if status == "answer":
            answer = str((result or {}).get("answer") or "").strip()
            if not answer:
                return "子代理已接手但未返回有效内容，请继续自行处理该部分工作。"
            return f"子代理已完成委派的任务（用时 {elapsed}），结果如下：\n\n{answer}"
        if status == "timeout":
            limit = f"{timeout} 秒内未返回结果" if timeout > 0 else "长时间未返回结果"
            return (
                f"等待超时（{limit}）。任务尚未完成，不要将其标记为完成；"
                "可稍后重新委派或自行处理。"
            )
        if status == "cancelled" or not isinstance(result, dict):
            # 面板 Esc 中断、回合取消哨兵、交互通道异常统一按取消收场
            return "委派调用被中断（回合取消），请基于现有信息继续。"
        return "任务被退回（子代理未接手）。请自行处理该部分工作，或稍后重新委派。"

    # ---- /asksub：status | on | off ----
    def _asksub_cmd(cctx, args: str) -> str:
        sub = (args or "").strip().lower()
        if sub == "on":
            ctx.set_setting("enabled", True)
            return "用户子代理已开启"
        if sub == "off":
            ctx.set_setting("enabled", False)
            return "用户子代理已关闭（立即生效，已发起的委派不受影响）"
        bias = _bias()
        if bias < 25:
            band = "低（几乎全部自己做）"
        elif bias < 60:
            band = "均衡"
        elif bias < 85:
            band = "高"
        else:
            band = "最高（能交就交）"
        brief = _one_line(str(ctx.settings.get("subagent_description") or ""))
        state = "开启" if _enabled() else "关闭"
        return (
            f"用户子代理：{state} · 委派倾向 {bias}/100（{band}）· 超时 {_timeout() or '不限'}\n"
            f"子代理说明：{brief[:80] or '（默认表述）'}"
        )

    ctx.register_command(
        "/asksub",
        hint="用户子代理：状态/开关",
        usage="/asksub [status|on|off]",
        handler=_asksub_cmd,
    )
