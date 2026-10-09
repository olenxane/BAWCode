#此文件为ui的渲染交互脚本：无边框分区、主题、确认流程、token状态
import difflib
import functools
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.text import Text

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from core import commands as cmdsys
from core import hooks as hooks_mod
from core import keymap as keymap_mod
from core import mcp as mcp_mod
from core import memory as memory_mod
from core import pasteboard
from core import policy
from core import snapshot as snapshot_mod
from core import subagent as subagent_mod
from core import toolstore as toolstore_mod
from core import tokens as tokenmod
from core.tools import tool_failure_hint
from core.keymap import Action, Context, Keymap
from core.textbuf import TextBuffer
from wcwidth import wcwidth
from core.config import (
    THEME_DIR,
    THINKING_OPTIONS,
    make_model_name,
)
from core.log import get_logger, init as init_logging
from core.settings import SettingsPanelMixin
# 渲染器已独立到 core/render.py：此处完整回导其模块级名字，保持 ui 对外命名空间
# 与拆分前一致（core/settings.py 等仍按 ui._clip / ui._pad … 访问）。
from core.render import (
    DEFAULT_COLORS,
    Layout,
    RenderMixin,
    TIPS,
    TreeNode,
    _ANSI_RE,
    _DIFF_ADD_BG,
    _DIFF_DEL_BG,
    _HINT_ROTATE_SECONDS,
    _MOUSE_Y_OFFSET,
    _PHASE_POOLS,
    _SPINNER_FRAMES,
    _STATUS_ICON,
    _STREAM_THINKING_LINES,
    _THINKING_PHRASES,
    _TOOL_CMD,
    _TOOL_CMD_OUTPUT_LINES,
    _TOOL_EDIT,
    _TOOL_WRITE,
    _TREE_FOLD_KINDS,
    _TREE_INLINE_CAP,
    _bg,
    _char_width,
    _clip,
    _clip_keep_ansi,
    _command_head,
    _copy_to_clipboard,
    _diff_wrapped,
    _display_width,
    _enable_windows_ansi,
    _fg,
    _git_branch,
    _hex_rgb,
    _hl_rows_wrapped,
    _lexer_for,
    _logo_frames,
    _mix,
    _oneline,
    _pad,
    _scrollbar_geometry,
    _term_size,
    _wrap,
    _wrap_keep_ansi,
    _wrap_line,
    load_theme,
    split_at_cells,
)

log = get_logger("ui")

from core.llm import CANCELLED as CANCEL_RESULT

_CONSOLE = Console(soft_wrap=True, force_terminal=True) if HAS_RICH else None
_app: Optional["TuiApp"] = None


class _LlmRetryGate:
    """LLM 错误手动重试闸：agent 线程在 wait 上阻塞，主线程按键/插件置位放行"""

    __slots__ = ("error", "retry", "give_up")

    def __init__(self, error: str):
        self.error = error
        self.retry = threading.Event()
        self.give_up = threading.Event()


