#此文件为ui的渲染交互脚本：无边框分区、主题、确认框、token状态
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from rich.console import Console

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from core import commands as cmdsys
from core import policy
from core import tokens as tokenmod
from core.config import (
    MODALITY_OPTIONS,
    TASK_ROLES,
    THEME_DIR,
    THINKING_OPTIONS,
    make_model_name,
)
from core.log import get_logger

log = get_logger("ui")

_CONSOLE = Console(soft_wrap=True, force_terminal=True) if HAS_RICH else None
_app: Optional["TuiApp"] = None
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_COLLAPSE_THRESHOLD = 160
_STATUS_ICON = {"pending": "○", "running": "◐", "done": "●", "failed": "✗"}

DEFAULT_COLORS = {
    "title": (87, 199, 255),
    "accent": (61, 220, 255),
    "ok": (61, 255, 160),
    "warn": (255, 209, 102),
    "err": (255, 107, 122),
    "tool": (199, 146, 234),
    "dim": (127, 146, 173),
    "ink": (214, 226, 240),
    "line": (42, 59, 85),
    "highlight_bg": (31, 78, 121),
}

TIPS = [
    "界面输入框：Enter 提交 · Tab 补全/切焦点",
    "Shift+Tab 或 /mode 切换访问模式",
    "/model 切换模型 · /settings 打开设置",
    "设置：↑↓选项 · ←→切换 · 文本项 Enter 后行输入编辑",
    "提供商页 Enter 保存并应用激活提供商",
    "中文输入时绘制合并刷新，候选上屏后自动更新",
    "/theme 列出并载入主题",
]


