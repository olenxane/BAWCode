"""TuiApp 混入的共享状态契约。

RenderMixin / SettingsPanelMixin 分文件实现同一批 UI 状态，方法体通过 self
访问由另一个混入或 TuiApp 赋值的成员。此处集中声明类型，运行时由 TuiApp 赋值。
"""

from typing import Any, Dict, List, Optional, Set, Tuple


class UiState:
    # 主题与颜色
    RESET: str
    C_HL: str
    C: Dict[str, str]
    theme: Dict[str, Any]
    colors: Dict[str, Any]

    # 顶部信息与全局状态
    project_name: str
    model_name: str
    git_branch: str
    mode: str
    focus: str
    keys: Any
    config: Any
    logo_enabled: bool

    # 会话内容
    messages: List[dict]
    plan: Dict[str, Any]
    steps: List[dict]
    task: Any
    phase_hint: str
    busy: bool
    collapse_done: bool
    expanded: Set[str]
    streaming_msg: Optional[dict]
    subagent_stream: Any
    token_meter: Any

    # 输入框与候选
    buffer: Any
    cursor: Any
    candidates: List[dict]
    candidate_index: int
    _input_layout: Any
    _input_prompt: str

    # 对话框面板
    confirm_mode: bool
    confirm_index: int
    confirm_tool: Any
    confirm_reason_mode: bool
    ask_mode: bool
    ask_index: int
    ask_spec: Any
    ask_deadline: Optional[float]
    ask_input_mode: bool
    _ask_buf: Any
    _confirm_buf: Any
    _dialog_active: bool

    # 会话列表 / 设置面板
    settings_mode: bool
    sessions_mode: bool
    sessions_items: List[Any]
    sessions_index: int
    sessions_notice: str
    _sessions_current_id: str

    # 会话树与渲染
    _flat_nodes: List[Any]
    _row_meta: Dict[str, Any]
    _sel_dragging: bool
    _sel_sig: Any
    _sel_anchor: Any
    _sel_focus: Any
    _sel_edge: int
    _tree_archive_messages: List[Any]
    _tree_archive_signature: Tuple[Any, ...]
    _tree_follow_tail: bool
    _tree_generation: int
    _tree_last_msg_n: int
    _tree_rows_cache: Optional[List[str]]
    _tree_rows_key: Any
    _node_spans: List[Tuple[int, int]]
    _turn_tok_cache: Optional[Tuple[float, int]]
    _last_lines: Optional[List[str]]
    _entered: bool
    _inline: Any
    render_mode: str