class TuiApp(SettingsPanelMixin, RenderMixin):
    def __init__(self):
        _enable_windows_ansi()
        self.theme = load_theme("dark")
        self.colors = self.theme["colors"]
        self.RESET = "\033[0m"
        self.C = {k: _fg(v) for k, v in self.colors.items()}
        self.C_HL = _bg(self.colors["highlight_bg"]) + _fg(self.colors["ink"])
        self.project_name = "BAWCode"
        self.git_branch = _git_branch()
        self.model_name = "deepseek-deepseek-chat"
        self.mode = policy.MODE_AUTO
        self.api_key_set = False
        self.messages: List[dict] = []
        self.plan: Dict[str, Any] = {}
        self.steps: List[dict] = []
        self.task = ""
        self.status = "就绪"
        self.tool_count = 0
        self.focus = "input"
        # 粘贴占位机制（core/pasteboard.py）：判定与登记在 PasteBuffer；
        # pending_images 由 _dispatch_key 提交时入队、_agent_turn 逐回合出队编码
        self.paste = pasteboard.PasteBuffer()
        self.pending_images: List[str] = []
        self.config = None  # bind_config 时填充；粘贴 gate 提前读
        # _paste_async：后台图片落盘单槽（后台线程单写、帧尾 _tick_paste_async 取回清空）
        self._paste_async = None
        self._paste_async_busy = False
        # LLM 错误手动重试闸（agent 线程等待、主线程按键/插件放行；None=非等待态）
        self._llm_retry_gate: Optional[_LlmRetryGate] = None
        self.scroll = 0
        self.input_scroll = 0
        self.cand_scroll = 0
        self.tree_cursor = 0
        # 会话树是否贴底：新消息/内容变长时自动下滑到最新行
        self._tree_follow_tail = True
        self._tree_last_msg_n = 0
        self.expanded: Set[str] = set()
        self.collapse_done = True
        self.buffer = TextBuffer()
        self.input_history: List[str] = []
        self.hist_index = -1
        self.candidates: List[dict] = []
        self.candidate_index = 0
        self._cand_text: Optional[str] = None
        self._cand_cursor: int = -1
        self._cand_config = None
        self._init_settings_state()
        self.pending_tool: Optional[dict] = None
        self.token_meter = tokenmod.TokenMeter()
        # 流式树尾直播状态（agent 线程单写者、帧循环只读，与 app.status 同模式）：
        # streaming_msg 为 {"role","content","thinking","type":"streaming"} dict，不进 session.messages
        self.streaming_msg = None
        # 子代理直播槽：{"id","role","phase","text","tools":[]}，agent 线程经
        # core/subagent.py 单写者写入；与 streaming_msg 分离，避免语义混叠
        self.subagent_stream = None
        self.phase_hint = ""  # 树底阶段提示：思考中/工具调用中/空（完成）
        # 回合进行中标志（main._AgentRunner 镜像写入，跨线程只读）：树回退等操作据此拒绝
        self.busy = False
        # 回合计时与本轮 token 基线：agent 线程回合起始写入，帧循环只读
        self.turn_start_time = 0.0
        self.turn_user_text = ""
        # 流式 token 本地分词的 2 秒节流缓存：(时间戳, token 数)
        self._turn_tok_cache = None
        # Esc 打断回滚后的输入回填单槽：agent 线程写、帧尾取回
        self.restore_input: Optional[str] = None
        # 主循环注册的 Esc 打断回调，返回是否已触发；None=未注册
        self.interrupt_turn_handler = None
        # 回合中插话队列（普通 Enter 提交）：主线程入队、回合循环取走并入当前回合
        self.inject_queue: List[str] = []
        self.inject_lock = threading.Lock()
        # Ctrl+Q 排队提交标记：read_line 返回后由主循环取回（排队=当前回合结束后新回合）
        self._submit_queued = False
        # 本次提交是否含图片：含图片的提交不插话，排队为新回合（图片按回合起始消费）
        self._submit_has_images = False
        # 树回退（Ctrl+Z）的取消暂存：{"cut","messages","session_id"}；末条 _rollback_note 标记配合校验
        self._conv_stash: Optional[dict] = None
        self.logo_enabled = True
        self.sessions_mode = False
        self.sessions_items: List[dict] = []
        self.sessions_index = 0
        self._sessions_scroll = 0
        self._sessions_current_id = ""
        self._sessions_confirm_delete = False
        self.sessions_notice = ""
        # 工具确认面板：取代输入框位置，↑↓ 选择 · Enter 确认 · 可输入拒绝原因
        self.confirm_mode = False
        # 树底阶段提示的隐藏信号：confirm/ask/choose/line 弹窗执行期间为 True（_serve_ui_request 维护）
        self._dialog_active = False
        self.confirm_tool: dict = {}
        self.confirm_index = 0
        self.confirm_reason_mode = False
        self._confirm_buf = TextBuffer()
        # 询问用户面板（ask_user 工具）：同位置渲染，选项+末位自由输入+可选倒计时
        self.ask_mode = False
        self.ask_spec: dict = {}
        self.ask_index = 0
        self.ask_input_mode = False
        self._ask_buf = TextBuffer()
        self.ask_deadline: Optional[float] = None
        self._ask_last_sec = -1
        # UI 请求桥：agent 线程的交互弹窗移交主线程执行（单读者保证）
        self._ui_req: Optional[dict] = None
        self._input_prompt = "> "
        # 渲染接管：rich Live（enter() 时启动）；_frame_time 为固定帧周期（TMP 同款 20fps）
        self._live: Optional["Live"] = None  # 保留字段兼容 leave()；渲染走行级 diff 直写
        self._last_lines: Optional[list] = None
        self._last_w = 0
        self._frame_time = 1.0 / 20
        self._row_meta: Dict[str, int] = {}
        self.keymap = Keymap()
        self.keymap.compile()
        self.keys = {
            "send": "ctrl+enter",
            "newline": "shift+enter",
            "switch_focus": "tab",
            "switch_mode": "shift+tab",
            "complete": "tab",
            "scroll_up": "pageup",
            "scroll_down": "pagedown",
            "expand": ["right", "l"],
            "collapse": ["left", "h"],
            "rollback": "ctrl+z",
            "scroll_v0": 1.0,
            "scroll_hold_ms": 150,
            "scroll_max_step": 20,
            "tip_interval": 5,
        }
        self._entered = False
        self._flat_nodes: List[Tuple[TreeNode, int, bool, str]] = []
        self._tree_nodes: Optional[List[TreeNode]] = None
        self._tree_sig: Optional[tuple] = None
        self._tree_rows_key: Optional[tuple] = None
        self._tree_rows_cache: Optional[List[str]] = None
        # 节点号 → 渲染行区间 (start, count)：树光标（节点空间）与滚动（行空间）的唯一换算表
        self._node_spans: List[Tuple[int, int]] = []
        self._input_layout = Layout("", width=40)
        self._input_layout_width = 0
        self._cursor_row_key: Optional[tuple] = None
        self._cursor_row_val: Optional[int] = None
        self._cursor_seg_w: int = 0
        self._cursor_col: int = 0
        self._cursor_rel_row: int = 0
        self._hold_dir = ""
        self._hold_start = 0.0
        self._hold_last = 0.0
        self._scroll_drag_grab: Optional[int] = None
        # 自绘选区（文档坐标：_tree_rows 缓存行号 + 单元格）；锚点/焦点 None=无选区
        self._sel_anchor: Optional[Tuple[int, int]] = None
        self._sel_focus: Optional[Tuple[int, int]] = None
        self._sel_dragging = False
        self._sel_sig: Optional[tuple] = None
        self._sel_edge = 0  # 拖拽选区越界方向：-1 顶缘 / +1 底缘 / 0 在树体内

    @property
    def cursor(self) -> int:
        return self.buffer.cursor

    @cursor.setter
    def cursor(self, value: int) -> None:
        self.buffer.set_cursor(value)


    def bind_config(self, config) -> None:
        log.debug(
            "绑定配置: model=%s mode=%s theme=%s",
            getattr(config, "model_name", "-"),
            getattr(config, "mode", "-"),
            getattr(config, "theme", "-"),
        )
        self.config = config
        self.model_name = config.model_name
        self.api_key_set = bool(config.api_key)
        ui_cfg = (config.data or {}).get("ui") or {}
        self.mode = ui_cfg.get("mode", policy.MODE_AUTO)
        self.load_theme(ui_cfg.get("theme", "dark"))
        # 快捷键（shift+tab 始终可用；switch_mode 为额外可配置键）
        for k in (
            "send",
            "newline",
            "switch_focus",
            "switch_mode",
            "complete",
            "scroll_up",
            "scroll_down",
            "expand",
            "collapse",
            "rollback",
        ):
            if k in ui_cfg and ui_cfg[k] not in (None, ""):
                val = ui_cfg[k]
                if isinstance(val, (list, tuple)):
                    # 多绑定保留整组（expand/collapse 等）
                    self.keys[k] = [str(x).lower() for x in val if x not in (None, "")]
                else:
                    self.keys[k] = str(val).lower()
        if not self.keys.get("switch_mode"):
            self.keys["switch_mode"] = "shift+tab"
        if not self.keys.get("newline"):
            self.keys["newline"] = "shift+enter"
        self.keymap = Keymap()
        self._sync_keymap()
        self.keys["scroll_v0"] = float(ui_cfg.get("scroll_v0", 1.0))
        self.keys["scroll_hold_ms"] = int(ui_cfg.get("scroll_hold_ms", 150))
        self.keys["scroll_max_step"] = int(ui_cfg.get("scroll_max_step", 20))
        self.keys["tip_interval"] = float(ui_cfg.get("tip_interval", 5))
        self.token_meter.set_model(config.model_name, getattr(config, "context_window", 0) or 0)
        self.token_meter.context_window = getattr(config, "context_window", 0) or 0
        self.logo_enabled = bool(ui_cfg.get("logo", True))

    def enter(self) -> None:
        _enable_windows_ansi()
        if self._entered:
            return
        log.info("进入 TUI 界面")
        # bracketed-paste 默认关闭（见 docstring）；终端模式直接写 stdout
        _bp = os.environ.get("BAW_BRACKETED_PASTE", "")
        _paste_on = _bp == "1" or (_bp == "" and sys.platform != "win32")
        # 鼠标：1000h 普通 + 1002h 按钮事件 + 1006h SGR 编码。
        # WT/ConPTY 实测：仅 1000h+1006h 时终端不转发滚轮加 1002h 后以 MOUSE_EVENT 记录到达。
        # 开启鼠标捕获后，终端内文本选择需 Shift+拖拽。关闭用 BAW_MOUSE=0
        _mouse_on = os.environ.get("BAW_MOUSE", "1") != "0"
        _mouse_seq = "\033[?1000h\033[?1002h\033[?1006h" if _mouse_on else ""
        sys.stdout.write("\033[2J\033[H\033[?25l" + ("\033[?2004h" if _paste_on else "") + _mouse_seq)
        sys.stdout.flush()
        self._entered = True

    def leave(self) -> None:
        """退出 TUI：停 Live、关粘贴协议、清屏恢复。

        主屏模式退出时清屏（?2J?H）——本会话的绘制内容覆盖了 shell 提示符，
        清屏比留残屏干净；用户按回车即出新提示符。
        """
        if not self._entered:
            return
        log.info("离开 TUI 界面")
        try:
            if self._live is not None:
                self._live.stop()
                self._live = None
        except Exception:
            pass
        sys.stdout.write("\033[?25h\033[0m\033[2J\033[H\033[?2004l\033[?1000l\033[?1002l\033[?1006l")
        sys.stdout.flush()
        self._entered = False

    def refresh_from_session(self, session, task: Optional[str] = None, render: bool = True) -> None:
        self.messages = list(getattr(session, "messages", []) or [])
        self.plan = dict(getattr(session, "plan", {}) or {})
        self.steps = list(getattr(session, "steps", []) or [])
        self._tree_sig = None  # 内容已同步，强制重建树缓存
        msg_n = len(self.messages)
        if msg_n > self._tree_last_msg_n and not self.confirm_mode:
            # 会话变长：恢复贴底，避免停在顶部看不到新回复；
            # 确认面板等待期间不拉底——用户可能正在翻看历史
            self._tree_follow_tail = True
        self._tree_last_msg_n = msg_n
        if task is not None:
            self.task = task
        elif self.plan.get("title"):
            self.task = self.plan.get("title")
        if render:
            # agent 线程传 render=False：渲染统一由主线程帧循环完成（线程安全）
            self.render()


    def cycle_mode(self) -> str:
        idx = policy.MODES.index(self.mode) if self.mode in policy.MODES else 0
        self.mode = policy.MODES[(idx + 1) % len(policy.MODES)]
        return self.mode


    # ----- 输入 / 补全 -----
    def _word_start(self) -> int:
        return self.buffer.word_start()

    def _buf_text(self) -> str:
        return self.buffer.to_text()

    def _buf_set(self, text: str, cursor: Optional[int] = None) -> None:
        self.buffer.set_text(text, cursor)

    def _buf_insert(self, s: str) -> None:
        self.buffer.insert(s)

    def _buf_backspace(self) -> None:
        self.buffer.backspace()

    def _buf_delete(self) -> None:
        self.buffer.delete()

    def _buf_clear(self) -> None:
        self.buffer.clear()
        self.cursor = 0

    def _buf_len(self) -> int:
        return self.buffer.length

    def _refresh_candidates(self, config=None) -> None:
        text = self._buf_text()
        # 输入循环每帧调用；缓冲与光标未变时跳过补全计算
        if text == self._cand_text and self.cursor == self._cand_cursor and config is self._cand_config:
            return
        self._cand_text = text
        self._cand_cursor = self.cursor
        self._cand_config = config
        # 传整段光标前文本：cmdsys.complete 依据空格区分命令名/参数补全，
        # 只传当前词会让 /model deep 之类的参数补全永远无法触发
        frag = text[: self.cursor]
        if frag.startswith("/"):
            self.candidates = cmdsys.complete(frag, config=config)
            if self.candidate_index >= len(self.candidates):
                self.candidate_index = 0
        else:
            self.candidates = []
            self.candidate_index = 0

    def _apply_completion(self) -> bool:
        if not self.candidates:
            return False
        name = self.candidates[self.candidate_index].get("name") or ""
        if not name:
            return False
        text = self._buf_text()
        start = self._word_start()
        end = self.cursor
        while end < len(text) and not text[end].isspace():
            end += 1
        # 替换当前词（命令名或参数片段），补空格便于继续输入
        new_text = text[:start] + name + " " + text[end:]
        self._buf_set(new_text, start + len(name) + 1)
        self.candidates = []
        self.candidate_index = 0
        return True

    # ----- LLM 错误手动重试闸 -----

    def wait_llm_retry(self, error_text: str, cancelled) -> bool:
        """API 错误等待态（TurnIO 桥，agent 线程调用）：状态行显示错误与按键提示。
        返回 True=用户按 Ctrl+Y 重试；Esc 放弃、提交消息自动放弃、回合取消或
        llm.retry_wait_seconds 超时（0=无限等待）亦放弃。等待期间其余交互不受影响。"""
        gate = _LlmRetryGate(_oneline(str(error_text or ""), 200))
        self._llm_retry_gate = gate
        self.status = f"API 错误 · {gate.error} · Ctrl+Y 重试 / Esc 放弃"
        timeout = 0.0
        cfg = getattr(self, "config", None)
        if cfg is not None:
            try:
                timeout = float((cfg.data or {}).get("llm", {}).get("retry_wait_seconds", 0) or 0)
            except (TypeError, ValueError):
                timeout = 0.0
        deadline = time.monotonic() + timeout if timeout > 0 else None
        try:
            while True:
                if gate.retry.wait(0.1):
                    return True
                if gate.give_up.is_set() or cancelled():
                    return False
                if deadline is not None and time.monotonic() >= deadline:
                    log.warn("LLM 重试等待超时（%.0fs），放弃", timeout)
                    return False
        finally:
            if self._llm_retry_gate is gate:
                self._llm_retry_gate = None

    def request_llm_retry(self) -> bool:
        """请求重试当前失败的 LLM 请求（插件接口同源）：仅错误等待态有效，返回是否触发"""
        gate = self._llm_retry_gate
        if gate is None:
            return False
        gate.retry.set()
        return True

    def _scroll_step(self, direction: str) -> int:
        """长按加速：v(t)=v0*(1+t^2)"""
        now = time.time()
        v0 = float(self.keys.get("scroll_v0") or 1.0)
        hold_ms = float(self.keys.get("scroll_hold_ms") or 150)
        max_step = int(self.keys.get("scroll_max_step") or 20)
        if direction != self._hold_dir or (now - self._hold_last) * 1000 > hold_ms * 1.6:
            self._hold_dir = direction
            self._hold_start = now
        t = max(0.0, now - self._hold_start)
        # 长按阈值后再加速
        if t * 1000 < hold_ms:
            step = 1
        else:
            step = int(round(v0 * (1.0 + t * t)))
        self._hold_last = now
        return max(1, min(max_step, step))

    def read_line(self, prompt: Optional[str] = None, config=None) -> str:
        """界面内输入框：Action 驱动主循环（Gap Buffer + Keymap O(1)）。"""
        self.settings_mode = False
        self.focus = "input"
        self._buf_clear()
        self.buffer = TextBuffer()
        self.cursor = 0
        self.candidates = []
        self.input_scroll = 0
        self._input_prompt = prompt or "> "
        self.render()
        frame_deadline = time.time()
        pending_events: List[Tuple[str, Any]] = []
        while True:
            self._refresh_candidates(config=config)
            if not pending_events:
                pending_events = _read_events(0.0)
            # 每帧分发本批全部事件：滚轮/按键高速到达（>20/秒）时若每帧只
            # 消费一个，积压线性增长，表现为滚动/删字明显滞后于手。
            while pending_events:
                key = pending_events.pop(0)
                kind, value = key
                if kind == "interrupt":
                    return "/exit"
                result = self._dispatch_key(kind, value, config)
                if result is not None:
                    return result

            # 帧尾：UI 请求桥 + 后台图片落盘结果 + 选区边缘自动滚动 + 无条件渲染 + 补足帧周期
            self._serve_ui_request()
            if self._dialog_active and _pump_dead():
                # 读键泵崩溃：对话框永远等不到键，按空行收场防回合永久挂起
                return ""
            self._tick_paste_async()
            self._tick_restore_input()
            self._selection_tick()
            self.render()
            frame_deadline += self._frame_time
            sleep_left = frame_deadline - time.time()
            if sleep_left > 0:
                time.sleep(sleep_left)
            else:
                frame_deadline = time.time()

    def _ui_context(self) -> Context:
        if self.settings_mode:
            return Context.SETTINGS
        if self.focus == "tree":
            return Context.TREE
        return Context.INPUT

    # ----- 粘贴占位机制（判定/登记在 core/pasteboard.py，此处只做缓冲插入与状态行） -----

    def _handle_paste_text(self, text: str) -> None:
        """paste 事件载荷（终端字符流/括号粘贴）：按文本/路径规则登记后插入缓冲"""
        inserted = self.paste.accept_text(text)
        if inserted:
            self._buf_insert(inserted)

    def _handle_clipboard_paste(self) -> None:
        """应用侧读剪贴板（Ctrl+V 热键）：文件列表/文本同步插入；纯图片后台落盘
        （PowerShell 0.5~1s，不阻帧循环），完成结果经 _paste_async 单槽由帧尾取回；
        非视觉模型纯图片分支不落盘不插入（状态行提示）"""
        supports = bool(getattr(self.config, "supports_vision", True)) if self.config else True
        if self._paste_async_busy:
            self.status = "剪贴板图片正在处理…"
            return
        data = pasteboard.read_clipboard()
        if (data or {}).get("kind") == "image" and supports:
            self._paste_async_busy = True
            self.status = "正在读取剪贴板图片…"

            def _save_bg():
                # 单槽协议 (True, 路径|None)：成败都回填并复位 busy，防帧循环永久卡"正在处理"
                try:
                    result = pasteboard.save_clipboard_image()
                except Exception as e:
                    log.warn("剪贴板图片落盘失败: %r", e)
                    result = None
                finally:
                    self._paste_async = (True, result)
                    self._paste_async_busy = False

            threading.Thread(target=_save_bg, daemon=True, name="bawcode-paste-img").start()
            self.render()
            return
        texts, status = self.paste.accept_clipboard(supports_vision=supports, data=data)
        for t in texts:
            if t:
                self._buf_insert(t)
        if status:
            self.status = status
        self.render()

    def _tick_paste_async(self) -> None:
        """帧尾检查后台图片落盘结果（单槽跨线程：后台线程单写、帧循环取回清空）"""
        if self._paste_async is None:
            return
        _, path = self._paste_async
        self._paste_async, self._paste_async_busy = None, False
        if path is not None:
            self._buf_insert(self.paste.accept_image(path))
            self.status = "剪贴板图片已插入（发送时随消息编码）"
        else:
            self.status = "剪贴板图片保存失败（详见日志）"
        self.render()

    def _tick_restore_input(self) -> None:
        """帧尾取回 Esc 打断回填的单槽；输入框非空则不覆盖用户新输入"""
        text = self.restore_input
        if text is None:
            return
        self.restore_input = None
        if self._buf_text().strip():
            self.status = "已回滚到回合前（输入框已有内容，未回填原消息）"
            return
        self._buf_set(text, len(text))
        self.status = "已回滚到回合前，原消息已回到输入框"
        self.render()

    def take_injections(self) -> List[str]:
        """取走并清空回合中插话队列（线程安全）：回合循环与主循环收尾共用"""
        with self.inject_lock:
            if not self.inject_queue:
                return []
            pending = list(self.inject_queue)
            self.inject_queue.clear()
            return pending

    def consume_queued_submit(self) -> bool:
        """取回并复位 Ctrl+Q 排队提交标记（主循环用）"""
        queued = self._submit_queued
        self._submit_queued = False
        return queued

    def consume_submit_images(self) -> bool:
        """取回并复位「本次提交含图片」标记（主循环用）：含图片的提交不插话，排队为新回合"""
        has = self._submit_has_images
        self._submit_has_images = False
        return has

    def _submit_line(self) -> str:
        """提交输入框内容：展开粘贴占位、记录历史、清空缓冲，返回提交行"""
        line = self._buf_text()
        images = None
        if line.startswith("/"):
            # 命令行不展开粘贴占位（占位机制只服务对话消息）
            self.paste.reset()
        else:
            line, images = self.paste.expand(line)
            if images:
                self.pending_images.append(images)
        self._submit_has_images = bool(images)
        if line.strip():
            self.input_history.append(line)
            if self._llm_retry_gate is not None:
                # 等待重试时提交新消息：自动放弃当前错误
                self._llm_retry_gate.give_up.set()
        self.hist_index = len(self.input_history)
        self._buf_clear()
        self.candidates = []
        return line

    def _dispatch_key(self, kind: str, value: Any, config=None) -> Optional[str]:
        """返回 None 表示继续循环；返回 str 为提交行。"""
        # 鼠标事件不走键表（也不必重编译键位表：拖拽中 move 事件高频）
        if kind in ("mouse_down", "mouse_move", "mouse_up"):
            self._on_mouse(kind, value)
            return None
        self._sync_keymap()
        ctx = self._ui_context()
        # 模式切换始终优先
        if kind == "mode_switch" or self._is_mode_switch(kind, value):
            mode = self.cycle_mode()
            # 单一事实源同步：策略（config.mode，读 data.ui.mode）与持久化值跟随
            # UI 显示——否则页脚显示完全访问而策略仍按旧模式弹确认、重启后丢失
            config = getattr(self, "config", None)
            if config is not None:
                try:
                    config.mode = mode
                    config.save()
                except Exception as e:
                    log.warn("模式切换写配置失败: %s", e)
            self.status = f"{policy.MODE_LABELS.get(mode, mode)}"
            self.render()
            return None

        # Ctrl+V 热键（终端透传场景）：应用侧读剪贴板（文本/文件列表/纯图片）
        if kind == "hotkey" and str(value) == "ctrl+v" and self.focus == "input":
            self._handle_clipboard_paste()
            return None

        # Ctrl+Q：排队提交（当前回合结束后作为新回合发送）；空闲时等同普通提交
        # 弹窗（含嵌套 line 输入）期间不拦截，避免污染主循环的排队标记
        if kind == "hotkey" and str(value) == "ctrl+q" and self.focus == "input" and not self._dialog_active:
            if self._buf_text().strip():
                self._submit_queued = True
                return self._submit_line()
            return None

        # Esc：回合进行中=打断并回滚到回合前（含文件与输入回填）；空闲保持原清空语义
        # 弹窗（含嵌套 line 输入）期间 _dialog_active 为真，Esc 语义交给弹窗自身
        if kind == "escape" and self.busy and not self._dialog_active:
            handler = self.interrupt_turn_handler
            if callable(handler) and handler():
                self.status = "正在打断回合…"
                self.render()
                return None

        # LLM 错误重试闸激活时：Ctrl+Y 放行重试、Esc 放弃；其余按键照常分发
        gate = self._llm_retry_gate
        if gate is not None:
            if kind == "hotkey" and str(value) == "ctrl+y":
                gate.retry.set()
                return None
            if kind == "escape":
                gate.give_up.set()
                return None

        act = self.keymap.resolve(ctx, kind, value)
        # Tab：输入框 complete 优先，再 focus；树上 focus
        if kind == "tab" or act in (Action.COMPLETE, Action.FOCUS_NEXT):
            if self.candidates and (kind == "tab" or act == Action.COMPLETE):
                self._apply_completion()
                self.render()
                return None
            if ctx == Context.TREE or act == Action.FOCUS_NEXT or kind == "tab":
                self._toggle_focus()
                self.render()
                return None

        if act in (Action.NEWLINE,):
            if self.focus == "input":
                self._buf_insert("\n")
            return None

        if act in (Action.SUBMIT, Action.SEND):
            if act == Action.SUBMIT and self.focus == "tree":
                self.toggle_fold(self.tree_cursor)
                self.render()
                return None
            # SEND 在树上也提交
            return self._submit_line()

        if act == Action.INSERT:
            s = value if isinstance(value, str) else ""
            if s and all(ord(ch) >= 32 for ch in s):
                if self.focus == "input":
                    self._buf_insert(s)
                elif s == " ":
                    self.toggle_fold(self.tree_cursor)
                    self.render()
            return None

        if act == Action.BACKSPACE:
            if self.focus == "input":
                self._buf_backspace()
            return None

        if act == Action.DELETE:
            if self.focus == "input":
                self._buf_delete()
            return None

        if act == Action.CLEAR:
            self._buf_clear()
            self.paste.reset()
            self.render()
            return None

        if act == Action.ESCAPE:
            if self.candidates:
                self.candidates = []
                self.render()
            else:
                self._buf_clear()
                self.paste.reset()
            return None

        if act == Action.PASTE:
            if self.focus == "input" and value:
                self._handle_paste_text(str(value))
            return None

        if act == Action.EXPAND:
            self._tree_expand()
            self.render()
            return None
        if act == Action.COLLAPSE:
            self._tree_collapse_or_toggle()
            self.render()
            return None
        if act == Action.ROLLBACK:
            self._tree_rollback()
            self.render()
            return None

        if act == Action.CARET_UP:
            self._on_up()
            return None
        if act == Action.CARET_DOWN:
            self._on_down()
            return None
        if act == Action.CARET_LEFT:
            if self.focus == "input":
                self.cursor = max(0, self.cursor - 1)
            return None
        if act == Action.CARET_RIGHT:
            if self.focus == "input":
                self.cursor = min(self._buf_len(), self.cursor + 1)
            return None
        if act == Action.CARET_HOME:
            if self.focus == "input":
                self.buffer.move_home()
            return None
        if act == Action.CARET_END:
            if self.focus == "input":
                self.buffer.move_end()
            return None

        if act == Action.SCROLL_UP:
            self._scroll_tree_by(-self._scroll_delta(kind, value))
            return None
        if act == Action.SCROLL_DOWN:
            self._scroll_tree_by(self._scroll_delta(kind, value))
            return None

        # 方向键在树/历史的语义（keymap 已给 CARET_*，按焦点细分）
        dirn = _key_direction(kind, value)
        if dirn == "up":
            self._on_up()
        elif dirn == "down":
            self._on_down()
        return None

    def _scroll_delta(self, kind: str, value: Any) -> int:
        """鼠标滚轮固定 3 行；键盘滚动沿用长按加速。"""
        tok = keymap_mod.event_token(kind, value)
        if kind == "mouse_wheel" or tok in ("wheel_up", "wheel_down"):
            return 3
        direction = "pu" if tok == "pageup" else "pd"
        return self._scroll_step(direction)

    def _set_scroll(self, new_scroll: int) -> None:
        """设置会话区滚动并同步树光标进视口（滚轮/翻页/拖拽共用）。

        到底自动恢复贴底跟随；行数与视口取 _row_meta 快照（无则回退树行缓存）。
        不在此处渲染：read_line 帧尾统一渲染。"""
        meta = self._row_meta
        tree_h = int(meta.get("tree_h") or 4)
        total = int(meta.get("scroll_total") or 0)
        if total <= 0:
            total = len(self._tree_rows_cache or [])
        max_scroll = max(0, total - tree_h)
        self.scroll = max(0, min(int(new_scroll), max_scroll))
        self._tree_follow_tail = self.scroll >= max_scroll
        # 滚动后把光标同步回可见节点区间（行→节点换算，防止节点号被当行号错位）
        if self._node_spans and tree_h > 0:
            first_node = self._node_at_row(self.scroll)
            last_node = self._node_at_row(self.scroll + tree_h - 1)
            if self.tree_cursor < first_node:
                self.tree_cursor = first_node
            elif self.tree_cursor > last_node:
                self.tree_cursor = last_node

    def _scroll_tree_by(self, delta: int) -> None:
        """按步长调整会话区显示范围（滚轮/翻页/长按加速）。"""
        self._set_scroll(self.scroll + delta)

    def _on_mouse(self, kind: str, value: Any) -> None:
        """鼠标交互：滚动条拖拽/翻页 + 会话区自绘选区（左键拖拽选、右键复制）。

        坐标为控制台 0-based 单元格，几何换算基于 _row_meta 每帧快照；
        mouse_down 的 value 自 keyinput 起携带按键前缀（"left:x,y"/"right:x,y"）。
        选区锚定文档行（_tree_rows 缓存行号），滚动时选区随内容移动；
        内容变化（签名改变）后选区自动失效。"""
        meta = self._row_meta
        text = str(value or "")
        btn, coord = "", text
        if kind == "mouse_down" and ":" in text:
            btn, coord = text.split(":", 1)
        try:
            x_s, y_s = coord.split(",", 1)
            x, y = int(x_s), int(y_s) + _MOUSE_Y_OFFSET
        except (ValueError, IndexError):
            return
        if self.settings_mode or self.sessions_mode:
            return
        tree_h = int(meta.get("tree_h") or 4)
        body_top = int(meta.get("body_top") or 1)
        scrollbar_on = bool(meta.get("scrollbar_on"))
        scrollbar_x = int(meta.get("scrollbar_x") or -1)
        if kind == "mouse_down":
            if btn == "right":
                self._copy_selection()
                return
            if scrollbar_on and x == scrollbar_x:
                y_rel = y - body_top
                if not (0 <= y_rel < tree_h):
                    return
                thumb_top = int(meta.get("thumb_top") or 0)
                thumb_h = int(meta.get("thumb_h") or 1)
                if thumb_top <= y_rel < thumb_top + thumb_h:
                    self._scroll_drag_grab = y_rel - thumb_top
                elif y_rel < thumb_top:
                    self._set_scroll(self.scroll - tree_h)  # 轨道上段：上翻页
                else:
                    self._set_scroll(self.scroll + tree_h)  # 轨道下段：下翻页
                return
            # 会话区左键按下：开启/重开选区（文档坐标锚定）
            y_rel = y - body_top
            if not (0 <= y_rel < tree_h):
                return
            total = len(self._tree_rows_cache or [])
            row = max(0, min(self.scroll + y_rel, max(0, total - 1)))
            self._sel_anchor = (row, max(0, x))
            self._sel_focus = (row, max(0, x))
            self._sel_dragging = True
            self._sel_edge = 0
            self._sel_sig = self._tree_signature()
        elif kind == "mouse_move":
            if self._scroll_drag_grab is not None:
                thumb_h = int(meta.get("thumb_h") or 1)
                total = int(meta.get("scroll_total") or 0)
                if total <= 0:
                    return
                y_rel = y - body_top
                usable = max(1, tree_h - thumb_h)
                new_thumb = max(0, min(y_rel - self._scroll_drag_grab, usable))
                self._set_scroll(round(new_thumb * max(1, total - tree_h) / usable))
            elif self._sel_dragging and self._sel_anchor is not None:
                y_rel = y - body_top
                last = max(0, len(self._tree_rows_cache or []) - 1)
                # 越界方向：上缘（表头行以上，鼠标出不了窗口故最深 -1）/
                # 下缘（提示·输入区视作"向下继续选"）；滚动交给帧循环 tick
                if y_rel < 0:
                    self._sel_edge = -1
                    row = self.scroll
                elif y_rel >= tree_h:
                    self._sel_edge = 1
                    row = min(self.scroll + tree_h - 1, last)
                else:
                    self._sel_edge = 0
                    row = max(0, min(self.scroll + y_rel, last))
                self._sel_focus = (row, max(0, x))
        elif kind == "mouse_up":
            if self._sel_dragging:
                # 原地单击（未拖出）：清除选区
                if self._sel_focus == self._sel_anchor:
                    self._sel_anchor = None
                    self._sel_focus = None
                self._sel_dragging = False
            self._sel_edge = 0
            self._scroll_drag_grab = None

    # ----- 自绘选区 -----


    def _toggle_focus(self) -> None:
        if self.focus == "input":
            self.focus = "tree"
            flat = self._flatten_tree()
            # 光标落最后一个节点；scroll 交给帧循环 clamp（贴底后尾节点必然可见）
            self.tree_cursor = max(0, len(flat) - 1)
            self._tree_follow_tail = True
        else:
            self.focus = "input"

    def _tree_expand(self) -> None:
        """树焦点下展开↔收起双向切换（裸 Enter/l/→ 同义）；输入焦点为右移光标"""
        if self.focus == "tree" and self._flat_nodes and self.tree_cursor < len(self._flat_nodes):
            self.toggle_fold(self.tree_cursor)
        elif self.focus == "input":
            self.cursor = min(self._buf_len(), self.cursor + 1)

    def _tree_collapse_or_toggle(self) -> None:
        if self.focus == "tree" and self._flat_nodes and self.tree_cursor < len(self._flat_nodes):
            self.toggle_fold(self.tree_cursor)
        elif self.focus == "input":
            self.cursor = max(0, self.cursor - 1)

    def _tree_rollback(self) -> None:
        """树焦点 Ctrl+Z：回退到选中消息节点（节点保留为其后最后一条消息，其后移除）；
        再按一次取消（恢复被移除的尾段）。工具调用块原子处理（assistant(tool_calls)+
        连续 tool 结果不拆分）。被移除回合存在文件快照时经确认框决定是否联动回滚文件
        （快照按回合起始消息序号定位，md5 门禁照旧保护事后修改）"""
        if self.focus != "tree":
            return
        if getattr(self, "busy", False):
            self.status = "回合进行中，无法回退"
            self.render()
            return
        session = memory_mod.get_session()
        if session is None or not session.messages:
            self.status = "无会话内容"
            self.render()
            return

        # 防漂移：app.messages 是副本，可能落后 session.messages——先刷新并按节点 id 重钉光标
        pinned_id = None
        if self._flat_nodes and self.tree_cursor < len(self._flat_nodes):
            pinned_id = self._flat_nodes[self.tree_cursor][0].id
        self.refresh_from_session(session, render=False)
        flat = self._flatten_tree()
        if pinned_id is not None:
            for i, entry in enumerate(flat):
                if entry[0].id == pinned_id:
                    self.tree_cursor = i
                    break
        if not flat:
            return
        self.tree_cursor = max(0, min(self.tree_cursor, len(flat) - 1))
        node = flat[self.tree_cursor][0]

        # 再按 Ctrl+Z = 取消回退：仅当截断后无新消息（末条是我们的回退标记）
        stash = self._conv_stash
        if stash is not None:
            note = session.messages[-1] if session.messages else None
            valid = (
                stash.get("session_id") == session.session_id
                and note is not None
                and note.get("_rollback_note")
                and len(session.messages) - 1 == stash.get("cut")
            )
            if valid:
                session.messages.pop()
                session.messages.extend(stash["messages"])
                self._conv_stash = None
                self.refresh_from_session(session)
                self._tree_follow_tail = True
                self.status = "已取消回退"
                self.render()
                return
            self._conv_stash = None  # 失效（已有新消息/切换会话），静默丢弃

        # 解析锚点消息绝对序号（msg:{i} / call:{i} 相对 messages[-200:] 窗口）
        if node.id.startswith("msg:") or node.id.startswith("call:"):
            tail = session.messages[-200:]
            offset = len(session.messages) - len(tail)
            try:
                idx = offset + int(node.id.split(":")[1])
            except (IndexError, ValueError):
                self.status = "节点锚点解析失败"
                self.render()
                return
        else:
            self.status = "该节点不支持作为回退锚点（请选中消息节点）"
            self.render()
            return
        if idx >= len(session.messages):
            self.status = "锚点已失效（会话已变化），请重新选择"
            self.render()
            return
        _start, end = memory_mod.conv_block_bounds(session.messages, idx)
        cut = end + 1
        if cut >= len(session.messages):
            self.status = "该节点后无内容可回退"
            self.render()
            return

        # 文件快照联动：被移除回合存在快照时询问（无快照直接回退，不弹框）。
        # 阈值 cut-1：锚点为用户消息（节点保留）时该回合的文件改动同样随移除；
        # 锚点更早时回合整体移除，也满足 msg_index >= cut-1
        affected: List[dict] = []
        try:
            affected = snapshot_mod.turns_from_msg_index(
                session.config, session.project_id, session.session_id, cut - 1
            )
        except Exception as exc:
            log.warn("快照联动查询失败: %r", exc)
        link_files = False
        if affected:
            choice = str(
                self.choose(
                    [("both", "对话+文件"), ("conv", "仅对话"), ("cancel", "取消")],
                    prompt=(
                        f"回退将移除其后 {len(session.messages) - cut} 条消息"
                        f"（{len(affected)} 个回合有文件改动）· 是否同时回滚文件？"
                    ),
                )
                or ""
            ).lower()
            if choice == "cancel":
                self.status = "已取消"
                self.render()
                return
            link_files = choice == "both"

        removed = session.messages[cut:]
        del session.messages[cut:]
        file_summary = ""
        if link_files and affected:
            restored_n = deleted_n = skipped_n = 0
            for turn in affected:  # seq 降序：后产生的先还原
                res = snapshot_mod.restore_turn(
                    session.config, session.project_id, session.session_id, turn["seq"]
                )
                if res:
                    restored_n += len(res["restored"])
                    deleted_n += len(res["deleted"])
                    skipped_n += len(res["skipped"])
            toolstore_mod.ledger_reset()  # 磁盘已变，强制模型重新 read
            file_summary = f" · 文件还原{restored_n} 删除{deleted_n} 跳过{skipped_n}"
        session.add_message(
            "system",
            f"[已回退到选中节点：移除其后 {len(removed)} 条消息{file_summary}；再按 Ctrl+Z 可取消（仅恢复对话）]",
            type="help",
            _rollback_note=True,
        )
        self._conv_stash = {"cut": cut, "messages": removed, "session_id": session.session_id}
        self.refresh_from_session(session)
        self._tree_follow_tail = True
        self.status = f"已回退（移除 {len(removed)} 条 · 再按 Ctrl+Z 取消）"
        self.render()

    def _on_up(self) -> None:
        if self.candidates:
            self.candidate_index = max(0, self.candidate_index - 1)
            self.render()
        elif self.focus == "tree":
            step = self._scroll_step("up")
            flat = self._flatten_tree()
            # 光标按节点移动；scroll 的可见性修正统一由帧循环 _clamp_tree_scroll 完成
            self.tree_cursor = max(0, min(len(flat) - 1, self.tree_cursor - step))
            self._tree_follow_tail = False
            self.render()
        elif self.input_history:
            self.hist_index = max(0, self.hist_index - 1)
            self._buf_set(self.input_history[self.hist_index], len(self.input_history[self.hist_index]))
            self.render()

    def _on_down(self) -> None:
        if self.candidates:
            self.candidate_index = min(len(self.candidates) - 1, self.candidate_index + 1)
            self.render()
        elif self.focus == "tree":
            step = self._scroll_step("down")
            flat = self._flatten_tree()
            last = max(0, len(flat) - 1)
            self.tree_cursor = min(last, self.tree_cursor + step)
            self._tree_follow_tail = self.tree_cursor >= last
            self.render()
        else:
            if self.hist_index < len(self.input_history) - 1:
                self.hist_index += 1
                line = self.input_history[self.hist_index]
                self._buf_set(line, len(line))
            else:
                self.hist_index = len(self.input_history)
                self._buf_clear()
            self.render()

    # ----- UI 请求桥：agent 线程经此把交互弹窗移交主线程执行 -----

    def request_ui(self, kind: str, payload: dict) -> dict:
        """agent 线程发起交互请求（confirm/choose/line）；结果经 wait_ui 取回。

        ui_request 变换链：插件/外部接口可代答——非 None 返回视为立即应答
        （wait_ui 直通返回，终端面板不再弹出）；全 None 走原终端面板路径。"""
        hooked = hooks_mod.call_hook("ui_request", {"kind": kind, **(payload or {})}, default=None)
        if hooked is not None:
            req = {
                "kind": kind,
                "payload": payload or {},
                "result": hooked,
                "event": threading.Event(),
                "served": True,  # 已由扩展点应答，_serve_ui_request 跳过
            }
            req["event"].set()
            self._ui_req = req
            return req
        req = {
            "kind": kind,
            "payload": payload or {},
            "result": None,
            "event": threading.Event(),
            "served": False,
        }
        self._ui_req = req
        return req

    def wait_ui(self, req: dict, cancelled=None, poll: float = 0.05):
        """agent 线程等待主线程完成交互；cancelled() 为真时回填取消哨兵"""
        while not req["event"].wait(poll):
            if cancelled is not None and cancelled():
                req["result"] = CANCEL_RESULT
                req["event"].set()
        return req["result"]

    def _serve_ui_request(self) -> None:
        """主线程（read_line 帧循环）执行待处理的交互请求；同刻最多一个"""
        req = self._ui_req
        if req is None or req.get("served"):
            return
        req["served"] = True
        # 弹窗（含其内部嵌套渲染）期间抑制树底阶段提示
        self._dialog_active = True
        try:
            kind = req["kind"]
            payload = req["payload"] or {}
            if kind == "confirm":
                result = self.show_confirm_form(
                    str(payload.get("name") or ""), payload.get("arguments") or {}
                )
            elif kind == "ask":
                result = self.show_ask_form(payload or {})
            elif kind == "choose":
                result = self.choose(
                    list(payload.get("options") or []), str(payload.get("prompt") or "")
                )
            elif kind == "line":
                result = self.read_line(payload.get("prompt"), config=payload.get("config"))
            else:
                result = None
        except Exception as exc:
            log.error("UI 请求执行失败: %r", exc)
            result = CANCEL_RESULT
        finally:
            self._dialog_active = False
        req["result"] = result
        req["event"].set()


    def show_ask_form(self, payload: dict) -> dict:
        """询问用户面板（阻塞）：↑↓/数字选择 · Enter 确认 · 末项进自由输入 · Esc 跳过。

        payload: {"question": str, "options": [{"title", "description"}], "timeout": 秒，0=禁用}
        返回 {"status": "answer"|"declined"|"timeout"|"cancelled", "answer"/"index"/"timeout"}。
        自动超时经键盘轮询 tick 检查 deadline，超时返回 timeout 让 agent 循环继续。"""
        question = str((payload or {}).get("question") or "")
        raw_opts = payload.get("options") or []
        options = [
            {"title": str(o.get("title") or ""), "description": str(o.get("description") or "")}
            for o in raw_opts
            if isinstance(o, dict) and str(o.get("title") or "").strip()
        ]
        try:
            timeout = max(0, int(payload.get("timeout") or 0))
        except (TypeError, ValueError):
            timeout = 0
        self.ask_mode = True
        self.ask_spec = {"question": question, "options": options}
        self.ask_index = 0
        self.ask_input_mode = False
        self._ask_buf = TextBuffer()
        self.ask_deadline = (time.monotonic() + timeout) if timeout > 0 else None
        self._ask_last_sec = -1
        try:
            try:
                _flush_input()
            except Exception:
                pass
            self.render()
            while True:
                ev = _read_key()
                kind, value = ev if ev is not None else ("tick", "")
                if kind == "tick":
                    # 倒计时：整秒变化才重渲染；到点自动跳过
                    if self.ask_deadline is not None:
                        now = time.monotonic()
                        if now >= self.ask_deadline:
                            return {"status": "timeout", "timeout": timeout}
                        sec = int(self.ask_deadline - now)
                        if sec != self._ask_last_sec:
                            self._ask_last_sec = sec
                            self.render()
                    elif _pump_dead():
                        # 无期限 ask 遇泵崩溃：无键可达，按取消收场防永久卡死
                        return {"status": "cancelled"}
                    continue
                if kind in ("mode_switch",):
                    continue
                if self.ask_input_mode:
                    # 自由输入态
                    if kind == "interrupt":
                        return {"status": "cancelled"}
                    if kind == "escape":
                        self.ask_input_mode = False
                        self._ask_buf.clear()
                        self.render()
                        continue
                    if kind in ("submit", "newline"):
                        return {"status": "answer", "answer": self._ask_buf.to_text().strip()}
                    if kind == "char":
                        if isinstance(value, str) and value and all(ord(c) >= 32 for c in value):
                            self._ask_buf.insert(value)
                            self.render()
                        continue
                    if kind == "backspace":
                        self._ask_buf.backspace()
                        self.render()
                        continue
                    if kind == "paste":
                        flat = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                        self._ask_buf.insert(flat)
                        self.render()
                        continue
                    continue
                # 选项模式
                n = len(options) + 1  # 末项=自由输入
                if kind == "interrupt":
                    return {"status": "cancelled"}
                if kind in ("escape",):
                    return {"status": "declined"}
                if kind == "up" or (kind == "hotkey" and str(value).endswith("up")):
                    self.ask_index = (self.ask_index - 1) % n
                    self.render()
                    continue
                if kind == "down" or (kind == "hotkey" and str(value).endswith("down")):
                    self.ask_index = (self.ask_index + 1) % n
                    self.render()
                    continue
                if kind == "mouse_wheel":
                    self._scroll_tree_by(3 if value == "down" else -3)
                    self.render()
                    continue
                if kind == "pageup" or (kind == "hotkey" and str(value) == "pageup"):
                    self._scroll_tree_by(-self._row_meta.get("tree_h", 10))
                    self.render()
                    continue
                if kind == "pagedown" or (kind == "hotkey" and str(value) == "pagedown"):
                    self._scroll_tree_by(self._row_meta.get("tree_h", 10))
                    self.render()
                    continue
                if kind == "submit":
                    if self.ask_index < len(options):
                        return {
                            "status": "answer",
                            "index": self.ask_index,
                            "answer": options[self.ask_index]["title"],
                        }
                    self.ask_input_mode = True
                    self._ask_buf = TextBuffer()
                    self.render()
                    continue
                # 数字快捷键：直接选中对应项（选项立即生效，末项进输入态）
                if kind == "char" and str(value).isdigit():
                    idx = int(value) - 1
                    if 0 <= idx < len(options):
                        return {"status": "answer", "index": idx, "answer": options[idx]["title"]}
                    if idx == len(options):
                        self.ask_index = len(options)
                        self.ask_input_mode = True
                        self._ask_buf = TextBuffer()
                        self.render()
                    continue
        finally:
            self.ask_mode = False
            self.ask_input_mode = False
            self.ask_deadline = None
            self.render()

    def show_confirm_form(self, tool_name: str, tool_args: Optional[dict] = None) -> dict:
        """工具确认面板（阻塞）：↑↓ 选择 · Enter 确认 · Esc 拒绝。

        选中「拒绝并输入原因」回车后进入行内输入状态，Enter 提交（空=默认理由）。
        返回 {"action": "allow_once"|"allow_always"|"reject", "reason": str}。"""
        options = ("allow_once", "allow_always", "reject", "reason")
        self.confirm_mode = True
        self.confirm_tool = {"name": tool_name, "arguments": tool_args or {}}
        self.confirm_index = 0
        self.confirm_reason_mode = False
        self._confirm_buf = TextBuffer()
        try:
            try:
                _flush_input()
            except Exception:
                pass
            self.render()
            while True:
                ev = _read_key()
                kind, value = ev if ev is not None else ("tick", "")
                if kind in ("tick", "mode_switch"):
                    if kind == "tick" and _pump_dead():
                        # 读键泵崩溃：无键可达，默认拒绝收场防弹窗永久卡死
                        return {"action": "reject", "reason": "输入线程已退出"}
                    continue
                if self.confirm_reason_mode:
                    # 拒绝原因输入态
                    if kind == "interrupt":
                        return {"action": "reject", "reason": ""}
                    if kind == "escape":
                        self.confirm_reason_mode = False
                        self._confirm_buf.clear()
                        self.render()
                        continue
                    if kind in ("submit", "newline"):
                        return {"action": "reject", "reason": self._confirm_buf.to_text().strip()}
                    if kind == "char":
                        if isinstance(value, str) and value and all(ord(c) >= 32 for c in value):
                            self._confirm_buf.insert(value)
                            self.render()
                        continue
                    if kind == "backspace":
                        self._confirm_buf.backspace()
                        self.render()
                        continue
                    if kind == "paste":
                        flat = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                        self._confirm_buf.insert(flat)
                        self.render()
                        continue
                    continue
                # 选项模式
                if kind in ("interrupt", "escape"):
                    return {"action": "reject", "reason": ""}
                if kind == "up" or (kind == "hotkey" and str(value).endswith("up")):
                    self.confirm_index = (self.confirm_index - 1) % len(options)
                    self.render()
                    continue
                if kind == "down" or (kind == "hotkey" and str(value).endswith("down")):
                    self.confirm_index = (self.confirm_index + 1) % len(options)
                    self.render()
                    continue
                if kind == "mouse_wheel":
                    # 滚轮滚动会话树（面板固定在输入框位置），不再用于切换选项
                    self._scroll_tree_by(3 if value == "down" else -3)
                    self.render()
                    continue
                if kind == "pageup" or (kind == "hotkey" and str(value) == "pageup"):
                    self._scroll_tree_by(-self._row_meta.get("tree_h", 10))
                    self.render()
                    continue
                if kind == "pagedown" or (kind == "hotkey" and str(value) == "pagedown"):
                    self._scroll_tree_by(self._row_meta.get("tree_h", 10))
                    self.render()
                    continue
                if kind == "submit":
                    action = options[self.confirm_index]
                    if action == "reason":
                        self.confirm_reason_mode = True
                        self._confirm_buf = TextBuffer()
                        self.render()
                        continue
                    return {"action": action, "reason": ""}
                # 数字快捷键 1/2/3
                if kind == "char" and value in ("1", "2", "3"):
                    idx = int(value) - 1
                    action = options[idx]
                    if action == "reason":
                        self.confirm_index = idx
                        self.confirm_reason_mode = True
                        self._confirm_buf = TextBuffer()
                        self.render()
                        continue
                    return {"action": action, "reason": ""}
        finally:
            self.confirm_mode = False
            self.confirm_reason_mode = False
            self.render()

    def choose(self, options: List[tuple], prompt: str = "") -> str:
        lines = []
        if prompt:
            lines.append(f"{prompt}:")
        for key, label in options:
            lines.append(f"  {key}) {label}")
        content = "\n".join(lines)
        self.messages.append({"role": "system", "content": content, "type": "help"})
        # 写入 session，避免随后 _sync 用 session 覆盖后选项消失
        try:
            sess = memory_mod.get_session()
            if sess is not None:
                sess.add_message("system", content, type="help")
        except Exception:
            pass
        self.render()
        return self.read_line().strip()

    def _sync_keymap(self) -> None:
        """由 self.keys 重编译倒排表（O(动作×绑定)，仅配置变更时）"""
        self.keymap.compile(
            {
                "send": self.keys.get("send"),
                "newline": self.keys.get("newline"),
                "switch_focus": self.keys.get("switch_focus"),
                "switch_mode": self.keys.get("switch_mode"),
                "complete": self.keys.get("complete"),
                "scroll_up": self.keys.get("scroll_up"),
                "scroll_down": self.keys.get("scroll_down"),
                "expand": self.keys.get("expand"),
                "collapse": self.keys.get("collapse"),
                "rollback": self.keys.get("rollback"),
            }
        )

    def _is_mode_switch(self, kind: str, value: Any) -> bool:
        """shift+tab 始终切换模式；额外尊重设置中的 switch_mode 映射"""
        if kind == "mode_switch":
            return True
        self._sync_keymap()
        act = self.keymap.resolve(Context.INPUT, kind, value)
        return act == Action.MODE_CYCLE

    # ----- 历史会话面板 -----


    def show_sessions_form(self, items: List[dict], current_id: str = "", directory=None) -> Optional[dict]:
        """历史会话面板（阻塞）：{"action":"switch","id","title"} / {"action":"new"}；Esc 返回 None"""
        from core import session_store as _store

        self.sessions_mode = True
        self.sessions_items = list(items)
        self.sessions_index = 0
        self._sessions_scroll = 0
        self._sessions_current_id = current_id
        self._sessions_confirm_delete = False
        self.sessions_notice = "↑↓ 选择 · Enter 切换 · d 删除 · Esc 取消"
        try:
            _flush_input()
        except Exception:
            pass
        self.render()
        while True:
            key = _read_key()
            if key is None:
                key = ("tick", "")
            kind, value = key
            if kind == "tick":
                continue
            if kind in ("interrupt", "escape"):
                self.sessions_mode = False
                return None
            if kind == "mode_switch":
                continue
            if kind in ("submit", "submit_ctrl"):
                if not self.sessions_items:
                    continue
                item = self.sessions_items[self.sessions_index]
                self.sessions_mode = False
                if item.get("action") == "new":
                    return {"action": "new"}
                return {"action": "switch", "id": item.get("id"), "title": item.get("title")}
            if kind == "delete" or (kind == "char" and str(value).lower() == "d"):
                item = self.sessions_items[self.sessions_index] if self.sessions_items else None
                if not item or item.get("action") == "new":
                    self.render()
                    continue
                if item.get("id") == self._sessions_current_id:
                    self._sessions_confirm_delete = False
                    self.sessions_notice = "当前会话不可删除"
                    self.render()
                    continue
                if not self._sessions_confirm_delete:
                    self._sessions_confirm_delete = True
                    self.sessions_notice = f"确认删除「{_oneline(str(item.get('title') or ''))}」？再按 d 确认，其它键取消"
                    self.render()
                    continue
                removed = False
                if directory is not None and item.get("id"):
                    removed = _store.delete_session_data(directory, item["id"])
                if removed:
                    self.sessions_items.pop(self.sessions_index)
                    if self.sessions_index >= len(self.sessions_items):
                        self.sessions_index = max(0, len(self.sessions_items) - 1)
                    self.sessions_notice = "会话已删除"
                else:
                    self.sessions_notice = "删除失败"
                self._sessions_confirm_delete = False
                self.render()
                continue
            if self._sessions_confirm_delete:
                # 任意其它键取消删除确认
                self._sessions_confirm_delete = False
                self.sessions_notice = "↑↓ 选择 · Enter 切换 · d 删除 · Esc 取消"
            dirn = _key_direction(kind, value)
            if dirn == "up":
                if self.sessions_index > 0:
                    self.sessions_index -= 1
            elif dirn == "down":
                if self.sessions_index < len(self.sessions_items) - 1:
                    self.sessions_index += 1
            self.render()


# ----- 输入栈：core.keyinput（prompt_toolkit 后端，旧实现在 _recycle/） -----

def _flush_input():
    from core import keyinput

    keyinput.flush_input()


def _key_direction(kind: str, value: str) -> Optional[str]:
    """归一化方向键事件：kind=up… 或 hotkey=ctrl+up 均映射为 up/down/left/right。"""
    from core import keyinput

    return keyinput.direction_of(kind, value)


def _read_key():
    """输入统一走 core.keyinput"""
    from core import keyinput

    return keyinput.read_key_event()


def _pump_dead():
    """读键泵是否已崩溃（keyinput 死亡哨兵）"""
    from core import keyinput

    return keyinput.pump_dead()


def _read_events(timeout: float = 0.0):
    """批处理读取：一次抽干输入队列返回事件列表（空列表=本轮无键）。

    timeout=0 非阻塞（固定帧循环供拍，帧尾 sleep 控制节拍）。"""
    from core import keyinput

    return keyinput.read_events(timeout)


def get_app() -> TuiApp:
    global _app
    if _app is None:
        _app = TuiApp()
    return _app


def settings(config) -> dict:
    return get_app().show_settings_form(config)
