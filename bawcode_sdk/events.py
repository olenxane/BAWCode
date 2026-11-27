"""Hook 事件目录（单一事实源，宿主 core/hooks.py 与本 SDK 共用）。

kind: call=变换链 collect=观察链。插件可直接引用常量避免拼写错误：

    from bawcode_sdk.events import AFTER_TURN
    ctx.register_hook(AFTER_TURN, fn)

目录外的事件名允许注册（插件间自定义互通事件，见开发指南 §14），但 register_hook 会 warn 一次提示可能拼错——确为自定义事件时可忽略。
"""
import difflib

# 事件名常量
PROMPT_REFINE = "prompt_refine"
PLAN_GENERATE = "plan_generate"
PLAN_CONFIRM = "plan_confirm"
STEP_GENERATE = "step_generate"
STEP_UPDATE = "step_update"
MEMORY_WRITE = "memory_write"
MEMORY_READ = "memory_read"
RAG_ADD = "rag_add"
RAG_QUERY = "rag_query"
COMPUTER_USE = "computer_use"
LLM_REQUEST = "llm_request"
TOOL_CONFIRM = "tool_confirm"
TOOL_CONFIRM_CLOSED = "tool_confirm_closed"
UI_REQUEST = "ui_request"
SESSION_START = "session_start"
BEFORE_TURN = "before_turn"
AFTER_TURN = "after_turn"
STREAM_DELTA = "stream_delta"
TURN_STATUS = "turn_status"
BEFORE_TOOL = "before_tool"
AFTER_TOOL = "after_tool"
USER_MESSAGE_SUPPLEMENT = "user_message_supplement"
MESSAGE_ADDED = "message_added"
UI_TREE_NODES = "ui_tree_nodes"
UI_BOTTOM_ROWS = "ui_bottom_rows"

EVENTS = {
    # ---- 用户参与型能力（external_apis 同名映射；call 变换链）----
    "prompt_refine": {"kind": "call", "desc": "改写用户的编码任务提示"},
    "plan_generate": {"kind": "call", "desc": "外部生成/接收计划内容"},
    "plan_confirm": {"kind": "call", "desc": "计划确认回调"},
    "step_generate": {"kind": "call", "desc": "外部生成执行步骤（返回 {steps: []} 生效）"},
    "step_update": {"kind": "call", "desc": "步骤状态变更通知"},
    "memory_write": {"kind": "call", "desc": "长期记忆写入旁路"},
    "memory_read": {"kind": "call", "desc": "长期记忆读取旁路"},
    "rag_add": {"kind": "call", "desc": "RAG 文档加入旁路"},
    "rag_query": {"kind": "call", "desc": "RAG 查询旁路"},
    "computer_use": {"kind": "call", "desc": "computer-use 外部执行接口"},
    "llm_request": {"kind": "call", "desc": "整段接管/转发 LLM 请求（返回 chat 响应结构生效）"},
    "tool_confirm": {"kind": "call", "desc": "工具确认代答，首个有效应答生效；None 交回 UI 面板"},
    "tool_confirm_closed": {"kind": "collect", "desc": "工具确认已应答，按 request_id 关闭其他通道请求"},
    "ui_request": {"kind": "call", "desc": "交互弹窗代答 confirm/choose/line/ask（返回与键盘输入同语义的应答值生效；None 交回 UI 面板）"},
    "session_start": {"kind": "collect", "desc": "会话初始化完成（collect）"},
    "before_turn": {"kind": "call", "desc": "回合开始，可改写 user_text（返回 {user_text: ...} 生效）"},
    "after_turn": {"kind": "collect", "desc": "回合结束通知（collect）"},
    "stream_delta": {"kind": "collect", "desc": "流式输出增量通知 {kind, piece}（collect，高频率）"},
    "turn_status": {"kind": "collect", "desc": "回合状态/阶段变化通知 {status} 或 {phase}（collect）"},
    "before_tool": {"kind": "call", "desc": "工具执行前，可改写 args 或拒绝执行"},
    "after_tool": {"kind": "collect", "desc": "工具执行后通知（collect）"},
    "user_message_supplement": {"kind": "collect", "desc": "用户消息补充文本（collect，返回 str 生效）：写入时焊进该条 user 消息，出站以 <system> 块拼在正文开头"},
    "message_added": {"kind": "collect", "desc": "会话消息新增通知 {role, content, type, ...extra}（collect，content 可能为多模态数组；处理函数内禁止再写会话消息，否则递归）"},
    "ui_tree_nodes": {"kind": "collect", "desc": "每帧提供插件主窗口会话树根节点（collect）"},
    "ui_bottom_rows": {"kind": "collect", "desc": "每帧提供主窗口统计状态栏下方的 ANSI 文本行（collect）"},
}


def is_known(event: str) -> bool:
    return event in EVENTS


def suggest(event: str) -> str:
    """拼写建议：返回最相近的目录内事件名，无相近返回空串"""
    matches = difflib.get_close_matches(str(event), list(EVENTS), n=1, cutoff=0.6)
    return matches[0] if matches else ""