def _enable_windows_ansi() -> None:
    """Windows：仅开启输出 VT（0x0004）

    不开启输入 VT（0x0020）。与 terminal-music-player 一致：
    msvcrt 对方向键走 0xE0/0x00 + 扫描码；Shift+Tab 由扫描码 15 识别。
    若终端仍投递 CSI，keyinput 已兼容解析且半包不会误判为 Esc。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

        out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(out, ctypes.byref(mode)):
            kernel32.SetConsoleMode(out, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


def _term_size() -> Tuple[int, int]:
    import os

    cols = os.environ.get("COLUMNS") or os.environ.get("BAW_COLS")
    rows = os.environ.get("LINES") or os.environ.get("BAW_LINES")
    if cols and rows:
        return max(70, int(cols)), max(22, int(rows))
    size = shutil.get_terminal_size(fallback=(100, 30))
    return max(70, size.columns), max(22, size.lines)


def _char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1


def _display_width(text: str) -> int:
    return sum(_char_width(ch) for ch in _ANSI_RE.sub("", text or ""))


def _hex_rgb(value: str) -> Optional[Tuple[int, int, int]]:
    text = (value or "").strip()
    if not text.startswith("#") or len(text) != 7:
        return None
    try:
        return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)
    except ValueError:
        return None


def _fg(rgb: Tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"\033[38;2;{r};{g};{b}m"


def _bg(rgb: Tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"\033[48;2;{r};{g};{b}m"


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    out, used = [], 0
    for ch in _ANSI_RE.sub("", text or ""):
        w = _char_width(ch)
        if used + w > width:
            out.append("…")
            break
        out.append(ch)
        used += w
    return "".join(out)


def _clip_keep_ansi(text: str, width: int) -> str:
    if width <= 0:
        return ""
    src = (text or "").replace("\r", "").replace("\n", " ")
    out, used, i = [], 0, 0
    while i < len(src):
        if src[i] == "\x1b":
            match = _ANSI_RE.match(src, i)
            if match:
                out.append(match.group(0))
                i = match.end()
                continue
        w = _char_width(src[i])
        if used + w > width:
            if used + 1 <= width:
                out.append("…")
            break
        out.append(src[i])
        used += w
        i += 1
    out.append("\033[0m")
    return "".join(out)


def _pad(text: str, width: int) -> str:
    visible = _display_width(text)
    return text if visible >= width else text + " " * (width - visible)


def _wrap(text: str, width: int) -> List[str]:
    if width <= 0:
        return [""]
    lines: List[str] = []
    for para in (text or "").splitlines() or [""]:
        if not para:
            lines.append("")
            continue
        cur, used = "", 0
        for ch in para:
            w = _char_width(ch)
            if used + w > width:
                lines.append(cur)
                cur, used = ch, w
            else:
                cur += ch
                used += w
        if cur:
            lines.append(cur)
    return lines or [""]


def _fold(text: str, limit: int = _COLLAPSE_THRESHOLD) -> str:
    plain = _ANSI_RE.sub("", str(text or ""))
    return plain if len(plain) <= limit else plain[:limit] + f" …(+{len(plain) - limit})"


def _oneline(text: str, limit: int = 80) -> str:
    plain = _ANSI_RE.sub("", str(text or "")).replace("\n", " ")
    return plain if len(plain) <= limit else plain[:limit] + "…"


def _git_branch() -> str:
    try:
        head = Path(__file__).resolve().parents[1] / ".git" / "HEAD"
        if head.exists():
            ref = head.read_text(encoding="utf-8", errors="replace").strip()
            if ref.startswith("ref:"):
                return ref.split("/")[-1] or "main"
            return ref[:7] if ref else "main"
    except Exception:
        pass
    return "main"


def load_theme(name: str) -> Dict[str, Any]:
    """载入 data/theme/<name>.json；无效则忽略并回落默认"""
    result = {"name": name, "colors": dict(DEFAULT_COLORS), "tips_rotate_seconds": 5.0}
    path = THEME_DIR / f"{name}.json"
    if not path.exists():
        log.debug("主题文件不存在，使用默认配色: %s", name)
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warn("主题文件载入失败，使用默认配色 %s: %s", name, e)
        return result
    if not isinstance(data, dict):
        log.warn("主题文件格式异常（非对象），使用默认配色: %s", name)
        return result
    colors = data.get("colors")
    if not isinstance(colors, dict):
        log.warn("主题缺少 colors 段，使用默认配色: %s", name)
        return result
    merged = dict(DEFAULT_COLORS)
    for key, value in colors.items():
        if key not in DEFAULT_COLORS:
            continue
        if isinstance(value, str):
            rgb = _hex_rgb(value)
            if rgb:
                merged[key] = rgb
    result["colors"] = merged
    result["name"] = data.get("name") or name
    try:
        result["tips_rotate_seconds"] = float(data.get("tips_rotate_seconds") or 5)
    except (TypeError, ValueError):
        result["tips_rotate_seconds"] = 5.0
    return result


def _logo_frames() -> List[List[str]]:
    """绿色渐变 BAW 字符图案，按行色相递进"""
    art = [
        r"██████╗  █████╗ ██╗    ██╗",
        r"██╔══██╗██╔══██╗██║    ██║",
        r"██████╔╝███████║██║ █╗ ██║",
        r"██╔══██╗██╔══██║██║███╗██║",
        r"██████╔╝██║  ██║╚███╔███╔╝",
        r"╚═════╝ ╚═╝  ╚═╝ ╚══╝╚══╝ ",
    ]
    greens = [(20, 80, 40), (30, 120, 60), (40, 160, 80), (60, 200, 100), (100, 230, 140), (160, 255, 180)]
    frames = []
    for shift in range(6):
        lines = []
        for i, row in enumerate(art):
            rgb = greens[(i + shift) % len(greens)]
            lines.append(_fg(rgb) + "  " + row + "\033[0m")
        frames.append(lines)
    return frames


def show_startup_logo(seconds: float = 1.5, project_name: str = "BAWCode") -> None:
    """程序真正启动时在主屏幕显示 Logo（不进入 TUI 备用屏，不占用对话区）"""
    import os

    if os.environ.get("BAW_NO_LOGO") == "1":
        return
    _enable_windows_ansi()
    frames = _logo_frames()
    steps = max(1, int(seconds / 0.25))
    dim = _fg(DEFAULT_COLORS["dim"])
    title_c = _fg(DEFAULT_COLORS["title"])
    try:
        for i in range(steps):
            art = frames[i % len(frames)]
            w, h = _term_size()
            lines = [""] * h
            top = max(2, h // 2 - len(art) // 2 - 1)
            lines[0] = title_c + f" {project_name} " + "\033[0m"
            for j, row in enumerate(art):
                if 0 <= top + j < h:
                    indent = max(0, (w - _display_width(row)) // 2)
                    lines[top + j] = " " * indent + row
            tip = "启动中…"
            if top + len(art) + 1 < h:
                lines[top + len(art) + 1] = " " * max(0, (w - len(tip)) // 2) + dim + tip + "\033[0m"
            parts = [f"\033[{r + 1};1H\033[2K{_clip_keep_ansi(lines[r], w)}" for r in range(h)]
            sys.stdout.write("".join(parts) + "\033[J")
            sys.stdout.flush()
            time.sleep(0.25)
        # 清空主屏，准备进入 TUI
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    except Exception:
        pass


class TreeNode:
    __slots__ = ("id", "label", "kind", "children", "default_expanded", "summary", "detail")

    def __init__(self, id, label, kind="text", children=None, default_expanded=True, summary="", detail=""):
        self.id = id
        self.label = label
        self.kind = kind
        self.children = children or []
        self.default_expanded = default_expanded
        self.summary = summary
        self.detail = detail


class TuiApp:
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
        self.scroll = 0
        self.input_scroll = 0
        self.cand_scroll = 0
        self.tree_cursor = 0
        self.expanded: Set[str] = set()
        self.collapse_done = True
        self.buffer: List[str] = []
        self.cursor = 0
        self.input_history: List[str] = []
        self.hist_index = -1
        self.max_display_lines = 3
        self.candidates: List[dict] = []
        self.candidate_index = 0
        self._cand_text: Optional[str] = None
        self._cand_cursor: int = -1
        self._cand_config = None
        self.settings_mode = False
        self.settings_fields: List[dict] = []
        self.settings_index = 0
        self.settings_scratch: Dict[str, Any] = {}
        self.pending_tool: Optional[dict] = None
        self.confirm_index = 0
        self.confirm_reject_edit = False
        self.reject_buffer: List[str] = []
        self.reject_cursor = 0
        self.token_meter = tokenmod.TokenMeter()
        self.logo_enabled = True
        self.settings_tab = 0
        self.settings_tabs = ["提供商", "模型", "系统", "快捷键"]
        self.settings_provider_id = ""
        self.settings_model_name = ""
        self._settings_scroll = 0
        self.settings_notice = ""
        self.settings_model_work: Dict[str, List[dict]] = {}
        # 设置页模型列表刷新防抖（秒）
        self.settings_debounce_sec = 0.35
        self._settings_debounce_at = 0.0
        self._settings_debounce_pending = False
        # 设置页文本绘制防抖：控制全屏刷新频率，减轻 IME 提交时的卡顿
        self.settings_paint_sec = 0.06
        self._settings_paint_at = 0.0
        self._settings_paint_pending = False
        self._settings_baseline: Dict[str, Any] = {}
        self._settings_esc_stage = 0
        self._settings_text_dirty = False
        self._input_prompt = "> "
        self._paint_at = 0.0
        self._paint_pending = False
        self._paint_partial = True
        self._last_paint = 0.0
        self._last_frame_lines: List[str] = []
        self._row_meta: Dict[str, int] = {}
        self.keys = {
            "send": "ctrl+enter",
            "newline": "enter",
            "switch_focus": "tab",
            "switch_mode": "shift+tab",
            "complete": "tab",
            "scroll_up": "pageup",
            "scroll_down": "pagedown",
            "expand": "right",
            "collapse": "left",
            "scroll_v0": 1.0,
            "scroll_hold_ms": 150,
            "scroll_max_step": 20,
            "tip_interval": 5,
        }
        self._entered = False
        self._flat_nodes: List[Tuple[TreeNode, int, bool]] = []
        self._tree_nodes: Optional[List[TreeNode]] = None
        self._tree_sig: Optional[tuple] = None
        self._tree_rows_key: Optional[tuple] = None
        self._tree_rows_cache: Optional[List[str]] = None
        self._input_wrap_key: Optional[tuple] = None
        self._input_wrap_cache: Optional[List[str]] = None
        self._cursor_row_key: Optional[tuple] = None
        self._cursor_row_val: Optional[int] = None
        self._cursor_seg_w: int = 0
        self._cursor_col: int = 0
        self._cursor_rel_row: int = 0
        self._cursor_screen_row: Optional[int] = None
        self._cursor_screen_col: Optional[int] = None
        self._hold_dir = ""
        self._hold_start = 0.0
        self._hold_last = 0.0
        self._logo_done = False

    # ----- 颜色 -----
    def c(self, name: str) -> str:
        return self.C.get(name, "")

    def _rule(self, w: int) -> str:
        return self.c("line") + "─" * max(1, w) + self.RESET

    def load_theme(self, name: str) -> None:
        self.theme = load_theme(name)
        self.colors = self.theme["colors"]
        self.C = {k: _fg(v) for k, v in self.colors.items()}
        self.C_HL = _bg(self.colors["highlight_bg"]) + _fg(self.colors["ink"])
        log.debug("主题已应用: %s", self.theme.get("name") or name)
        try:
            self.keys["tip_interval"] = self.theme.get("tips_rotate_seconds", 5)
        except Exception:
            pass

    def bind_config(self, config) -> None:
        log.debug(
            "绑定配置: model=%s mode=%s theme=%s",
            getattr(config, "model_name", "-"),
            getattr(config, "mode", "-"),
            getattr(config, "theme", "-"),
        )
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
        ):
            if k in ui_cfg and ui_cfg[k] not in (None, ""):
                val = ui_cfg[k]
                if isinstance(val, (list, tuple)):
                    val = val[0] if val else self.keys.get(k)
                self.keys[k] = str(val).lower()
        if not self.keys.get("switch_mode"):
            self.keys["switch_mode"] = "shift+tab"
        self.keys["scroll_v0"] = float(ui_cfg.get("scroll_v0", 1.0))
        self.keys["scroll_hold_ms"] = int(ui_cfg.get("scroll_hold_ms", 150))
        self.keys["scroll_max_step"] = int(ui_cfg.get("scroll_max_step", 20))
        self.keys["tip_interval"] = float(ui_cfg.get("tip_interval", 5))
        self.token_meter.set_model(config.model_name, getattr(config, "context_window", 0) or 0)
        self.token_meter.context_window = getattr(config, "context_window", 0) or 0
        self.logo_enabled = bool(ui_cfg.get("logo", True))
        try:
            self.settings_debounce_sec = max(0.05, float(ui_cfg.get("settings_debounce_ms", 350)) / 1000.0)
        except (TypeError, ValueError):
            self.settings_debounce_sec = 0.35

    def enter(self) -> None:
        """进入 TUI：终端模式初始化。

        - 主屏模式（默认，对齐 music player 的 Live(screen=False)）：
          不切换备用屏，直接清屏绘制。备用屏（?1049h）下 Windows Terminal
          的 TSF IME 组合层与行级更新失同步（曾导致删除残影），且主屏与
          TMP 行为完全一致；代价是退出后 shell 提示符被覆盖（回车重现）。
          设 BAWCODE_ALT=1 可回退备用屏旧模式。
        - ?2004h 开 bracketed-paste：终端粘贴包 ESC[200~/201~ 标记，
          keyinput 由此合成整体 paste 事件（conhost 不支持则走启发式）。
        - ?25l 隐藏物理光标（绘制后由 _write_cursor_pos 钉位到输入处）。
        """
        _enable_windows_ansi()
        if self._entered:
            return
        log.info("进入 TUI 界面")
        # ?2004h 开 bracketed-paste：终端粘贴包 ESC[200~/201~ 标记，
        # keyinput 由此合成整体 paste 事件（conhost 不支持则走启发式）
        if os.environ.get("BAWCODE_ALT") == "1":
            sys.stdout.write("\033[?1049h\033[?25l\033[2J\033[H\033[?2004h")
        else:
            # 主屏模式（对齐 music player 的 Live(screen=False)）：
            # Windows Terminal 的 TSF IME（搜狗等）在备用屏(?1049h)下
            # 组合层与行级更新失同步——删除后残影直到下一键才消失。
            # 主屏 + 整屏定位绘制无此问题；shell 提示符被覆盖，退出清屏。
            sys.stdout.write("\033[?25l\033[2J\033[H\033[?2004h")
        sys.stdout.flush()
        self._entered = True

    def leave(self) -> None:
        """退出 TUI：恢复光标/属性，关粘贴协议。

        主屏模式退出时清屏（?2J?H）——本会话的绘制内容覆盖了 shell 提示符，
        清屏比留残屏干净；用户按回车即出新提示符。
        备用屏模式（BAWCODE_ALT=1）退屏即恢复原 shell 内容。
        """
        if not self._entered:
            return
        log.info("离开 TUI 界面")
        if os.environ.get("BAWCODE_ALT") == "1":
            sys.stdout.write("\033[?25h\033[0m\033[?1049l\033[?2004l")
        else:
            sys.stdout.write("\033[?25h\033[0m\033[2J\033[H\033[?2004l")
        sys.stdout.flush()
        self._entered = False

    def show_logo(self, seconds: float = 1.5) -> None:
        """兼容入口：转发到启动 Logo（主屏幕）"""
        show_startup_logo(seconds, self.project_name)
        self._logo_done = True

    def refresh_from_session(self, session, task: Optional[str] = None) -> None:
        self.messages = list(getattr(session, "messages", []) or [])
        self.plan = dict(getattr(session, "plan", {}) or {})
        self.steps = list(getattr(session, "steps", []) or [])
        self._tree_sig = None  # 内容已同步，强制重建树缓存
        if task is not None:
            self.task = task
        elif self.plan.get("title"):
            self.task = self.plan.get("title")
        self.render()

    def _has_conversation_content(self) -> bool:
        """会话区是否已有需要展示的内容（有则隐藏 Logo）"""
        for m in self.messages:
            role = m.get("role")
            mtype = m.get("type") or ""
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if role in ("user", "assistant", "tool"):
                return True
            if mtype in ("task", "plan", "refine", "diff", "code"):
                return True
            if role == "system" and mtype not in ("", None) and content:
                # 帮助/命令结果等也占用会话区
                return True
            if role == "system" and content and mtype == "help":
                return True
        if self.steps:
            return True
        plan = self.plan or {}
        if plan.get("content") or plan.get("title"):
            if plan.get("status") not in ("empty", "", None) or plan.get("content"):
                return True
        return False

    def _show_splash_logo(self) -> bool:
        """空会话区显示 Logo；界面开始展示其他文字时消失"""
        import os

        if os.environ.get("BAW_NO_LOGO") == "1":
            return False
        if not self.logo_enabled:
            return False
        return not self._has_conversation_content()

    def _compose_logo_body(self, w: int, height: int) -> List[str]:
        """在会话区居中绘制绿色渐变 BAW（就地渐变，非独立启动界面）"""
        frames = _logo_frames()
        idx = int(time.time() / 0.35) % len(frames)
        art = frames[idx]
        lines = [""] * max(0, height)
        if height <= 0:
            return lines
        top = max(0, height // 2 - len(art) // 2 - 1)
        for j, row in enumerate(art):
            if 0 <= top + j < height:
                indent = max(2, (w - _display_width(row)) // 2)
                lines[top + j] = " " * indent + row
        sub = "BAWCode"
        if top + len(art) < height:
            indent = max(2, (w - len(sub)) // 2)
            lines[top + len(art)] = " " * indent + self.c("dim") + sub + self.RESET
        return lines

    def cycle_mode(self) -> str:
        idx = policy.MODES.index(self.mode) if self.mode in policy.MODES else 0
        self.mode = policy.MODES[(idx + 1) % len(policy.MODES)]
        return self.mode

    # ----- 树 -----
    def _build_tree(self) -> List[TreeNode]:
        roots = []
        task_node = TreeNode("task", f"任务 · {_oneline(self.task or '（新会话）', 50)}", "task")
        roots.append(task_node)
        plan = self.plan or {}
        if plan.get("status") not in ("empty", "", None) or plan.get("content") or plan.get("title"):
            expanded = plan.get("status") in ("draft", "confirmed", "executing", "running")
            plan_node = TreeNode(
                "plan",
                f"计划 [{plan.get('status') or 'empty'}] {plan.get('title') or ''}",
                "plan",
                default_expanded=expanded,
                summary=_oneline(plan.get("content") or "", 40),
                detail=plan.get("content") or "",
            )
            if plan_node.detail:
                for i, line in enumerate(_wrap(plan_node.detail, 50)[:12]):
                    plan_node.children.append(TreeNode(f"plan:{i}", line, "plan_line"))
            task_node.children.append(plan_node)
        if self.steps:
            done = sum(1 for s in self.steps if s.get("status") == "done")
            step_root = TreeNode("steps", f"步骤 {done}/{len(self.steps)}", "steps")
            for step in self.steps:
                st = step.get("status", "pending")
                icon = _STATUS_ICON.get(st, "○")
                detail = step.get("detail") or ""
                expanded = (not self.collapse_done) or st in ("running", "pending")
                node = TreeNode(
                    f"step:{step.get('id')}",
                    f"{icon} {step.get('id')}. {step.get('title')} [{st}]",
                    "step",
                    default_expanded=expanded,
                    summary=_oneline(detail, 30),
                    detail=detail,
                )
                if detail:
                    node.children.append(TreeNode(f"step:{step.get('id')}:d", _oneline(detail, 50), "step_detail"))
                step_root.children.append(node)
            task_node.children.append(step_root)
        for index, item in enumerate(self.messages[-200:]):
            role = item.get("role")
            content = item.get("content") or ""
            msg_type = item.get("type") or ""
            tool_name = item.get("tool_name") or ""
            if role == "user" and msg_type == "task":
                continue
            if role == "tool" or msg_type == "tool":
                expanded = len(content) <= _COLLAPSE_THRESHOLD
                node = TreeNode(
                    f"msg:{index}",
                    f"⚙ {tool_name or 'tool'} · {_oneline(content, 30)}",
                    "tool",
                    default_expanded=expanded,
                    detail=content,
                )
            elif role == "assistant":
                head = _oneline(content, 40)
                node = TreeNode(f"msg:{index}", f"Agent · {head}", "assistant", detail=content)
                for i, line in enumerate(_wrap(content, 50)[1:8], start=1):
                    node.children.append(TreeNode(f"msg:{index}:{i}", _clip(line, 60), "text"))
            elif role == "user":
                node = TreeNode(f"msg:{index}", f"用户 · {_oneline(content, 40)}", "user", default_expanded=False, detail=content)
            elif msg_type == "help":
                node = TreeNode(f"msg:{index}", "帮助", "help", detail=content)
                for i, line in enumerate(content.splitlines()[:10]):
                    node.children.append(TreeNode(f"msg:{index}:{i}", _clip(line, 60), "help_line"))
            else:
                node = TreeNode(f"msg:{index}", f"系统 · {_oneline(content, 40)}", "system", default_expanded=False, detail=content)
            task_node.children.append(node)
        return roots

    def _tree_signature(self) -> tuple:
        """会话内容签名：命中则复用 _build_tree 结果，避免每帧全量重建"""
        msgs = self.messages[-200:]
        total = 0
        for m in msgs:
            total += len(m.get("content") or "")
        plan = self.plan or {}
        return (
            len(msgs),
            total,
            self.task or "",
            len(self.steps),
            plan.get("status") or "",
            len(plan.get("content") or ""),
            self.collapse_done,
        )

    def _flatten_tree(self) -> List[Tuple[TreeNode, int, bool]]:
        sig = self._tree_signature()
        if self._tree_sig != sig or self._tree_nodes is None:
            self._tree_nodes = self._build_tree()
            self._tree_sig = sig
        flat = []

        def walk(nodes, depth):
            for node in nodes:
                if node.id in self.expanded:
                    expanded = True
                elif f"!{node.id}" in self.expanded:
                    expanded = False
                else:
                    expanded = node.default_expanded
                flat.append((node, depth, expanded))
                if expanded and node.children:
                    walk(node.children, depth + 1)

        walk(self._tree_nodes, 0)
        self._flat_nodes = flat
        return flat

    def toggle_fold(self, index: int) -> None:
        if not self._flat_nodes:
            return
        index = max(0, min(index, len(self._flat_nodes) - 1))
        node, _, expanded = self._flat_nodes[index]
        if expanded:
            self.expanded.discard(node.id)
            self.expanded.add(f"!{node.id}")
        else:
            self.expanded.discard(f"!{node.id}")
            self.expanded.add(node.id)

    def _tree_rows(self, width: int) -> List[str]:
        # 树行缓存：按键路径（partial 重绘）不重建行字符串，只做窗口切片。
        # key 覆盖全部影响行内容的输入：宽度/会话内容/折叠态/焦点。
        key = (
            width,
            self._tree_sig,
            self.collapse_done,
            tuple(sorted(self.expanded)),
            self.focus,
            self.tree_cursor,
        )
        if self._tree_rows_key == key and self._tree_rows_cache is not None:
            return self._tree_rows_cache
        flat = self._flatten_tree()
        tree_focus = self.focus == "tree"
        colors = {
            "task": self.c("accent"),
            "plan": self.c("warn"),
            "plan_line": self.c("dim"),
            "steps": self.c("warn"),
            "step": self.c("ink"),
            "step_detail": self.c("dim"),
            "tool": self.c("tool"),
            "assistant": self.c("ok"),
            "user": self.c("accent"),
            "help": self.c("title"),
            "help_line": self.c("dim"),
            "system": self.c("dim"),
            "text": self.c("dim"),
        }
        rows = []
        if not flat:
            return [self.c("dim") + _clip("  —", width) + self.RESET]
        for abs_i, (node, depth, expanded) in enumerate(flat):
            has = bool(node.children or node.detail)
            marker = "▾" if has and expanded else ("▸" if has else "·")
            indent = " " * (depth * 2)
            label = f"{indent}{marker} {node.label}"
            if not expanded and node.summary:
                label += f" · {node.summary}"
            if abs_i == self.tree_cursor and tree_focus:
                rows.append(self.C_HL + _pad(_clip(_ANSI_RE.sub("", label), width), width) + self.RESET)
            else:
                rows.append(colors.get(node.kind, self.c("ink")) + _clip(label, width) + self.RESET)
            if expanded and node.detail and not node.children:
                for dline in _wrap(str(node.detail), max(10, width - depth * 2 - 4))[:8]:
                    rows.append(self.c("dim") + _clip(" " * (depth * 2 + 2) + dline, width) + self.RESET)
        self._tree_rows_key = key
        self._tree_rows_cache = rows
        return rows
        return rows

    def _rotating_tip(self) -> str:
        interval = float(self.keys.get("tip_interval") or 5)
        return TIPS[int(time.time() // interval) % len(TIPS)]

    def _compose_plain(self, width: int, height: int) -> List[str]:
        w = max(40, width)
        h = max(20, height)
        if self.settings_mode:
            return self._compose_settings(w, h)

        prompt = "> "
        raw = "".join(self.buffer)
        inner_w = max(10, w - len(prompt) - 1)

        # 确认态：输入区显示三选项
        if self.pending_tool:
            return self._compose_confirm(w, h, inner_w)

        logical = raw.splitlines() or [""]
        # 输入 wrap 缓存：长文本按键路径只重算变化的行切片，不重折行
        wkey = (inner_w, raw)
        if self._input_wrap_key == wkey and self._input_wrap_cache is not None:
            wrapped = self._input_wrap_cache
        else:
            wrapped = []
            for para in logical:
                wrapped.extend(_wrap(para, inner_w) or [""])
            self._input_wrap_key = wkey
            self._input_wrap_cache = wrapped
        if not wrapped:
            wrapped = [""]
        text_h = min(3, max(1, len(wrapped)))
        cand_show = min(3, len(self.candidates)) if self.candidates else 0
        input_zone_h = min(6, text_h + cand_show)

        bottom_fixed = 1 + 2 + 1 + 1  # rule + info/mode+tip + rule + status
        top_fixed = 1 + 1
        tree_h = max(4, h - top_fixed - bottom_fixed - input_zone_h)
        tree_focus = self.focus == "tree"
        mode_label = policy.MODE_LABELS.get(self.mode, self.mode)

        lines = []
        # 会话
        lines.append(self.C_HL + _pad(_clip(" 会话", w), w) + self.RESET if tree_focus else self.c("title") + "会话" + self.RESET)
        # 空会话：Logo 占位会话区；有内容后切回树
        if self._show_splash_logo():
            body = self._compose_logo_body(w, tree_h)
        else:
            rows = self._tree_rows(w)
            total = len(rows)
            if self.tree_cursor < self.scroll:
                self.scroll = self.tree_cursor
            if self.tree_cursor >= self.scroll + tree_h:
                self.scroll = self.tree_cursor - tree_h + 1
            self.scroll = max(0, min(self.scroll, max(0, total - tree_h)))
            visible = rows[self.scroll : self.scroll + tree_h]
            body = list(visible) + [""] * (tree_h - len(visible))
            body = body[:tree_h]
            if total > tree_h:
                if self.scroll > 0:
                    body[0] = self.c("dim") + _clip(f"  ↑{self.scroll}", w) + self.RESET
                if self.scroll + tree_h < total:
                    body[-1] = self.c("dim") + _clip(f"  ↓{total - self.scroll - tree_h}", w) + self.RESET
        lines.extend(body)
        lines.append(self._rule(w))

        # 输入区
        before = "".join(self.buffer[: self.cursor]) if self.buffer else ""
        ckey = (inner_w, before)
        if self._cursor_row_key == ckey and self._cursor_row_val is not None:
            cursor_row = self._cursor_row_val
            before_last_w = self._cursor_seg_w
        else:
            before_wrapped = _wrap(before, inner_w)
            cursor_row = max(0, len(before_wrapped) - 1)
            before_last_w = _display_width(before_wrapped[-1]) if before_wrapped else 0
            self._cursor_row_key = ckey
            self._cursor_row_val = cursor_row
            self._cursor_seg_w = before_last_w
        # 物理光标列（可视行内）：前缀宽 + 光标所在可视行文本宽；IME 组合窗跟随此位置
        self._cursor_col = _display_width(self._input_prompt) + before_last_w
        self._cursor_rel_row = cursor_row - self.input_scroll
        if cursor_row < self.input_scroll:
            self.input_scroll = cursor_row
        if cursor_row >= self.input_scroll + text_h:
            self.input_scroll = cursor_row - text_h + 1
        self.input_scroll = max(0, min(self.input_scroll, max(0, len(wrapped) - text_h)))
        vis = wrapped[self.input_scroll : self.input_scroll + text_h]
        input_rows = []
        accent = self.c("accent") if self.focus == "input" else self.c("dim")
        # 输入框：显示缓冲与光标
        if not raw.strip():
            input_rows.append(
                f"{accent}>{self.RESET} {self.c('dim')}{_clip('输入消息或 / 命令 · Enter 提交 · Tab 补全', inner_w)}{self.RESET}{accent}▌{RESET if False else self.RESET}"
            )
        else:
            for i, part in enumerate(vis):
                abs_row = self.input_scroll + i
                prefix = self._input_prompt if abs_row == 0 else " " * _display_width(self._input_prompt)
                mark = (
                    f"{self.c('accent')}▌{self.RESET}"
                    if (self.focus == "input" and abs_row == cursor_row)
                    else ""
                )
                input_rows.append(
                    f"{accent}{prefix}{self.RESET}{self.c('ink')}{_clip(part, inner_w)}{self.RESET}{mark}"
                )
        if cand_show:
            if self.candidate_index < self.cand_scroll:
                self.cand_scroll = self.candidate_index
            if self.candidate_index >= self.cand_scroll + cand_show:
                self.cand_scroll = self.candidate_index - cand_show + 1
            self.cand_scroll = max(0, min(self.cand_scroll, max(0, len(self.candidates) - cand_show)))
            for offset in range(cand_show):
                idx = self.cand_scroll + offset
                if idx >= len(self.candidates):
                    break
                cand = self.candidates[idx]
                label = _clip(f"  {cand.get('name','')}  {cand.get('hint','')}", w)
                if idx == self.candidate_index:
                    input_rows.append(self.C_HL + _pad(label, w) + self.RESET)
                else:
                    input_rows.append(self.c("ok") + label + self.RESET)
        while len(input_rows) < input_zone_h:
            input_rows.append(" ")
        lines.extend(input_rows[:input_zone_h])
        lines.append(self._rule(w))

        # 底部：项目 · git · 模型 · 模式（同一行）
        info = (
            f"→{self.project_name} · git:({self.git_branch}) · {self.model_name} · {mode_label}"
        )
        lines.append(self.c("accent") + _clip(info, w) + self.RESET)
        lines.append(self.c("dim") + _clip(self._rotating_tip(), w) + self.RESET)
        lines.append(self._rule(w))

        # 状态栏：token / 余额
        focus_tag = "会话" if tree_focus else "输入"
        token_txt = self.token_meter.status_text()
        status = f" {self.status or '就绪'} · {focus_tag} · {len(self.messages)}msg · {token_txt} "
        lines.append(self.c("dim") + _pad(_clip(status, w), w) + self.RESET)

        if len(lines) > h:
            lines = lines[: h - 1] + lines[-1:]
        while len(lines) < h:
            lines.append(" ")
        # 记录输入/状态行号，供局部重绘
        try:
            input_start = None
            # 从后往前找「→项目」信息行，其前为输入区
            for i, ln in enumerate(lines):
                plain = _ANSI_RE.sub("", ln)
                if plain.startswith("→") or plain.startswith("→"):
                    self._row_meta["info"] = i
                    self._row_meta["status"] = min(h - 1, i + 3)
                    self._row_meta["tip"] = min(h - 1, i + 1)
                    input_start = max(0, i - input_zone_h - 2)
                    break
            if input_start is None:
                input_start = max(0, h - input_zone_h - 5)
            self._row_meta["input_start"] = input_start
            self._row_meta["input_h"] = input_zone_h
            # 物理光标的屏幕位置（VT 1-based）：输入区可视首行 + 光标相对行
            self._cursor_screen_row = input_start + 2 + max(0, self._cursor_rel_row)
            self._cursor_screen_col = self._cursor_col + 1
        except Exception:
            pass
        return lines[:h]

    def _compose_confirm(self, w: int, h: int, inner_w: int) -> List[str]:
        """确认框：占用输入区，三选项"""
        pending = self.pending_tool or {}
        tool_name = pending.get("name", "")
        args = pending.get("arguments") or pending.get("args") or {}
        arg_text = _oneline(json.dumps(args, ensure_ascii=False), max(20, w - 30))
        tree_focus = self.focus == "tree"
        mode_label = policy.MODE_LABELS.get(self.mode, self.mode)
        options = [
            "允许执行一次",
            "在本项目中始终允许执行该类指令",
            "拒绝并说明",
        ]
        bottom_fixed = 1 + 2 + 1 + 1
        confirm_h = 5  # 标题 + 3选项 + reject输入行
        top_fixed = 1 + 1
        tree_h = max(4, h - top_fixed - bottom_fixed - confirm_h)

        lines = []
        lines.append(self.C_HL + _pad(_clip(" 会话", w), w) + self.RESET if tree_focus else self.c("title") + "会话" + self.RESET)
        if self._show_splash_logo():
            body = self._compose_logo_body(w, tree_h)
        else:
            rows = self._tree_rows(w)
            total = len(rows)
            self.scroll = max(0, min(self.scroll, max(0, total - tree_h)))
            visible = rows[self.scroll : self.scroll + tree_h]
            body = list(visible) + [""] * tree_h
            body = body[:tree_h]
        lines.extend(body[:tree_h])
        lines.append(self._rule(w))

        lines.append(self.c("warn") + _clip(f"工具确认 · {tool_name} · {arg_text}", w) + self.RESET)
        for i, opt in enumerate(options):
            if i == self.confirm_index:
                lines.append(self.C_HL + _pad(_clip(f"  ▸ {opt}", w), w) + self.RESET)
            else:
                lines.append(self.c("ink") + _clip(f"    {opt}", w) + self.RESET)
        if self.confirm_reject_edit and self.confirm_index == 2:
            reason = "".join(self.reject_buffer)
            lines.append(self.c("accent") + f"原因> {self.RESET}{self.c('ink')}{_clip(reason, inner_w)}{self.RESET}{self.c('accent')}▌{self.RESET}")
        else:
            lines.append(self.c("dim") + _clip("  ↑↓选择 · Enter确认 · 空原因=默认拒绝文案", w) + self.RESET)
        lines.append(self._rule(w))

        info = f"→{self.project_name} · git:({self.git_branch}) · {self.model_name} · {mode_label}"
        lines.append(self.c("accent") + _clip(info, w) + self.RESET)
        lines.append(self.c("dim") + _clip("工具调用待确认（占用输入区）", w) + self.RESET)
        lines.append(self._rule(w))
        status = f" 等待确认 · {tool_name} · {self.token_meter.status_text()} "
        lines.append(self.c("dim") + _pad(_clip(status, w), w) + self.RESET)
        while len(lines) < h:
            lines.append(" ")
        return lines[:h]

    def render(self, *args, **kwargs) -> None:
        """整帧绘制（对齐 music player Live 方案）：每帧无条件重画所有行。

        不做行 diff、不做局部重绘——diff/partial 在主屏上曾产生残留
        （旧内容未覆盖），且全帧组合成本实测 <1ms，优化是负资产。"""
        w, h = _term_size()
        lines = self._compose_plain(w, h)
        if len(lines) < h:
            lines = list(lines) + [""] * (h - len(lines))
        lines = lines[:h]
        parts = []
        for i, line in enumerate(lines):
            parts.append(f"\033[{i + 1};1H\033[2K{_clip_keep_ansi(line, w)}")
        self._last_frame_lines = lines
        sys.stdout.write("".join(parts))
        self._write_cursor_pos()
        sys.stdout.flush()

    def _write_cursor_pos(self) -> None:
        """把物理光标钉在输入光标处（不显示）。

        IME 的组合串/候选窗定位、以及部分输入法对退格键的决策都依赖物理
        光标位置；隐藏且不定位时 IME 拿到的是错误位置，行为可能异常
        （对齐 qwen-code software-cursor 的 setCursorPosition 方案）。"""
        if self.settings_mode or self.pending_tool or self.focus != "input":
            return
        row = getattr(self, "_cursor_screen_row", None)
        col = getattr(self, "_cursor_screen_col", None)
        if row is None or col is None:
            return
        h, w = _term_size()
        try:
            sys.stdout.write(f"\033[{max(1, min(h, row))};{max(1, min(w, col))}H")
        except Exception:
            pass

    def render_partial_input(self) -> None:
        """兼容入口（TMP 方案下与全帧等价）：局部重绘已废弃，统一全帧重画。

        历史上的 partial/diff 路径在主屏上产生过残留（旧内容未被覆盖），
        全帧组合成本实测 <1ms，保留此入口只为不破坏既有调用点。"""
        self.render()

    _PAINT_MIN_INTERVAL = 0.03  # 绘制最小间隔（≈33fps）；间隔外立即刷，间隔内挂起补刷
    _IDLE_REPAINT = 0.05        # 空闲重画周期（对齐 TMP 20fps 主循环的兜底行为）

    def _do_paint(self, partial: bool = True) -> None:
        self._last_paint = time.time()
        try:
            from core.keyinput import _LOG_ON, _log as _klog

            if _LOG_ON:
                _klog("paint full")
        except Exception:
            pass
        self.render()

    def _schedule_paint(self, partial: bool = True) -> None:
        """输入绘制：距上次绘制超过最小间隔则**立即刷新**（零延迟，对齐
        music player 每键即画）；间隔内挂起，由主循环 tick 到点补刷，
        保证最后一次更新不丢。partial 参数保留兼容，实际统一全帧。"""
        now = time.time()
        if not self._paint_pending and (now - self._last_paint) >= self._PAINT_MIN_INTERVAL:
            # 候选区同步后再画（buffer 刚变，_refresh_candidates 内部有未变跳过）
            try:
                self._refresh_candidates(config=self._cand_config)
            except Exception:
                pass
            self._paint_pending = False
            self._paint_partial = True
            self._do_paint()
        else:
            if not self._paint_pending:
                self._paint_at = now + self._PAINT_MIN_INTERVAL
            self._paint_pending = True
            self._paint_partial = True

    def _flush_paint(self) -> None:
        if not self._paint_pending:
            return
        if time.time() < self._paint_at:
            return
        self._paint_pending = False
        self._do_paint()

    # ----- 输入 / 补全 -----
    def _word_start(self) -> int:
        text = "".join(self.buffer)
        i = self.cursor
        while i > 0 and not text[i - 1].isspace():
            i -= 1
        return i

    def _refresh_candidates(self, config=None) -> None:
        text = "".join(self.buffer)
        # 输入循环每帧调用；缓冲与光标未变时跳过补全计算
        if text == self._cand_text and self.cursor == self._cand_cursor and config is self._cand_config:
            return
        self._cand_text = text
        self._cand_cursor = self.cursor
        self._cand_config = config
        start = self._word_start()
        word = text[start : self.cursor] if start <= self.cursor else ""
        if word.startswith("/"):
            self.candidates = cmdsys.complete(word, config=config)
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
        text = "".join(self.buffer)
        start = self._word_start()
        end = self.cursor
        while end < len(text) and not text[end].isspace():
            end += 1
        # /model deepseek-chat 需要整段替换
        if " " in name:
            new_text = name + " " + text[end:]
            self.buffer = list(new_text)
            self.cursor = len(name) + 1
        else:
            new_text = text[:start] + name + " " + text[end:]
            self.buffer = list(new_text)
            self.cursor = start + len(name) + 1
        self.candidates = []
        self.candidate_index = 0
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
        """界面内输入框：回车提交；中文键入合并绘制，避免整屏逐键刷新。

        主循环结构（TMP 对齐）：
          每轮 = 刷新补全候选 → 消费到期的挂起绘制 → 批量读事件 → 分发。
          事件队列空时：20ms 心跳 + 空闲重画兜底（_IDLE_REPAINT）。
        事件分发要点：
          - char/backspace/delete/光标移动 → 改 buffer/cursor → _schedule_paint
            （leading-edge 节流：间隔外立即画，间隔内挂起 30ms 补刷）
          - paste → 整段插入 buffer，绝不触发 submit（keyinput 管线保证）
          - ↑↓/Tab/PgUp/PgDn 等低频全帧操作 → 直接 render()
        """
        self.settings_mode = False
        if self.pending_tool:
            return self._confirm_loop()
        self.focus = "input"
        self.buffer = []
        self.cursor = 0
        self.candidates = []
        self.input_scroll = 0
        self._paint_at = 0.0
        self._paint_pending = False
        self._input_prompt = prompt or "> "
        self.render()
        pending_events: List[Tuple[str, Any]] = []
        while True:
            self._refresh_candidates(config=config)
            self._flush_paint()
            # 批处理读取：一次抽干输入队列逐个消费；无键时阻塞等待 20ms 心跳。
            # 绘制频率由 _schedule_paint 节流合并，不随事件数线性增长。
            if not pending_events:
                pending_events = _read_events()
                if not pending_events:
                    # TMP 式空闲重画兜底：终端（conhost/WT）偶发延迟渲染一帧
                    # 应用输出，事件驱动下该帧会滞留到下一个键（"删除不消失
                    # 直到打下一个字"）；低频恒定重画保证被吞的帧自动修正。
                    # 全帧成本 0.4ms，50ms 周期下 CPU <1%。
                    if time.time() - self._last_paint >= self._IDLE_REPAINT:
                        self._paint_pending = False
                        self._do_paint()
                    continue
            key = pending_events.pop(0)
            kind, value = key
            if kind == "tick":
                self._flush_paint()
                continue
            if kind == "interrupt":
                return "/exit"
            if self._is_mode_switch(kind, value):
                mode = self.cycle_mode()
                self.status = f"{policy.MODE_LABELS.get(mode, mode)}"
                self._paint_pending = False
                self.render()
                continue
            if kind in ("submit_ctrl", "submit"):
                # Enter / Ctrl+Enter 提交当前行
                if kind == "submit" and self.focus == "tree":
                    self.toggle_fold(self.tree_cursor)
                    self._paint_pending = False
                    self.render()
                    continue
                line = "".join(self.buffer)
                if line.strip():
                    self.input_history.append(line)
                self.hist_index = len(self.input_history)
                self.buffer = []
                self.cursor = 0
                self.candidates = []
                self._paint_pending = False
                return line
            if kind == "tab":
                if self.candidates:
                    self._apply_completion()
                    self._paint_pending = False
                    self.render()
                    continue
                if self.focus == "input":
                    self.focus = "tree"
                    flat = self._flatten_tree()
                    self.tree_cursor = max(0, len(flat) - 1)
                    self.scroll = max(0, len(flat) - 4)
                else:
                    self.focus = "input"
                self._paint_pending = False
                self.render()
                continue
            if kind == "backspace":
                if self.focus == "input" and self.cursor > 0:
                    self.buffer.pop(self.cursor - 1)
                    self.cursor -= 1
                    self._schedule_paint()
            elif kind == "delete":
                if self.focus == "input" and self.cursor < len(self.buffer):
                    self.buffer.pop(self.cursor)
                    self._schedule_paint()
            dirn = _key_direction(kind, value)
            if dirn == "up":
                if self.candidates:
                    self.candidate_index = max(0, self.candidate_index - 1)
                    self._paint_pending = False
                    self.render()
                elif self.focus == "tree":
                    step = self._scroll_step("up")
                    self.tree_cursor = max(0, self.tree_cursor - step)
                    if self.tree_cursor < self.scroll:
                        self.scroll = self.tree_cursor
                    self._paint_pending = False
                    self.render()
                elif self.input_history:
                    self.hist_index = max(0, self.hist_index - 1)
                    self.buffer = list(self.input_history[self.hist_index])
                    self.cursor = len(self.buffer)
                    self._paint_pending = False
                    self.render()
            elif dirn == "down":
                if self.candidates:
                    self.candidate_index = min(len(self.candidates) - 1, self.candidate_index + 1)
                    self._paint_pending = False
                    self.render()
                elif self.focus == "tree":
                    step = self._scroll_step("down")
                    flat = self._flatten_tree()
                    self.tree_cursor = min(max(0, len(flat) - 1), self.tree_cursor + step)
                    if self.tree_cursor >= self.scroll + 4:
                        self.scroll = self.tree_cursor - 3
                    self._paint_pending = False
                    self.render()
                else:
                    if self.hist_index < len(self.input_history) - 1:
                        self.hist_index += 1
                        self.buffer = list(self.input_history[self.hist_index])
                    else:
                        self.hist_index = len(self.input_history)
                        self.buffer = []
                    self.cursor = len(self.buffer)
                    self._paint_pending = False
                    self.render()
            elif dirn == "left":
                if self.focus == "tree":
                    if self._flat_nodes and self.tree_cursor < len(self._flat_nodes) and self._flat_nodes[self.tree_cursor][2]:
                        self.toggle_fold(self.tree_cursor)
                        self._paint_pending = False
                        self.render()
                else:
                    self.cursor = max(0, self.cursor - 1)
                    self._schedule_paint()
            elif dirn == "right":
                if self.focus == "tree":
                    if self._flat_nodes and self.tree_cursor < len(self._flat_nodes) and not self._flat_nodes[self.tree_cursor][2]:
                        self.toggle_fold(self.tree_cursor)
                        self._paint_pending = False
                        self.render()
                else:
                    self.cursor = min(len(self.buffer), self.cursor + 1)
                    self._schedule_paint()
            elif kind == "home":
                self.cursor = 0
                self._schedule_paint()
            elif kind == "end":
                self.cursor = len(self.buffer)
                self._schedule_paint()
            elif kind == "scroll_up":
                self.scroll = max(0, self.scroll - self._scroll_step("pu"))
                self._paint_pending = False
                self.render()
            elif kind == "scroll_down":
                self.scroll += self._scroll_step("pd")
                self._paint_pending = False
                self.render()
            elif kind == "escape":
                if self.candidates:
                    self.candidates = []
                    self._paint_pending = False
                    self.render()
                else:
                    self.buffer = []
                    self.cursor = 0
                    self._schedule_paint()
            elif kind == "clear":
                self.buffer = []
                self.cursor = 0
                self._paint_pending = False
                self.render()
            elif kind == "paste":
                # 粘贴整体插入：换行归一为 \n，绝不触发 submit（管线保证）
                text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
                if text and self.focus == "input":
                    self.buffer[self.cursor : self.cursor] = list(text)
                    self.cursor += len(text)
                    self._schedule_paint()
            elif kind == "char":
                if self.focus == "tree":
                    if value == " ":
                        self.toggle_fold(self.tree_cursor)
                        self._paint_pending = False
                        self.render()
                    elif value in ("l", "L") and self._flat_nodes and not self._flat_nodes[self.tree_cursor][2]:
                        self.toggle_fold(self.tree_cursor)
                        self._paint_pending = False
                        self.render()
                    elif value in ("h", "H") and self._flat_nodes and self._flat_nodes[self.tree_cursor][2]:
                        self.toggle_fold(self.tree_cursor)
                        self._paint_pending = False
                        self.render()
                elif value and all(ord(ch) >= 32 for ch in value):
                    chunk = self._append_text_burst(value)
                    self.buffer[self.cursor : self.cursor] = list(chunk)
                    self.cursor += len(chunk)
                    self._schedule_paint()

    def _confirm_loop(self) -> str:
        """工具确认：1 允许一次 / 2 本项目始终允许 / 3 拒绝（可输入原因）"""
        self.settings_mode = False
        self.render()
        pending = self.pending_tool or {}
        tool_name = pending.get("name", "")
        print(f"工具确认: {tool_name}")
        print("  1) 允许执行一次")
        print("  2) 在本项目中始终允许该类指令")
        print("  3) 拒绝（可输入原因，直接回车则使用默认拒绝）")
        try:
            choice = input("请选择> ").strip()
        except (EOFError, KeyboardInterrupt):
            return policy.default_reject_message("")
        if choice == "1":
            return "__ALLOW_ONCE__"
        if choice == "2":
            return "__ALLOW_ALWAYS__"
        try:
            reason = input("拒绝原因> ").strip()
        except (EOFError, KeyboardInterrupt):
            reason = ""
        return policy.default_reject_message(reason)

    def choose(self, options: List[tuple], prompt: str = "") -> str:
        lines = [f"{prompt}:"]
        for key, label in options:
            lines.append(f"  {key}) {label}")
        self.messages.append({"role": "system", "content": "\n".join(f"{k} {v}" for k, v in options), "type": "help"})
        self.render()
        return self.read_line().strip()

    def _compose_settings(self, w: int, h: int) -> List[str]:
        """设置：标签页 + 表单；choice 用 ←→，text 直接键入"""
        tabs = self.settings_tabs
        tab_i = self.settings_tab
        lines = []
        lines.append(self.c("title") + _pad(_clip(f" {self.project_name} · 设置 · {self.model_name}", w), w) + self.RESET)
        # 标签栏
        tab_parts = []
        for i, name in enumerate(tabs):
            if i == tab_i:
                tab_parts.append(self.C_HL + f" {name} " + self.RESET)
            else:
                tab_parts.append(self.c("dim") + f" {name} " + self.RESET)
        tab_line = " ".join(tab_parts) + self.c("dim") + "  Tab切换标签 · ↑↓选项 · ←→切换 · Enter保存 · Esc放弃" + self.RESET
        lines.append(_clip_keep_ansi(tab_line, w))
        lines.append(self._rule(w))

        fields = self.settings_fields or []
        list_h = max(8, h - 8)
        if self.settings_index < self._settings_scroll:
            self._settings_scroll = self.settings_index
        if self.settings_index >= self._settings_scroll + list_h:
            self._settings_scroll = self.settings_index - list_h + 1
        self._settings_scroll = max(0, min(self._settings_scroll, max(0, len(fields) - list_h)))

        body = []
        if not fields:
            body.append(self.c("dim") + "  （本页无配置项）" + self.RESET)
        else:
            visible = fields[self._settings_scroll : self._settings_scroll + list_h]
            for offset, field in enumerate(visible):
                i = self._settings_scroll + offset
                label = field.get("label", "")
                ftype = field.get("type", "text")
                value = self.settings_scratch.get(field["key"], field.get("current"))
                if ftype == "choice":
                    options = field.get("options") or []
                    show = "" if value is None else str(value)
                    hint_lr = " ←→"
                    display = _clip(f"{label:20}  < {show} >{hint_lr}", w - 4)
                elif ftype == "bool":
                    show = "开" if value in (True, "true", "1", 1) else "关"
                    display = _clip(f"{label:20}  < {show} > ←→", w - 4)
                elif ftype == "action":
                    display = _clip(f"{label:20}  [ Enter 执行 ]", w - 4)
                elif ftype == "sep":
                    display = _clip(f"  {label}", w - 4)
                else:
                    show = "" if value is None else str(value)
                    marker = "▌" if i == self.settings_index else ""
                    display = _clip(f"{label:20}  {show}{marker}", w - 4)
                if field.get("type") == "sep":
                    body.append(self.c("dim") + _pad(display, w) + self.RESET)
                    continue
                prefix = f"▸ {display}" if i == self.settings_index else f"  {display}"
                if i == self.settings_index:
                    body.append(self.C_HL + _pad(prefix, w) + self.RESET)
                else:
                    body.append(self.c("ink") + prefix + self.RESET)
        body.extend([""] * (list_h - len(body)))
        lines.extend(body[:list_h])
        lines.append(self._rule(w))
        cur = fields[self.settings_index] if 0 <= self.settings_index < len(fields) else {}
        hint = f" {cur.get('label','')} · {cur.get('hint','')} · type={cur.get('type','text')}"
        if getattr(self, "settings_notice", ""):
            lines.append(self.c("ok") + _clip(" " + self.settings_notice, w) + self.RESET)
        else:
            lines.append(self.c("dim") + _clip(hint, w) + self.RESET)
        lines.append(self._rule(w))
        lines.append(self.c("accent") + _pad(_clip(f"→{self.project_name} · {self.model_name} · {policy.MODE_LABELS.get(self.mode, self.mode)}", w), w) + self.RESET)
        while len(lines) < h:
            lines.append(" ")
        return lines[:h]

    # ----- 设置：三标签页 -----
    settings_tabs = ["提供商", "模型", "系统", "快捷键"]

    # 快捷键可选值（设置页 ←→）
    SHORTCUT_OPTIONS = {
        "switch_mode": [
            "shift+tab",
            "f2",
            "f3",
            "ctrl+g",
            "ctrl+t",
            "ctrl+e",
            "`",
            "~",
        ],
        "switch_focus": ["tab", "f4", "ctrl+o"],
        "send": ["ctrl+enter", "f5", "ctrl+s"],
        "newline": ["enter", "shift+enter"],
        "complete": ["tab", "ctrl+space"],
        "scroll_up": ["pageup", "ctrl+up"],
        "scroll_down": ["pagedown", "ctrl+down"],
        "expand": ["right", "l"],
        "collapse": ["left", "h"],
    }

    def _is_mode_switch(self, kind: str, value: Any) -> bool:
        """shift+tab 始终切换模式；额外尊重设置中的 switch_mode 映射"""
        if kind == "mode_switch":
            return True
        binding = str(self.keys.get("switch_mode") or "shift+tab").strip().lower()
        if not binding or binding == "shift+tab":
            return kind == "mode_switch"
        if kind == "hotkey" and str(value).lower() == binding:
            return True
        if kind == "char" and binding not in ("shift+tab",) and str(value).lower() == binding:
            return True
        return False

    def _mode_key_label(self) -> str:
        return str(self.keys.get("switch_mode") or "shift+tab")

    def _settings_model_names(self, config) -> List[str]:
        names = config.list_model_names() or [config.model_name]
        return names

    # 焦点离开时才重建模型列表的文本字段
    _MODEL_LIST_TEXT_KEYS = frozenset({"provider_id", "model_id"})

    def _schedule_settings_refresh(self) -> None:
        """登记一次防抖重建，到期后仅执行一次 _build_settings_fields"""
        self._settings_debounce_at = time.time() + float(self.settings_debounce_sec)
        self._settings_debounce_pending = True

    def _settings_snapshot(self, config) -> dict:
        s = self.settings_scratch
        return {
            "active_provider_id": s.get("active_provider_id", config.data.get("active_provider_id")),
            "provider_id": s.get("provider_id", self.settings_provider_id),
            "provider_name": s.get("provider_name"),
            "api_key": s.get("api_key"),
            "base_url": s.get("base_url"),
            "balance_url": s.get("balance_url"),
            "provider_temperature": s.get("provider_temperature"),
            "default_model_id": s.get("default_model_id"),
            "model_id": s.get("model_id"),
            "theme": s.get("theme", getattr(config, "theme", "")),
            "mode": s.get("mode", getattr(config, "mode", "")),
            "select_model": s.get("select_model"),
            "task_plan": s.get("task_plan"),
            "task_code": s.get("task_code"),
            "task_review": s.get("task_review"),
            "switch_mode": s.get("key_switch_mode"),
        }

    def _settings_is_dirty(self, config) -> bool:
        if not self._settings_baseline:
            return False
        now = self._settings_snapshot(config)
        for key, old in self._settings_baseline.items():
            new = now.get(key)
            if new is None and old is None:
                continue
            if str(new if new is not None else "") != str(old if old is not None else ""):
                return True
        return False

    def _settings_apply_active_provider(self, config) -> str:
        """将「激活提供商」写入配置并生效"""
        s = self.settings_scratch
        active = str(s.get("active_provider_id") or "").strip()
        if not active:
            chosen = str(s.get("switch_provider") or "").strip()
            if chosen and chosen != "(新建)":
                active = chosen
        if not active:
            active = str(s.get("provider_id") or self.settings_provider_id or "").strip()
        if active and config.find_provider(active):
            config.data["active_provider_id"] = active
            s["active_provider_id"] = active
            config.apply_active()
            return active
        return ""

    def _cancel_settings_refresh(self) -> None:
        self._settings_debounce_pending = False
        self._settings_debounce_at = 0.0

    def _flush_settings_refresh(self, config, force: bool = False) -> bool:
        """若防抖到期或 force，则重建设置字段表；返回是否发生了重建"""
        if force or self._settings_debounce_pending:
            if force or time.time() >= self._settings_debounce_at:
                self._settings_debounce_pending = False
                self._build_settings_fields(config)
                return True
        return False

    def _immediate_settings_refresh(self, config) -> None:
        """立刻重建（切标签/保存/切换提供商等），并取消未完成的防抖"""
        self._cancel_settings_refresh()
        self._build_settings_fields(config)

    def _schedule_settings_paint(self, delay: Optional[float] = None) -> None:
        self._settings_paint_at = time.time() + (self.settings_paint_sec if delay is None else delay)
        self._settings_paint_pending = True

    def _flush_settings_paint(self) -> bool:
        if self._settings_paint_pending and time.time() >= self._settings_paint_at:
            self._settings_paint_pending = False
            self.render()
            return True
        return False

    def _append_text_burst(self, first: str) -> str:
        """keyinput 已合并可打印字符；此处禁止再从 msvcrt 抽队列。

        二次 drain 会吃掉 0xE0/0x00 扩展键前缀，导致后续扫描码被当成
        普通字符，表现为设置页/输入框方向键失效（对齐 music player：只走一条读键路径）。
        """
        return first or ""

    def _build_settings_fields(self, config) -> None:
        """按当前标签生成字段列表"""
        tab = self.settings_tabs[self.settings_tab]
        fields: List[dict] = []
        scratch_keep = dict(self.settings_scratch)
        if tab == "提供商":
            providers = config.providers() or [{}]
            pids = [p.get("provider_id", "") for p in providers]
            pid = self.settings_provider_id or config.data.get("active_provider_id") or (pids[0] if pids else "")
            if pid not in pids and pids:
                pid = pids[0]
            self.settings_provider_id = pid
            provider = config.find_provider(pid) or {}
            # 实时模型列表 = 配置中已有 + 本页/模型页工作副本
            live_models = self._provider_live_models(config, pid)
            live_mids = [m.get("model_id") for m in live_models]
            model_list = ",".join(live_mids) if live_mids else "（暂无，请在模型页添加后保存）"
            default_id = str(
                scratch_keep.get("default_model_id")
                or provider.get("default_model_id")
                or ""
            )
            # 默认模型随实时列表自动对齐
            if live_mids and default_id not in live_mids:
                default_id = live_mids[0]
            if not live_mids:
                default_id = ""
            fields = [
                # —— 切换与激活（置顶，与下方配置隔离）——
                {
                    "key": "switch_provider",
                    "label": "切换提供商",
                    "type": "choice",
                    "options": pids + ["(新建)"],
                    "current": pid if pid in pids else (pid or "(新建)"),
                    "hint": "←→ 选择要编辑的提供商",
                },
                {
                    "key": "active_provider_id",
                    "label": "激活提供商",
                    "type": "choice",
                    "options": pids or ["-"],
                    "current": config.data.get("active_provider_id", pid),
                    "hint": "←→ 运行时使用的提供商",
                },
                {"key": "_sep_switch", "label": "——————————————", "type": "sep", "hint": "以下为提供商配置"},
                # —— 提供商配置 ——
                {
                    "key": "provider_id",
                    "label": "提供商ID",
                    "type": "text",
                    "current": scratch_keep.get("provider_id", provider.get("provider_id", pid)),
                    "hint": "不存在则创建；中文 IME 提交后才会写入",
                },
                {
                    "key": "provider_name",
                    "label": "显示名称",
                    "type": "text",
                    "current": scratch_keep.get("provider_name", provider.get("name", pid)),
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "text",
                    "current": scratch_keep.get("api_key", provider.get("api_key", "")),
                    "hint": "保存时检测是否更新",
                },
                {
                    "key": "base_url",
                    "label": "Base URL",
                    "type": "text",
                    "current": scratch_keep.get("base_url", provider.get("base_url", "")),
                    "hint": "保存时检测是否更改",
                },
                {
                    "key": "balance_url",
                    "label": "余额URL",
                    "type": "text",
                    "current": scratch_keep.get("balance_url", provider.get("balance_url", "")),
                },
                {
                    "key": "provider_temperature",
                    "label": "默认温度",
                    "type": "text",
                    "current": scratch_keep.get("provider_temperature", provider.get("temperature", 1.0)),
                },
                {
                    "key": "default_model_id",
                    "label": "默认模型",
                    "type": "choice",
                    "options": live_mids or ["（无模型）"],
                    "current": default_id or (live_mids[0] if live_mids else "（无模型）"),
                    "hint": "←→ 按实时模型列表选择",
                },
                {
                    "key": "provider_models_list",
                    "label": "实时模型列表",
                    "type": "text",
                    "current": model_list,
                    "hint": "只读 · 与模型页工作列表同步",
                },
                {"key": "_sep_cfg", "label": "——————————————", "type": "sep", "hint": ""},
                {
                    "key": "save_provider",
                    "label": "保存提供商",
                    "type": "action",
                    "hint": "Enter：id 存在则 diff 模型并更新字段，否则创建",
                },
            ]
        elif tab == "模型":
            names = self._settings_model_names(config)
            active = config.model_name
            current_model = self.settings_model_name or active
            if current_model not in names:
                current_model = active if active in names else (names[0] if names else "")
            self.settings_model_name = current_model
            row = config.find_model(current_model) or {}
            pids = [p.get("provider_id", "") for p in config.providers()] or ["-"]
            edit_pid = row.get("provider_id") or self.settings_provider_id or config.data.get("active_provider_id")
            live = self._provider_live_models(config, edit_pid)
            live_names = [make_model_name(edit_pid, m.get("model_id", "")) for m in live]
            if live_names and current_model not in live_names:
                current_model = live_names[0]
                self.settings_model_name = current_model
                row = config.find_model(current_model) or {}
            modalities = row.get("modalities") or ["text"]
            mod_joined = ",".join(modalities)
            mod_choices = [
                "text",
                "text,vision",
                "text,vision,audio",
                "text,audio",
                "text,embedding",
            ]
            if mod_joined not in mod_choices:
                mod_choices = [mod_joined] + mod_choices
            task = config.task_models()
            model_options = names or live_names or ["-"]
            fields = [
                {"key": "select_provider", "label": "提供商", "type": "choice", "options": pids, "current": edit_pid, "hint": "←→ 选择"},
                {"key": "select_model", "label": "模型", "type": "choice", "options": live_names or model_options, "current": current_model, "hint": "实时列表"},
                {"key": "model_id", "label": "模型ID", "type": "text", "current": row.get("model_id", "")},
                {"key": "modalities", "label": "支持模态", "type": "choice", "options": mod_choices, "current": mod_joined, "hint": "←→ 预置组合"},
                {"key": "context_window", "label": "最大上下文", "type": "text", "current": row.get("context_window", 0)},
                {"key": "max_tokens", "label": "最大输出token", "type": "text", "current": row.get("max_tokens", 0)},
                {"key": "model_temperature", "label": "温度", "type": "text", "current": row.get("temperature", 1.0)},
                {"key": "thinking_effort", "label": "思考强度", "type": "choice", "options": THINKING_OPTIONS, "current": row.get("thinking_effort", "none"), "hint": "←→"},
                {"key": "task_plan", "label": "规划模型", "type": "choice", "options": names or model_options, "current": task.get("plan", active), "hint": "任务规划默认模型"},
                {"key": "task_code", "label": "编写模型", "type": "choice", "options": names or model_options, "current": task.get("code", active), "hint": "代码编写默认模型"},
                {"key": "task_review", "label": "审查模型", "type": "choice", "options": names or model_options, "current": task.get("review", active), "hint": "代码审查默认模型"},
                {"key": "add_provider", "label": "添加提供商", "type": "action", "hint": "Enter 后输入 provider_id"},
                {"key": "add_model", "label": "添加模型到提供商", "type": "action", "hint": "写入工作列表，保存提供商时 diff 生效"},
            ]
        elif tab == "快捷键":
            key_defs = [
                ("switch_mode", "切换模式", "Shift+Tab 始终有效；此为额外映射"),
                ("switch_focus", "切换焦点", "会话树 ↔ 输入框"),
                ("send", "发送", "Ctrl+Enter 默认"),
                ("newline", "换行", "Enter / Shift+Enter"),
                ("complete", "命令补全", "有候选时 Tab 优先补全"),
                ("expand", "树展开", "焦点在会话树时"),
                ("collapse", "树折叠", "焦点在会话树时"),
                ("scroll_up", "滚动上", "会话历史向上"),
                ("scroll_down", "滚动下", "会话历史向下"),
            ]
            fields = []
            for key, label, hint in key_defs:
                options = list(self.SHORTCUT_OPTIONS.get(key) or [self.keys.get(key, "")])
                current = str(self.keys.get(key) or options[0]).lower()
                if current not in options:
                    options = [current] + options
                fields.append(
                    {
                        "key": f"key_{key}",
                        "label": label,
                        "type": "choice",
                        "options": options,
                        "current": current,
                        "hint": hint + " · ←→ 选择",
                        "shortcut": key,
                    }
                )
        else:
            ui_cfg = (config.data or {}).get("ui") or {}
            mem_cfg = (config.data or {}).get("memory") or {}
            llm_cfg = (config.data or {}).get("llm") or {}
            themes = ["dark"] + [t for t in config.list_themes() if t != "dark"]
            fields = [
                {"key": "theme", "label": "主题", "type": "choice", "options": themes, "current": config.theme, "hint": "←→ 切换主题"},
                {"key": "mode", "label": "访问模式", "type": "choice", "options": list(policy.MODES), "current": config.mode, "hint": "auto/manual/full"},
                {"key": "logo", "label": "会话区Logo", "type": "bool", "current": bool(ui_cfg.get("logo", True)), "hint": "←→ 开/关"},
                {"key": "font_size", "label": "字体大小", "type": "text", "current": getattr(config, "font_size", 16)},
                {"key": "tip_interval", "label": "提示间隔秒", "type": "text", "current": ui_cfg.get("tip_interval", 5)},
                {"key": "settings_debounce_ms", "label": "设置防抖ms", "type": "text", "current": ui_cfg.get("settings_debounce_ms", 350), "hint": "模型列表实时刷新防抖，默认 350"},
                {"key": "retry_times", "label": "LLM重试次数", "type": "text", "current": llm_cfg.get("retry_times", 3)},
                {"key": "retry_delay", "label": "LLM重试延迟", "type": "text", "current": llm_cfg.get("retry_delay", 1.0)},
                {"key": "scroll_v0", "label": "滚动v0", "type": "text", "current": ui_cfg.get("scroll_v0", 1.0)},
                {"key": "scroll_hold_ms", "label": "长按阈值ms", "type": "text", "current": ui_cfg.get("scroll_hold_ms", 150)},
                {"key": "scroll_max_step", "label": "滚动最大步长", "type": "text", "current": ui_cfg.get("scroll_max_step", 20)},
                {"key": "auto_compress", "label": "自动压缩上下文", "type": "bool", "current": bool(mem_cfg.get("auto_compress", True)), "hint": "←→"},
                {"key": "compress_threshold", "label": "压缩阈值", "type": "text", "current": mem_cfg.get("compress_threshold", 0.8)},
                {"key": "active_model_name", "label": "全局默认模型", "type": "choice", "options": self._settings_model_names(config), "current": config.model_name, "hint": "←→ 切换当前模型"},
            ]
        self.settings_fields = fields
        # 初始化 scratch：保留同 key 已编辑值
        for field in fields:
            if field["key"] not in scratch_keep:
                scratch_keep[field["key"]] = field.get("current")
            # choice 新 options 时校正
            if field.get("type") in ("choice", "bool") and field.get("options"):
                val = scratch_keep[field["key"]]
                if val not in field["options"] and field.get("type") == "choice":
                    scratch_keep[field["key"]] = field.get("current")
        self.settings_scratch = scratch_keep
        if self.settings_index >= len(fields):
            self.settings_index = 0

    def show_settings_form(self, config) -> dict:
        """设置页：↑↓ 移动 · ←→ 修改选项 · Tab 切标签 · Enter 保存并退出 · Esc 放弃"""
        from core import keyinput as _ki

        self.settings_mode = True
        self.settings_tab = 0
        self.settings_index = 0
        self._settings_scroll = 0
        self.settings_scratch = {}
        self.settings_provider_id = config.data.get("active_provider_id", "")
        self.settings_model_name = config.model_name
        self.settings_notice = "↑↓ 选择 · ←→ 修改 · Enter保存退出 · Esc放弃"
        self.settings_model_work = getattr(self, "settings_model_work", {}) or {}
        self._cancel_settings_refresh()
        self._build_settings_fields(config)
        self._settings_baseline = self._settings_snapshot(config)
        self._settings_esc_stage = 0
        self._settings_text_dirty = False
        self._last_frame_lines = []  # 进入设置时强制全量绘制
        try:
            _ki.flush_input()
        except Exception:
            pass
        self.render()
        ticks = 0
        while True:
            key = _read_key()
            if key is None:
                key = ("tick", "")
            kind, value = key
            if kind == "tick":
                ticks += 1
                # 空闲不退出；仅处理防抖
                if self._settings_text_dirty:
                    if self._flush_settings_paint():
                        self._settings_text_dirty = False
                        self._schedule_settings_refresh()
                    continue
                if self._settings_debounce_pending and time.time() >= self._settings_debounce_at:
                    self._settings_debounce_pending = False
                    self._build_settings_fields(config)
                    self._settings_paint_pending = False
                    self._last_frame_lines = []
                    self.render()
                elif self._flush_settings_paint():
                    pass
                continue
            if kind == "interrupt":
                if self._settings_is_dirty(config) and self._settings_esc_stage == 0:
                    self._settings_esc_stage = 1
                    self.settings_notice = "有未保存更改 · Enter保存退出 · Esc放弃"
                    self.render()
                    continue
                self.settings_mode = False
                return {}
            if kind == "escape":
                if self._settings_esc_stage == 1:
                    self.settings_mode = False
                    return {}
                if self._settings_is_dirty(config):
                    self._settings_esc_stage = 1
                    self.settings_notice = "有未保存更改 · Enter保存退出 · Esc放弃 · 其它键继续编辑"
                    self.render()
                    continue
                self.settings_mode = False
                return {}
            if self._settings_esc_stage == 1:
                if kind in ("submit", "submit_ctrl"):
                    updates = self._settings_apply(config)
                    self.settings_mode = False
                    self.bind_config(config)
                    return updates
                self._settings_esc_stage = 0
                self.settings_notice = "↑↓ 选择 · ←→ 修改 · Enter保存退出 · Esc放弃"
                # 继续处理当前键
            if kind == "mode_switch":
                continue
            if self._is_mode_switch(kind, value) and kind != "tab":
                continue
            if kind == "tab":
                if self._settings_text_dirty:
                    self._schedule_settings_refresh()
                self._settings_text_dirty = False
                self.settings_tab = (self.settings_tab + 1) % len(self.settings_tabs)
                self.settings_index = 0
                self._settings_scroll = 0
                self._immediate_settings_refresh(config)
                self._clamp_settings_index()
                self._last_frame_lines = []
                self.render()
                continue
            if kind in ("submit", "submit_ctrl"):
                self._clamp_settings_index()
                field = self.settings_fields[self.settings_index] if self.settings_fields else {}
                ftype = field.get("type")
                # 执行动作 / 文本编辑 / 开关
                if ftype == "action":
                    if field.get("key") == "save_provider":
                        msg = self._settings_save_provider(config)
                        self.settings_notice = msg
                        self._immediate_settings_refresh(config)
                        self._clamp_settings_index()
                        self._last_frame_lines = []
                        self.render()
                        continue
                    self._settings_run_action(config, field.get("key"))
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                    self._last_frame_lines = []
                    self.render()
                    continue
                if ftype == "text":
                    key_name = field.get("key")
                    label = field.get("label", key_name)
                    cur = self.settings_scratch.get(key_name, field.get("current"))
                    self.settings_mode = False
                    self._last_frame_lines = []
                    self.render()
                    try:
                        raw = input(f"{label} [{cur}]: ")
                    except (EOFError, KeyboardInterrupt):
                        raw = ""
                    self.settings_mode = True
                    if raw != "":
                        self.settings_scratch[key_name] = raw
                    self._settings_text_dirty = False
                    if key_name in self._MODEL_LIST_TEXT_KEYS:
                        self._immediate_settings_refresh(config)
                    else:
                        self._build_settings_fields(config)
                    self.settings_notice = f"{label} 已更新 · Enter 保存退出"
                    self._clamp_settings_index()
                    self._last_frame_lines = []
                    self.render()
                    continue
                if ftype == "bool":
                    cur = self.settings_scratch.get(field["key"], field.get("current"))
                    self.settings_scratch[field["key"]] = not bool(cur in (True, "true", "1", 1))
                    self.render()
                    continue
                # choice / 其它：Enter = 保存并退出设置
                updates = self._settings_apply(config)
                self.settings_mode = False
                self._cancel_settings_refresh()
                self.bind_config(config)
                self._last_frame_lines = []
                self.render()
                return updates
            dirn = _key_direction(kind, value)
            if dirn == "up":
                if self._settings_text_dirty:
                    self._schedule_settings_refresh()
                self._settings_text_dirty = False
                self._settings_move_selection(-1)
                self.render()
            elif dirn == "down":
                if self._settings_text_dirty:
                    self._schedule_settings_refresh()
                self._settings_text_dirty = False
                self._settings_move_selection(1)
                self.render()
            elif dirn == "left":
                self._settings_cycle_choice(-1, config)
                self._settings_text_dirty = False
                self.render()
            elif dirn == "right":
                self._settings_cycle_choice(1, config)
                self._settings_text_dirty = False
                self.render()
            elif kind == "backspace":
                field = self.settings_fields[self.settings_index] if self.settings_fields else {}
                if field.get("type") == "text":
                    val = list(str(self.settings_scratch.get(field["key"], "") or ""))
                    if val:
                        val.pop()
                    self.settings_scratch[field["key"]] = "".join(val)
                    self._settings_text_dirty = True
                    self._schedule_settings_paint(0.15)
            elif kind == "char" and value and all(ord(ch) >= 32 for ch in value):
                field = self.settings_fields[self.settings_index] if self.settings_fields else {}
                if field.get("type") == "choice" and value in ("\x1b",):
                    continue
            else:
                # 其它键（hotkey 等）仅重绘，避免异常退出
                self.render()

    def _clamp_settings_index(self) -> None:
        n = len(self.settings_fields or [])
        if n <= 0:
            self.settings_index = 0
            return
        if self.settings_index < 0:
            self.settings_index = 0
        if self.settings_index >= n:
            self.settings_index = n - 1

    def _settings_move_selection(self, direction: int) -> None:
        """↑↓ 移动选中项，跳过 sep 分隔行"""
        fields = self.settings_fields or []
        if not fields:
            self.settings_index = 0
            return
        n = len(fields)
        idx = self.settings_index
        for _ in range(n + 1):
            idx += direction
            if idx < 0:
                idx = 0
                break
            if idx >= n:
                idx = n - 1
                break
            if fields[idx].get("type") != "sep":
                break
        self.settings_index = max(0, min(n - 1, idx))

    def _settings_cycle_choice(self, direction: int, config) -> None:
        fields = self.settings_fields or []
        if not fields:
            return
        self._clamp_settings_index()
        idx = max(0, min(len(fields) - 1, self.settings_index))
        field = fields[idx]
        if not isinstance(field, dict) or "key" not in field:
            return
        ftype = field.get("type")
        key = field["key"]
        if ftype == "bool":
            cur = self.settings_scratch.get(key, field.get("current"))
            self.settings_scratch[key] = not bool(cur in (True, "true", "1", 1))
            return
        if ftype != "choice":
            return
        if field.get("type") == "sep":
            return
        options = list(field.get("options") or [])
        if not options:
            return
        try:
            cur = self.settings_scratch.get(key, field.get("current"))
            if cur not in options:
                idx = 0
            else:
                idx = options.index(cur)
            nxt = (idx + direction) % len(options)
            self.settings_scratch[key] = options[nxt]
        except Exception:
            self.settings_scratch[key] = options[0]
        # 选择提供商/模型后重建字段
        if key == "switch_provider":
            # 切换编辑目标提供商；“(新建)” 清空表单
            chosen = str(self.settings_scratch.get("switch_provider") or "")
            if chosen and chosen != "(新建)":
                self.settings_provider_id = chosen
                provider = config.find_provider(chosen) or {}
                s = self.settings_scratch
                s["provider_id"] = provider.get("provider_id", chosen)
                s["provider_name"] = provider.get("name", chosen)
                s["api_key"] = provider.get("api_key", "")
                s["base_url"] = provider.get("base_url", "")
                s["balance_url"] = provider.get("balance_url", "")
                s["provider_temperature"] = provider.get("temperature", 1.0)
                s["default_model_id"] = provider.get("default_model_id", "")
                s["provider_models_list"] = ",".join(
                    m.get("model_id") for m in provider.get("models") or []
                )
            elif chosen == "(新建)":
                self.settings_provider_id = ""
                s = self.settings_scratch
                s["provider_id"] = ""
                s["provider_name"] = ""
                s["api_key"] = ""
                s["base_url"] = "https://api.openai.com/v1"
                s["balance_url"] = ""
                s["provider_temperature"] = 1.0
                s["default_model_id"] = ""
                s["provider_models_list"] = "（暂无）"
            # 切换提供商属于明确操作，立即刷新列表
            self._immediate_settings_refresh(config)
        elif key == "select_provider":
            pids_models = []
            for row in config.list_models():
                if row["provider_id"] == self.settings_scratch[key]:
                    pids_models.append(row["model_name"])
            if pids_models:
                self.settings_scratch["select_model"] = pids_models[0]
                self.settings_model_name = pids_models[0]
            # 下拉切换提供商：立即刷新模型列表（非逐字输入）
            self._immediate_settings_refresh(config)
        elif key == "select_model":
            self.settings_model_name = self.settings_scratch[key]
            self._immediate_settings_refresh(config)
        elif key == "theme":
            self.load_theme(str(self.settings_scratch[key]))
        elif key == "mode":
            self.mode = str(self.settings_scratch[key])

    def _provider_live_models(self, config, pid: str) -> List[dict]:
        """提供商实时模型列表：config + 工作副本（模型页增改）"""
        by_id = {}
        provider = config.find_provider(pid) if pid else None
        if provider:
            for m in provider.get("models") or []:
                if m.get("model_id"):
                    by_id[m["model_id"]] = dict(m)
        for m in self.settings_model_work.get(pid, []) or []:
            mid = m.get("model_id")
            if mid:
                by_id[mid] = dict(m)
        return list(by_id.values())

    def _settings_save_provider(self, config) -> str:
        """提供商独立保存：diff 模型列表（无「附带新增」字段）"""
        s = self.settings_scratch
        pid = str(s.get("provider_id") or self.settings_provider_id or "").strip()
        if not pid:
            msg = "提供商ID为空，无法保存"
            self.settings_notice = msg
            return msg
        live_models = self._provider_live_models(config, pid)
        form = {
            "provider_id": pid,
            "name": s.get("provider_name") or pid,
            "api_key": s.get("api_key", ""),
            "base_url": s.get("base_url", ""),
            "balance_url": s.get("balance_url", ""),
            "temperature": s.get("provider_temperature", 1.0),
            "default_model_id": s.get("default_model_id", ""),
        }
        if form["default_model_id"] in ("（无模型）", "-", ""):
            form["default_model_id"] = live_models[0].get("model_id", "") if live_models else ""
        # 与保存前列表 diff
        result = config.save_provider_from_form(form, models_after=live_models)
        # 激活提供商与编辑目标一并写入
        self._settings_apply_active_provider(config)
        if str(s.get("active_provider_id") or ""):
            config.data["active_provider_id"] = str(s["active_provider_id"])
        config.apply_active()
        config.save()
        self.settings_provider_id = pid
        self.settings_model_work.pop(pid, None)
        provider = config.find_provider(pid) or {}
        s["provider_id"] = pid
        s["provider_name"] = provider.get("name", form["name"])
        s["api_key"] = provider.get("api_key", form["api_key"])
        s["base_url"] = provider.get("base_url", form["base_url"])
        s["balance_url"] = provider.get("balance_url", form["balance_url"])
        s["provider_temperature"] = provider.get("temperature", form["temperature"])
        live_ids = [m.get("model_id") for m in self._provider_live_models(config, pid)]
        s["default_model_id"] = provider.get("default_model_id") or (live_ids[0] if live_ids else "")
        s["provider_models_list"] = ",".join(live_ids) if live_ids else "（暂无）"
        s["active_provider_id"] = config.data.get("active_provider_id", s.get("active_provider_id", pid))
        msg = result.get("message") or "已保存提供商"
        act = s.get("active_provider_id")
        if act:
            msg += f" · 激活提供商:{act}"
        self.settings_notice = msg
        self._settings_baseline = self._settings_snapshot(config)
        return msg

    def _settings_run_action(self, config, action: str) -> None:
        """添加提供商/模型：占用输入行读入 id"""
        if action == "add_provider":
            self.settings_mode = False
            self.render()
            pid = input("新 provider_id: ").strip()
            self.settings_mode = True
            if pid:
                config.add_or_update_provider(
                    {
                        "provider_id": pid,
                        "name": pid,
                        "api_key": "",
                        "base_url": "https://api.openai.com/v1",
                        "models": [{"model_id": "default", "context_window": 65536, "max_tokens": 8192}],
                    }
                )
                self.settings_provider_id = pid
                self.settings_scratch["switch_provider"] = pid
        elif action == "add_model":
            pid = (
                self.settings_scratch.get("select_provider")
                or self.settings_provider_id
                or config.data.get("active_provider_id")
            )
            self.settings_mode = False
            self.render()
            mid = input(f"新 model_id ({pid}): ").strip()
            self.settings_mode = True
            if mid:
                entry = {
                    "model_id": mid,
                    "context_window": 65536,
                    "max_tokens": 8192,
                    "temperature": 1.0,
                    "modalities": ["text"],
                    "thinking_effort": "none",
                }
                # 写入工作副本，保存提供商时 diff；若提供商已存在也同步 config 便于模型页编辑
                work = self.settings_model_work.setdefault(pid, [])
                work[:] = [m for m in work if m.get("model_id") != mid]
                work.append(entry)
                if config.find_provider(pid):
                    config.add_model(pid, entry)
                self.settings_model_name = make_model_name(pid, mid)
                self.settings_scratch["select_model"] = self.settings_model_name
                self.settings_notice = f"模型 {mid} 已加入工作列表，保存提供商时生效"

    def _settings_apply(self, config) -> dict:
        """将三个标签页 scratch 写入 config 并 save"""
        s = self.settings_scratch
        # 提供商
        pid = s.get("provider_id") or self.settings_provider_id or config.data.get("active_provider_id")
        provider = {
            "provider_id": str(pid),
            "name": str(s.get("provider_name") or pid),
            "api_key": str(s.get("api_key", "")),
            "base_url": str(s.get("base_url", "")),
            "balance_url": str(s.get("balance_url", "")),
        }
        try:
            provider["temperature"] = float(s.get("provider_temperature", 1.0))
        except (TypeError, ValueError):
            provider["temperature"] = 1.0
        existing = config.find_provider(str(pid))
        if existing:
            provider["models"] = existing.get("models") or []
            provider["default_model_id"] = s.get("default_model_id") or existing.get("default_model_id")
        try:
            config.add_or_update_provider(provider)
        except Exception:
            pass
        # 激活提供商
        self._settings_apply_active_provider(config)
        config.apply_active()
        # 模型
        select_model = s.get("select_model") or self.settings_model_name
        row = config.find_model(select_model) if select_model else None
        if row:
            updates = {
                "model_id": str(s.get("model_id") or row.get("model_id")),
                "modalities": [x.strip() for x in str(s.get("modalities") or "text").split(",") if x.strip()],
                "thinking_effort": str(s.get("thinking_effort") or "none"),
            }
            try:
                updates["context_window"] = int(float(s.get("context_window", row.get("context_window", 0))))
                updates["max_tokens"] = int(float(s.get("max_tokens", row.get("max_tokens", 0))))
                updates["temperature"] = float(s.get("model_temperature", row.get("temperature", 1.0)))
            except (TypeError, ValueError):
                pass
            config.update_model(row["provider_id"], row["model_id"], updates)
            config.data["active_provider_id"] = row["provider_id"]
            config.data["active_model_id"] = updates.get("model_id", row["model_id"])
        # 任务模型
        if s.get("task_plan"):
            config.set_task_model("plan", s["task_plan"])
        if s.get("task_code"):
            config.set_task_model("code", s["task_code"])
        if s.get("task_review"):
            config.set_task_model("review", s["task_review"])
        # 系统
        updates = {}
        if s.get("active_model_name"):
            config.switch_model(s["active_model_name"])
        ui_cfg = config.data.setdefault("ui", {})
        # 快捷键：scratch 中 key_* 写入 ui 与 self.keys
        for scratch_key, val in s.items():
            if not str(scratch_key).startswith("key_"):
                continue
            shortcut = str(scratch_key)[4:]
            if val:
                ui_cfg[shortcut] = str(val).lower()
                self.keys[shortcut] = str(val).lower()
        if "theme" in s:
            ui_cfg["theme"] = str(s["theme"])
        if "mode" in s:
            ui_cfg["mode"] = str(s["mode"])
        if "logo" in s:
            ui_cfg["logo"] = bool(s["logo"] in (True, "true", "1", 1) if isinstance(s["logo"], str) else bool(s["logo"]))
        for key, cast in (
            ("font_size", int),
            ("tip_interval", float),
            ("scroll_v0", float),
            ("scroll_hold_ms", int),
            ("scroll_max_step", int),
            ("settings_debounce_ms", float),
        ):
            if key in s and s[key] is not None and str(s[key]) != "":
                try:
                    ui_cfg[key] = cast(float(s[key]))
                except (TypeError, ValueError):
                    pass
        if "settings_debounce_ms" in ui_cfg:
            try:
                self.settings_debounce_sec = max(0.05, float(ui_cfg["settings_debounce_ms"]) / 1000.0)
            except (TypeError, ValueError):
                pass
        if "font_size" in ui_cfg:
            config.data.setdefault("system", {})["font_size"] = ui_cfg["font_size"]
        llm_cfg = config.data.setdefault("llm", {})
        if "retry_times" in s:
            try:
                llm_cfg["retry_times"] = int(float(s["retry_times"]))
            except (TypeError, ValueError):
                pass
        if "retry_delay" in s:
            try:
                llm_cfg["retry_delay"] = float(s["retry_delay"])
            except (TypeError, ValueError):
                pass
        mem_cfg = config.data.setdefault("memory", {})
        if "auto_compress" in s:
            mem_cfg["auto_compress"] = bool(s["auto_compress"] in (True, "true", "1", 1) if isinstance(s["auto_compress"], str) else bool(s["auto_compress"]))
        if "compress_threshold" in s:
            try:
                mem_cfg["compress_threshold"] = float(s["compress_threshold"])
            except (TypeError, ValueError):
                pass
        config.apply_active()
        config.save()
        updates["model_name"] = config.model_name
        updates["task_models"] = config.task_models()
        return updates

    def set_commands_source(self, *args, **kwargs) -> None:
        return None


def _key_direction(kind: str, value: str) -> Optional[str]:
    """归一化方向键事件：kind=up… 或 hotkey=ctrl+up 均映射为 up/down/left/right。"""
    from core import keyinput

    return keyinput.direction_of(kind, value)


def _read_key():
    """输入统一走 core.keyinput"""
    from core import keyinput

    return keyinput.read_key_event()


def _read_events():
    """批处理读取：一次抽干输入队列返回事件列表（空列表=本轮无键）"""
    from core import keyinput

    return keyinput.read_events()


def _read_key_windows():
    from core import keyinput

    return keyinput.read_key_event()


def _read_key_posix():
    from core import keyinput

    return keyinput.read_key_event()


def get_app() -> TuiApp:
    global _app
    if _app is None:
        _app = TuiApp()
    return _app


def chatbox(messages=None, task="", plan=None, steps=None, status="") -> None:
    """会话区刷新（UI 不提供外部 API）"""
    app = get_app()
    if messages is not None:
        app.messages = list(messages)
    if plan is not None:
        app.plan = dict(plan)
    if steps is not None:
        app.steps = list(steps)
    if task:
        app.task = task
    if status:
        app.status = status
    app.render()


def input_box(prompt: str = "> ", config=None) -> str:
    app = get_app()
    return app.read_line(prompt, config=config)


def settings(config) -> dict:
    return get_app().show_settings_form(config)
