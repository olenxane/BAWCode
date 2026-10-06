#此文件为ui的渲染交互脚本：无边框分区、主题、确认流程、token状态
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

log = get_logger("ui")

# UI 请求桥的取消哨兵：复用 llm 的常量，桥等待被取消时回填给 agent 线程
from core.llm import CANCELLED as CANCEL_RESULT

_CONSOLE = Console(soft_wrap=True, force_terminal=True) if HAS_RICH else None
_app: Optional["TuiApp"] = None
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# 会话树收起渲染：消息类节点最多显示的视觉行数，超出部分折叠并附「… (+N 行)」指示行
_TREE_INLINE_CAP = 3
# 参与 3 行折叠的消息类节点；assistant/stream_* 全文显示，plan/help 走子节点机制
_TREE_FOLD_KINDS = {"user", "tool", "system", "system_prompt"}
_STATUS_ICON = {"pending": "○", "running": "◐", "done": "●", "failed": "✗"}
# 树底阶段提示转轮：盲文方点阵帧序（cli-spinners "dots" 同款，80ms/帧）
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# 「思考中」文案池：树底提示每 4s 轮换一句（按时间槽取值，帧循环无状态、线程安全）
_THINKING_PHRASES = (
    "思考中", "推敲中", "琢磨中", "构思中", "盘算中", "酝酿中", "权衡方案",
    "梳理思路", "整理上下文", "排查疑点", "串联线索", "打腹稿", "翻找思路", "灵感加载中",
)
_HINT_ROTATE_SECONDS = 4.0
# 树底阶段文案池：key=「 · 」前的阶段基名，值=按时间槽轮换的文案元组（帧循环无状态、线程安全）；
# 带后缀的阶段（如「工具调用中 · read」）轮换基名文案、保留后缀；未登记的阶段按原样显示
_PHASE_POOLS = {
    "思考中": _THINKING_PHRASES,
    "工具调用中": ("调用工具中", "等待工具返回", "处理工具输出"),
    "待确认": ("等待确认", "等待你的决定"),
}
# 控制台鼠标 y → compose 行号偏移（rich Live 主屏模式 1:1；真机如有固定偏差在此校准）
_MOUSE_Y_OFFSET = 0

def _command_head(command: str, limit: int = 24) -> str:
    """命令头提取：取前两个 token，clip 到 limit 显示宽——供树节点显示简洁命令"""
    parts = str(command or "").strip().split()
    if not parts:
        return ""
    return _clip(" ".join(parts[:2]), limit)


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
    # 会话消息配色：角色徽标用饱和色加粗，正文由角色色混合 ink 生成浅色变体
    "user_msg": (70, 184, 178),      # 46b8b2 明青
    "agent": (143, 191, 106),        # 8fbf6a 新叶绿
    "thinking": (79, 163, 159),      # 4fa39f 灰青
    "tool_title": (20, 186, 188),    # 14babc
    "tool_content": (255, 255, 255), # 纯白
    "subagent": (240, 156, 88),      # f09c58 子代理节点/直播（暖橙，与主对话区分）
    "mcp": (106, 204, 132),          # 6acc84 MCP 状态行（外接服务器，冷绿示连通）
    "tree_guide": (58, 85, 120),     # 3a5578 会话树引导线（│）
    "selection_bg": (42, 82, 134),   # 2a5286 自绘选区背景（比光标高亮略深一档）
}

TIPS = [
    "界面输入框：Enter 提交 · Shift+Enter 换行 · Tab 补全/切焦点",
    "Shift+Tab 或 /mode 切换访问模式",
    "/model 切换模型 · /settings 打开设置",
    "树模式 Ctrl+Z 回退到选中节点 · 再按取消",
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
    cols = os.environ.get("COLUMNS") or os.environ.get("BAW_COLS")
    rows = os.environ.get("LINES") or os.environ.get("BAW_LINES")
    if cols and rows:
        return max(70, int(cols)), max(22, int(rows))
    size = shutil.get_terminal_size(fallback=(100, 30))
    return max(70, size.columns), max(22, size.lines)


def _char_width(ch: str) -> int:
    if not ch:
        return 0
    w = wcwidth(ch)
    # wcwidth 对控制字符返回 -1，按 1 列记（与终端占位一致的保守值）
    return w if w >= 0 else 1


def _display_width(text: str) -> int:
    return sum(_char_width(ch) for ch in _ANSI_RE.sub("", text or ""))


def split_at_cells(text: str, cut: int) -> tuple:
    """按显示列宽切开；cut 为左段应占单元格数。宽字符不折半。"""
    if cut <= 0:
        return "", text or ""
    src = text or ""
    used = 0
    for i, ch in enumerate(src):
        w = _char_width(ch)
        if used + w > cut:
            return src[:i], src[i:]
        used += w
        if used == cut:
            return src[: i + 1], src[i + 1 :]
    return src, ""


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


def _mix(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    """c1 向 c2 混合 t（0~1）：由角色饱和色生成浅色正文变体"""
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def _copy_to_clipboard(text: str) -> bool:
    """CF_UNICODETEXT 写入系统剪贴板（win32；失败静默返回 False 不打断渲染）"""
    if sys.platform != "win32" or not text:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 显式原型：64 位下 windll 默认把句柄返回值截成 32 位 c_int，
        # GlobalAlloc 的 HGLOBAL 高位被砍会导致 GlobalLock 拿野句柄返回 NULL
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        buf = text.encode("utf-16-le") + b"\x00\x00"
        opened = False
        for _ in range(6):  # 剪贴板是共享资源，被短暂锁住时按惯例重试
            if user32.OpenClipboard(0):
                opened = True
                break
            time.sleep(0.03)
        if not opened:
            return False
        try:
            if not user32.EmptyClipboard():
                return False
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(buf))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(ptr, buf, len(buf))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)  # 失败时回收；成功后句柄归系统
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False

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


def _scrollbar_geometry(total: int, tree_h: int, scroll: int) -> Tuple[int, int]:
    """滚动条滑块几何：(thumb_h, thumb_top)。"""
    max_scroll = max(1, total - tree_h)
    thumb_h = max(1, round(tree_h * tree_h / total))
    thumb_top = round(max(0, min(scroll, max_scroll)) * (tree_h - thumb_h) / max_scroll)
    return thumb_h, min(thumb_top, tree_h - thumb_h)


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


def _wrap_line(text: str, width: int) -> List[str]:
    """单条逻辑行按显示宽度贪心折断；宽字符不折半。"""
    if width <= 0:
        return [""]
    if not text:
        return [""]
    lines: List[str] = []
    cur, used = "", 0
    for ch in text:
        w = _char_width(ch)
        if w > width:
            # 单字就超宽：单独成行
            if cur:
                lines.append(cur)
            lines.append(ch)
            cur, used = "", 0
            continue
        if used + w > width:
            lines.append(cur)
            cur, used = ch, w
        else:
            cur += ch
            used += w
    lines.append(cur)
    return lines or [""]


class Layout:
    """多行输入布局：按逻辑行缓存 visual 行，编辑只重折脏行（宽度来自 wcwidth）。"""

    __slots__ = ("width", "_paras", "_visuals", "_dirty")

    def __init__(self, text: str = "", width: int = 40) -> None:
        self.width = max(1, width)
        self._paras: List[str] = (text or "").split("\n")
        self._visuals: List[List[str]] = []
        self._dirty: set = set(range(len(self._paras)))
        self._reflow_dirty()

    def set_width(self, width: int) -> None:
        width = max(1, width)
        if width == self.width:
            return
        self.width = width
        self._dirty = set(range(len(self._paras)))
        self._reflow_dirty()

    def set_text(self, text: str) -> None:
        self._paras = (text or "").split("\n")
        self._dirty = set(range(len(self._paras)))
        self._visuals = []
        self._reflow_dirty()

    def apply_edit(self, text: str, cursor: int) -> None:
        """用完整文本 + 光标做增量：仅重折内容变化的逻辑行。"""
        new = (text or "").split("\n")
        # 对齐前缀
        i = 0
        while i < len(self._paras) and i < len(new) and self._paras[i] == new[i]:
            i += 1
        # 对齐后缀
        j = 0
        while (
            j < len(self._paras) - i
            and j < len(new) - i
            and self._paras[len(self._paras) - 1 - j] == new[len(new) - 1 - j]
        ):
            j += 1
        old_mid_end = len(self._paras) - j
        new_mid_end = len(new) - j
        self._dirty = set(range(i, max(i, new_mid_end)))
        # visuals 对齐切片
        head = self._visuals[:i] if i <= len(self._visuals) else []
        tail = self._visuals[old_mid_end:] if old_mid_end <= len(self._visuals) else []
        mid_slots = max(0, new_mid_end - i)
        self._paras = new
        self._visuals = head + [[] for _ in range(mid_slots)] + tail
        # 长度校正
        if len(self._visuals) != len(self._paras):
            self._dirty = set(range(len(self._paras)))
            self._visuals = [[] for _ in self._paras]
        self._reflow_dirty()
        _ = cursor

    def _reflow_dirty(self) -> None:
        if len(self._visuals) != len(self._paras):
            self._visuals = [[] for _ in self._paras]
            self._dirty = set(range(len(self._paras)))
        for i in list(self._dirty):
            if 0 <= i < len(self._paras):
                self._visuals[i] = _wrap_line(self._paras[i], self.width)
        self._dirty.clear()

    def visual_rows(self) -> List[str]:
        rows: List[str] = []
        for vs in self._visuals:
            rows.extend(vs if vs else [""])
        return rows or [""]

    def cursor_visual(self, text: str, cursor: int) -> Tuple[int, int]:
        """返回 (visual_row, cells_before_cursor)。"""
        text = text or ""
        cursor = max(0, min(len(text), cursor))
        before = text[:cursor]
        # 光标所在逻辑行序号与行内偏移
        para_index = before.count("\n")
        last_nl = before.rfind("\n")
        col_in_para = cursor - (last_nl + 1)
        para = self._paras[para_index] if para_index < len(self._paras) else ""
        prefix = para[:col_in_para]
        wrapped = self._visuals[para_index] if para_index < len(self._visuals) else _wrap_line(para, self.width)
        if not wrapped:
            wrapped = [""]
        # 重建前缀折行以定位（仅短前缀）
        if prefix:
            pre_wrapped = _wrap_line(prefix, self.width)
            row_in_para = len(pre_wrapped) - 1
            cells = _display_width(pre_wrapped[-1]) if pre_wrapped else 0
        else:
            row_in_para = 0
            cells = 0
        # 绝对 visual 行 = 前面逻辑行的 visual 行数 + row_in_para
        visual_row = 0
        for k in range(para_index):
            vs = self._visuals[k] if k < len(self._visuals) else [""]
            visual_row += max(1, len(vs))
        visual_row += row_in_para
        # 校正：_wrap_line 与 pre_wrapped 在恰好贴边时可能差一行
        if wrapped and row_in_para >= len(wrapped):
            row_in_para = len(wrapped) - 1
            visual_row = sum(max(1, len(self._visuals[k] if k < len(self._visuals) else [""])) for k in range(para_index)) + row_in_para
            cells = _display_width(prefix) - _display_width("".join(wrapped[:row_in_para]))
            cells = max(0, cells)
        return visual_row, cells


def _oneline(text: str, limit: int = 80) -> str:
    plain = _ANSI_RE.sub("", str(text or "")).replace("\n", " ")
    return plain if len(plain) <= limit else plain[:limit] + "…"


def _git_branch() -> str:
    # 项目即进程启动目录（与 ensure_project_identity(Path.cwd()) 同源）
    try:
        head = Path.cwd() / ".git" / "HEAD"
        if head.exists():
            ref = head.read_text(encoding="utf-8", errors="replace").strip()
            if ref.startswith("ref:"):
                return ref.split("/")[-1] or "main"
            return ref[:7] if ref else "main"
    except OSError:
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


class _LlmRetryGate:
    """LLM 错误手动重试闸：agent 线程在 wait 上阻塞，主线程按键/插件置位放行"""

    __slots__ = ("error", "retry", "give_up")

    def __init__(self, error: str):
        self.error = error
        self.retry = threading.Event()
        self.give_up = threading.Event()


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
        self.settings_mode = False
        self.settings_fields: List[dict] = []
        self.settings_index = 0
        self.settings_scratch: Dict[str, Any] = {}
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
        # 树回退（Ctrl+Z）的取消暂存：{"cut","messages","session_id"}；末条 _rollback_note 标记配合校验
        self._conv_stash: Optional[dict] = None
        self.logo_enabled = True
        self.settings_tab = 0
        self.settings_tabs = ["提供商", "模型", "系统", "快捷键", "插件"]
        self._plugin_expanded: set = set()  # 设置页插件行展开的插件 id 集合
        self.settings_provider_id = ""
        self.settings_model_name = ""
        self._settings_scroll = 0
        self.settings_notice = ""
        self.settings_model_work: Dict[str, List[dict]] = {}
        self._settings_baseline: Dict[str, Any] = {}
        self._settings_esc_stage = 0
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
        self._live: Optional["Live"] = None
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
        # 配色已变：会话树行缓存失效（缓存 key 不含主题，否则残留旧配色直到内容变化）
        self._tree_rows_key = None
        self._tree_sig = None
        self.keys["tip_interval"] = self.theme.get("tips_rotate_seconds", 5)

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
        """进入 TUI：rich Live 接管渲染（TMP app.py:1584 同构）+ 终端模式初始化。

        - Live(screen=False, auto_refresh=False)：主屏 + 应用驱动刷新，
          与 music player 的 Live 用法完全一致——每帧 live.update(refresh=True)，
          rich 负责光标回卷与整帧输出（LiveRender 无 diff，每帧真实写字节），
          应用不再手拼 ANSI/diff/节流。
        - 不开 ?2004h bracketed-paste：conhost 上开启后 TSF IME 的删除序列
          （孤立 \x00 握手字节之后的 \x1e\x08）会被扣押到下一次按键才放行，
          表现为"退格删不掉已上屏的中文，再打中文才一起刷新"
          （minimal_repro 二分：四个开 2004 的模式全卡，唯一不卡的 tmplayer
          模式无 2004；参照项目 TMP 也不开——这是两进程间唯一的控制台状态差）。
          粘贴改走 keyinput 的 conhost 启发式（批次中部回车/Tab → paste 事件）。
          调试可设 BAW_BRACKETED_PASTE=1 强制开启，=0 全平台强制关闭。
        - 隐藏光标交给 Live（console.show_cursor）。
        """
        _enable_windows_ansi()
        if self._entered:
            return
        log.info("进入 TUI 界面")
        # bracketed-paste 默认关闭（见 docstring）；终端模式直接写 stdout
        _bp = os.environ.get("BAW_BRACKETED_PASTE", "")
        _paste_on = _bp == "1" or (_bp == "" and sys.platform != "win32")
        # 鼠标：1000h 普通 + 1002h 按钮事件 + 1006h SGR 编码。
        # WT/ConPTY 实测（2026-09-25 探针）：仅 1000h+1006h 时终端不转发滚轮
        # （滚轮被 WT 自己消费），加 1002h 后以 MOUSE_EVENT 记录到达。
        # 开启鼠标捕获后，终端内文本选择需 Shift+拖拽。关闭用 BAW_MOUSE=0
        _mouse_on = os.environ.get("BAW_MOUSE", "1") != "0"
        _mouse_seq = "\033[?1000h\033[?1002h\033[?1006h" if _mouse_on else ""
        sys.stdout.write("\033[2J\033[H" + ("\033[?2004h" if _paste_on else "") + _mouse_seq)
        sys.stdout.flush()
        if HAS_RICH and _CONSOLE is not None:
            self._live = Live(
                console=_CONSOLE,
                screen=False,        # 主屏（TMP 同款；备用屏有 TSF IME 失同步问题）
                auto_refresh=False,  # 应用每帧驱动刷新（TMP 同款）
            )
            self._live.start()
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

    def _clamp_tree_scroll(self, total: int, tree_h: int) -> int:
        """按 follow_tail / tree_cursor 计算并夹紧会话树 scroll"""
        max_scroll = max(0, total - tree_h)
        if total <= tree_h:
            self.scroll = 0
            return 0
        if self._tree_follow_tail:
            self.scroll = max_scroll
        else:
            # 仅在会话树焦点下保证光标节点可见；输入焦点时不要把 scroll 拉回 tree_cursor
            if self.focus == "tree":
                self._ensure_cursor_visible(tree_h)
            self.scroll = max(0, min(self.scroll, max_scroll))
        return self.scroll

    def _ensure_cursor_visible(self, tree_h: int) -> None:
        """把树光标所在节点的标题行滚进视口。

        光标是节点号、scroll 是渲染行号，经 _node_spans 换算；
        仅在标题行不可见时最小滚动（节点高于视口时顶对齐，否则贴底露出标题），
        不强求整个节点入窗——避免与滚轮/翻页互相抢滚动位置。"""
        spans = self._node_spans
        if spans and 0 <= self.tree_cursor < len(spans):
            first, count = spans[self.tree_cursor]
            if first < self.scroll:
                self.scroll = first
            elif first >= self.scroll + tree_h:
                self.scroll = first if count >= tree_h else max(0, first + count - tree_h)
        else:
            # 行区间缺失（尚无渲染缓存）时按节点号近似兜底
            if self.tree_cursor < self.scroll:
                self.scroll = self.tree_cursor
            elif self.tree_cursor >= self.scroll + tree_h:
                self.scroll = self.tree_cursor - tree_h + 1

    def _node_at_row(self, row: int) -> int:
        """渲染行号 → 树节点号（经 _node_spans；越界夹到端点）"""
        spans = self._node_spans
        if not spans:
            return 0
        for i, (first, count) in enumerate(spans):
            if first <= row < first + count:
                return i
        return len(spans) - 1 if row >= 0 else 0

    def _has_conversation_content(self) -> bool:
        """会话区是否已有需要展示的内容（有则隐藏 Logo）"""
        for m in self.messages:
            role = m.get("role")
            mtype = m.get("type") or ""
            content = memory_mod.content_text(m.get("content")).strip()
            if not content:
                continue
            if role in ("user", "assistant", "tool"):
                return True
            if mtype in ("task", "plan", "refine", "diff", "code"):
                return True
            if role == "system" and mtype not in ("", None) and content:
                # 帮助/命令结果等也占用会话区
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
        # 根节点标题：取「首次」用户消息，避免被新一轮输入替换掉
        first_user = ""
        for m in self.messages:
            first_content = memory_mod.content_text(m.get("content"))
            if m.get("role") == "user" and first_content.strip():
                first_user = first_content
                break
        if first_user:
            root_label = f"任务 · {_oneline(first_user, 40)}"
        else:
            root_label = f"任务 · {_oneline(self.task or '（新会话）', 40)}"
        task_node = TreeNode("task", root_label, "task", default_expanded=True)
        roots.append(task_node)

        # 消息按对话轮次挂在树上：每条用户消息都是独立节点（不替换根标题）。
        # 计划/步骤不再用置顶/置底状态单节点：四件套工具消息与工作流快照消息自带
        # JSON 快照，在时间线位置增量渲染（冻结），旧节点不随状态原地突变
        current_turn: Optional[TreeNode] = None
        rendered_plan = False
        rendered_steps = False

        def _parent() -> TreeNode:
            return current_turn if current_turn is not None else task_node

        # 命令头索引：从 tool_call 信封提取 execute_command/run_program 的简洁命令
        #（结果消息不带参数，按 tool_call_id 关联到调用），供结果单行节点与信封节点显示
        cmd_heads: Dict[str, str] = {}
        for item in self.messages[-200:]:
            if item.get("type") != "tool_call":
                continue
            for c in item.get("tool_calls") or []:
                if not isinstance(c, dict):
                    continue
                args = c.get("arguments") or {}
                head = ""
                if c.get("name") == "execute_command":
                    head = _command_head(memory_mod.content_text(args.get("command")))
                elif c.get("name") == "run_program":
                    rest = " ".join(str(a) for a in (args.get("args") or [])[:2])
                    head = _command_head((str(args.get("program") or "") + " " + rest).strip())
                cid = str(c.get("id") or "")
                if cid and head:
                    cmd_heads[cid] = head

        for index, item in enumerate(self.messages[-200:]):
            role = item.get("role")
            content = memory_mod.content_text(item.get("content"))
            n_imgs = memory_mod.count_image_parts(item.get("content"))
            image_note = f" 🖼×{n_imgs}" if n_imgs else ""
            msg_type = item.get("type") or ""
            tool_name = item.get("tool_name") or ""
            pending_extra: List[TreeNode] = []  # type=plan 快照等附加节点（排在 node 之后）
            if role == "user":
                # 正文写入节点 label 折行展示；超 3 行默认收起，树内手动展开
                node = TreeNode(
                    f"msg:{index}",
                    f"用户 · {content}",
                    "user",
                    default_expanded=False,
                    detail="",
                )
                task_node.children.append(node)
                current_turn = node
                continue
            if role == "tool" or msg_type == "tool":
                # 计划四件套：结果消息自带 JSON 快照 → 冻结的时间线节点（计划/步骤/步骤更新）
                node = self._plan_step_node(index, tool_name, content)
                if node is not None:
                    if node.kind == "plan":
                        rendered_plan = True
                    elif node.kind == "steps":
                        rendered_steps = True
                else:
                    failed = tool_failure_hint(content)
                    icon = "❌" if failed else "✅"
                    # 结果正文 trim 后再拼（纯空白结果不挂悬空「 · 」）；内部换行保留供展开查看
                    result_text = content.strip()
                    # execute_command/run_program 显示提取的命令头（无索引回落工具名）
                    display = cmd_heads.get(str(item.get("tool_call_id") or "")) or tool_name or "tool"
                    label = f"⚙ {display} {icon}{image_note}"
                    if result_text:
                        label = f"{label} · {result_text}"
                    node = TreeNode(
                        f"msg:{index}",
                        label,
                        "tool",
                        default_expanded=False,
                        detail="",
                    )
            elif msg_type == "system_prompt":
                files = item.get("files") or []
                label = "注入系统提示词"
                if files:
                    label += " · " + ", ".join(str(x) for x in files)
                node = TreeNode(
                    f"msg:{index}",
                    f"⚙ {label}",
                    "system_prompt",
                    default_expanded=False,
                    detail="",
                )
            elif msg_type == "workflow_node":
                # 工作流节点标记（run_workflow 在每个节点执行前落库）：
                # 该轮用户消息下按时间线显示节点推进，最后一条即当前节点
                nid = str(item.get("node_id") or "")
                ntype = str(item.get("node_type") or "")
                label = f"⚙ 节点 · {content.strip()}"
                if nid and nid != ntype:
                    label += f" · {nid}"
                node = TreeNode(
                    f"msg:{index}",
                    label,
                    "workflow_node",
                    default_expanded=False,
                    detail="",
                )
            elif msg_type == "tool_call":
                names = []
                for c in item.get("tool_calls") or []:
                    if isinstance(c, dict):
                        # execute_command/run_program 优先显示命令头
                        names.append(cmd_heads.get(str(c.get("id") or "")) or str(c.get("name") or ""))
                # 模型随工具调用输出的说明文字是给用户看的话：独立成 assistant 节点
                # 全文展示；不并进可折叠的工具节点（否则默认被折叠藏住，且部分模型
                # 的 content 带 \n\n 包裹，展开后标题行悬空「 · 」+ 空行）
                text = content.strip()
                if text:
                    _parent().children.append(
                        TreeNode(
                            f"msg:{index}",
                            f"Agent · {text}",
                            "assistant",
                            default_expanded=True,
                            detail="",
                        )
                    )
                node = TreeNode(
                    f"call:{index}",
                    f"⚙ {','.join(names) if names else '工具调用'}",
                    "tool",
                    default_expanded=False,
                    detail="",
                )
            elif msg_type == "subagent":
                # 子代理记录节点：收起=一行状态，展开=完整工作上下文（trace 逐条子节点）；
                # 子节点 id 用 subagent_id 派生（稳定，规避 /resume 后 msg index 漂移）
                sid = str(item.get("subagent_id") or "?")
                label = (
                    f"子代理 #{sid} · {item.get('subagent_role') or '?'}"
                    f" · {subagent_mod.status_label(str(item.get('status') or ''))}"
                )
                node = TreeNode(
                    f"sub:{sid}",
                    label,
                    "subagent",
                    default_expanded=False,
                    summary=_oneline(item.get("task") or "", 40),
                )
                for i, tm in enumerate(item.get("trace") or []):
                    if not isinstance(tm, dict):
                        continue
                    t_role = tm.get("role")
                    t_content = memory_mod.content_text(tm.get("content"))
                    # trace 正文同样可能是模型给用户的说明：strip 后拆分渲染，
                    # 与主对话 tool_call 分支同规则（避免折叠藏正文 / 悬空分隔符）
                    t_text = t_content.strip()
                    if t_role == "assistant" and tm.get("tool_calls"):
                        names = ",".join(
                            str(c.get("name") or "") for c in tm["tool_calls"] if isinstance(c, dict)
                        )
                        if t_text:
                            node.children.append(
                                TreeNode(f"sub:{sid}:{i}", f"Agent · {t_text}", "assistant")
                            )
                        child = TreeNode(
                            f"sub:{sid}:{i}:call" if t_text else f"sub:{sid}:{i}",
                            f"⚙ {names or 'assistant'}",
                            "tool",
                            default_expanded=False,
                        )
                    elif t_role == "tool" or tm.get("type") == "tool":
                        icon = "❌" if tool_failure_hint(t_content) else "✅"
                        label = f"⚙ {tm.get('tool_name') or 'tool'} {icon}"
                        if t_text:
                            label = f"{label} · {t_text}"
                        child = TreeNode(
                            f"sub:{sid}:{i}", label, "tool", default_expanded=False
                        )
                    elif t_role == "assistant":
                        if not t_text:
                            continue  # 空正文条目不产出悬空「Agent · 」行
                        child = TreeNode(f"sub:{sid}:{i}", f"Agent · {t_text}", "assistant")
                    else:
                        if not t_text:
                            continue
                        child = TreeNode(
                            f"sub:{sid}:{i}", f"指令 · {t_text}", "user", default_expanded=False
                        )
                    node.children.append(child)
            elif role == "assistant":
                # 完整回复写入 label，由 _tree_rows 折行；LLM 输出不限行数、无折叠指示
                # 不建子节点、不开独立详情区
                node = TreeNode(
                    f"msg:{index}",
                    f"Agent · {content}",
                    "assistant",
                    default_expanded=True,
                    detail="",
                )
                if msg_type == "plan":
                    # 工作流 plan 节点收尾快照（_exec_plan 附带最终版 plan/steps JSON）：
                    # 计划/步骤节点随消息落位、排在文本节点之后（经 pending_extra）
                    for extra in self._snapshot_nodes(index, item):
                        pending_extra.append(extra)
                        if extra.kind == "plan":
                            rendered_plan = True
                        elif extra.kind == "steps":
                            rendered_steps = True
            elif msg_type == "help":
                # 帮助/命令反馈：≤3 行直接可见，更长默认收起、树内手动展开
                node = TreeNode(
                    f"msg:{index}",
                    "帮助",
                    "help",
                    default_expanded=len(content.splitlines()) <= 3,
                    detail=content,
                )
                for i, line in enumerate(content.splitlines()):
                    # 不预截断：长行（如含密钥链接）由 _tree_rows 按实时宽度折行展示
                    node.children.append(TreeNode(f"msg:{index}:{i}", line, "help_line"))
            else:
                node = TreeNode(
                    f"msg:{index}",
                    f"系统 · {content}",
                    "system",
                    default_expanded=False,
                    detail="",
                )
            _parent().children.append(node)
            for extra in pending_extra:
                _parent().children.append(extra)

        # 旧会话回落：消息时间线没渲染出计划/步骤节点且状态非空（历史会话无快照消息）
        # 时，按当前状态在任务根尾部补节点（仅兼容显示；新会话均走消息快照）
        plan = self.plan or {}
        if not rendered_plan and (
            plan.get("status") not in ("empty", "", None) or plan.get("content") or plan.get("title")
        ):
            task_node.children.append(self._plan_node_from("fallback:plan", plan))
        if not rendered_steps and self.steps:
            task_node.children.append(self._steps_node_from("fallback:steps", self.steps))

        # 流式树尾直播（纯 UI 状态，不在 session.messages 里）：思考过程折叠为尾部窗口，正文全文折行
        streaming = self.streaming_msg if isinstance(self.streaming_msg, dict) else None
        if streaming is not None:
            thinking = str(streaming.get("thinking") or "")
            content = str(streaming.get("content") or "")
            if thinking:
                _parent().children.append(
                    TreeNode("stream:thinking", thinking, "stream_thinking", default_expanded=True, detail="")
                )
            if content:
                _parent().children.append(
                    TreeNode("stream:content", content, "stream_content", default_expanded=True, detail="")
                )
        # 子代理直播（纯 UI 状态，不进 session.messages）：头部状态 + 最近输出尾部窗口 + 工具行
        live = self.subagent_stream if isinstance(self.subagent_stream, dict) else None
        if live is not None:
            text = str(live.get("text") or "")
            tail = [ln for ln in text.splitlines() if ln.strip()][-3:]
            label = f"子代理 #{live.get('id') or '?'} · {live.get('role') or ''} · {live.get('phase') or ''}"
            if tail:
                label += "\n" + "\n".join(tail)
            live_node = TreeNode("subagent:live", label, "subagent_live", default_expanded=True)
            for i, tline in enumerate(live.get("tools") or []):
                live_node.children.append(TreeNode(f"subagent:live:t{i}", str(tline), "tool"))
            _parent().children.append(live_node)
        # MCP 状态行（进程级常驻，core/mcp.py 后台线程维护；未启用/无配置时为空串不渲染）
        mcp_text = self._mcp_line()
        if mcp_text:
            _parent().children.append(TreeNode("mcp:status", mcp_text, "mcp", default_expanded=True))
        return roots

    def _plan_node_from(self, id_base: str, data: dict) -> TreeNode:
        """计划快照 dict → 节点（kind plan + plan_line 子行）；id_base 须全局唯一"""
        status = str(data.get("status") or "")
        title = str(data.get("title") or "")
        body = str(data.get("content") or "")
        node = TreeNode(
            id_base,
            f"计划 [{status or 'empty'}] {title}",
            "plan",
            default_expanded=status in ("draft", "confirmed", "executing", "running"),
            summary=_oneline(body, 40),
            detail=body,
        )
        if node.detail:
            # 计划不限行数：全部子行展示，不截断
            for i, line in enumerate(_wrap(node.detail, 50)):
                node.children.append(TreeNode(f"{id_base}:p{i}", line, "plan_line"))
        return node

    def _steps_node_from(self, id_base: str, steps: List[dict]) -> TreeNode:
        """步骤清单快照 list → 节点（kind steps + step 子节点，done 折叠沿用 collapse_done）"""
        done = sum(1 for s in steps if isinstance(s, dict) and s.get("status") == "done")
        node = TreeNode(id_base, f"步骤 {done}/{len(steps)}", "steps")
        for step in steps:
            st = str(step.get("status") or "pending")
            icon = _STATUS_ICON.get(st, "○")
            detail = str(step.get("detail") or "")
            expanded = (not self.collapse_done) or st in ("running", "pending")
            child = TreeNode(
                f"{id_base}:s{step.get('id')}",
                f"{icon} {step.get('id')}. {step.get('title')} [{st}]",
                "step",
                default_expanded=expanded,
                summary=_oneline(detail, 30),
                detail=detail,
            )
            if detail:
                child.children.append(TreeNode(f"{id_base}:s{step.get('id')}:d", _oneline(detail, 50), "step_detail"))
            node.children.append(child)
        return node

    def _plan_step_node(self, index: int, tool_name: str, content: str) -> Optional[TreeNode]:
        """计划四件套工具结果 → 时间线节点（JSON 快照冻结渲染）。
        非四件套 / JSON 解析失败（如白名单预算外置后的指针文本）返回 None 回落普通工具节点。
        节点 id 保持 msg:{index}（树回退锚点兼容）"""
        if tool_name not in ("write_plan", "update_plan", "generate_steps", "update_step_status"):
            return None
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return None
        if tool_name in ("write_plan", "update_plan") and isinstance(data, dict):
            return self._plan_node_from(f"msg:{index}", data)
        if tool_name == "generate_steps" and isinstance(data, list) and all(isinstance(s, dict) for s in data):
            return self._steps_node_from(f"msg:{index}", data)
        if tool_name == "update_step_status" and isinstance(data, dict):
            st = str(data.get("status") or "")
            icon = _STATUS_ICON.get(st, "○")
            note = str(data.get("detail") or "") or str(data.get("title") or "")
            label = f"{icon} 步骤 {data.get('id')} → {st or '?'}"
            if note:
                label += f" · {_oneline(note, 60)}"
            return TreeNode(f"msg:{index}", label, "step_update", default_expanded=True, detail="")
        return None

    def _snapshot_nodes(self, index: int, item: dict) -> List[TreeNode]:
        """type=plan 消息的 plan_snapshot/steps_snapshot extras（_exec_plan 落位最终版）→ 节点对"""
        out: List[TreeNode] = []
        raw_plan = item.get("plan_snapshot")
        if raw_plan:
            try:
                data = json.loads(raw_plan)
            except (json.JSONDecodeError, ValueError):
                data = None
            if isinstance(data, dict):
                out.append(self._plan_node_from(f"msg:{index}:plan", data))
        raw_steps = item.get("steps_snapshot")
        if raw_steps:
            try:
                data = json.loads(raw_steps)
            except (json.JSONDecodeError, ValueError):
                data = None
            if isinstance(data, list):
                out.append(self._steps_node_from(f"msg:{index}:steps", data))
        return out

    def _mcp_line(self) -> str:
        return mcp_mod.status_line()

    def _tree_signature(self) -> tuple:
        """会话内容签名：命中则复用 _build_tree 结果，避免每帧全量重建（流式增长亦靠它失效）"""
        msgs = self.messages[-200:]
        total = 0
        for m in msgs:
            total += len(memory_mod.content_text(m.get("content")))
            total += memory_mod.count_image_parts(m.get("content"))  # 图片块计入签名防漏失效
        plan = self.plan or {}
        streaming = self.streaming_msg if isinstance(self.streaming_msg, dict) else None
        live = self.subagent_stream if isinstance(self.subagent_stream, dict) else None
        return (
            len(msgs),
            total,
            self.task or "",
            len(self.steps),
            plan.get("status") or "",
            len(plan.get("content") or ""),
            self.collapse_done,
            len(streaming.get("thinking") or "") if streaming else -1,
            len(streaming.get("content") or "") if streaming else -1,
            len(live.get("text") or "") if live else -1,
            len(live.get("tools") or []) if live else -1,
            live.get("phase") or "" if live else "",
            self._mcp_line(),
        )

    def _flatten_tree(self) -> List[Tuple[TreeNode, int, bool, str]]:
        sig = self._tree_signature()
        if self._tree_sig != sig or self._tree_nodes is None:
            self._tree_nodes = self._build_tree()
            self._tree_sig = sig
        flat = []

        def walk(nodes, depth, guide):
            # guide：树引导线前缀，每级祖先占 2 列（"│ " 该祖先还有后继兄弟 / "  " 已是末位）
            for i, node in enumerate(nodes):
                last = i == len(nodes) - 1
                if node.id in self.expanded:
                    expanded = True
                elif f"!{node.id}" in self.expanded:
                    expanded = False
                else:
                    expanded = node.default_expanded
                flat.append((node, depth, expanded, guide))
                # 折叠类消息节点（user 等）收起的只是正文溢出行，
                # 其子节点（本回合的 Agent 回复/流式直播）始终展示
                if node.children and (expanded or node.kind in _TREE_FOLD_KINDS):
                    child_guide = (guide + ("  " if last else "│ ")) if depth >= 1 else ""
                    walk(node.children, depth + 1, child_guide)

        walk(self._tree_nodes, 0, "")
        self._flat_nodes = flat
        return flat

    def toggle_fold(self, index: int) -> None:
        if not self._flat_nodes:
            return
        index = max(0, min(index, len(self._flat_nodes) - 1))
        node, _, expanded, _ = self._flat_nodes[index]
        if expanded:
            self.expanded.discard(node.id)
            self.expanded.add(f"!{node.id}")
        else:
            self.expanded.discard(f"!{node.id}")
            self.expanded.add(node.id)
        # 帮助/计划/步骤等子节点型节点展开会改变 flat 布局（消息类子树恒 walk 不变）：
        # 重新展平并把光标钉回被切换的节点，保证“再按一次”仍作用于同一节点
        flat = self._flatten_tree()
        for i, (n, _, _, _) in enumerate(flat):
            if n.id == node.id:
                self.tree_cursor = i
                break

    def _tree_rows(self, width: int) -> List[str]:
        # 树行缓存：按键路径（partial 重绘）不重建行字符串，只做窗口切片。
        # key 必须用「当前实时签名」——不能用 self._tree_sig（它只在
        # _flatten_tree 重建后才更新，会导致消息已增加仍命中旧树缓存）。
        sig = self._tree_signature()
        key = (
            width,
            sig,
            self.collapse_done,
            tuple(sorted(self.expanded)),
            self.focus,
            self.tree_cursor,
        )
        if self._tree_rows_key == key and self._tree_rows_cache is not None:
            return self._tree_rows_cache
        flat = self._flatten_tree()
        tree_focus = self.focus == "tree"
        # 双色分层：角色徽标=饱和色加粗，正文=角色色向 ink 混合的浅色变体
        user_text = _fg(_mix(self.colors["user_msg"], self.colors["ink"], 0.45))
        agent_text = _fg(_mix(self.colors["agent"], self.colors["ink"], 0.50))
        subagent_text = _fg(_mix(self.colors["subagent"], self.colors["ink"], 0.40))
        colors = {
            "task": "\033[1m" + self.c("accent"),
            "plan": "\033[1m" + self.c("warn"),
            "plan_line": self.c("dim"),
            "steps": "\033[1m" + self.c("warn"),
            "step": self.c("ink"),
            "step_detail": self.c("dim"),
            "tool": self.c("tool"),
            "assistant": self.c("agent"),
            "user": self.c("user_msg"),
            "help": self.c("title"),
            "help_line": self.c("dim"),
            "system": self.c("dim"),
            "system_prompt": self.c("tool"),
            "text": self.c("dim"),
            "stream_thinking": self.c("thinking"),
            "stream_content": agent_text,
            "subagent": "\033[1m" + self.c("subagent"),
            "subagent_live": subagent_text,
            "mcp": self.c("mcp"),
            "step_update": self.c("tool"),
        }
        guide_c = self.c("tree_guide")
        sep_c = self.c("dim")
        # 角色徽标 → 浅色正文 的双色节点
        content_of = {"user": user_text, "assistant": agent_text}
        rows = []
        if not flat:
            self._node_spans = []
            return [self.c("dim") + _clip("  —", width) + self.RESET]
        # 消息类节点：正文在 label 内直接折行展示（同色连续），不另开详情区
        inline_kinds = {"assistant", "user", "system", "tool", "system_prompt"}
        def _marker(has: bool, is_expanded: bool) -> str:
            return "▾" if has and is_expanded else ("▸" if has else "·")

        spans: List[Tuple[int, int]] = []
        for abs_i, (node, _depth, expanded, guide) in enumerate(flat):
            span_start = len(rows)
            has = bool(node.children)
            if node.detail and node.kind not in inline_kinds:
                has = True
            label = node.label or ""
            color = colors.get(node.kind, self.c("ink"))
            foldable = node.kind in _TREE_FOLD_KINDS
            if foldable:
                # 折叠只作用于正文溢出行（子节点照常展示），marker 仅反映正文是否超限
                has = False
            gw = _display_width(guide)
            gseg = (guide_c + guide + self.RESET) if guide else ""
            indent = guide
            cont_prefix = guide + "  "
            if node.kind == "tool":
                # 工具调用（含工具轮）：标题青色（14babc）+ 内容纯白，同一物理行双色。
                # 折叠仅显示标题行（✅/❌ 状态在标题内），完整输出展开后查看
                title, sep, content_text = label.partition(" · ")
                probe_title = f"{indent}· {title}"
                budget = max(8, width - _display_width(probe_title) - _display_width(" · "))
                has_output = bool(sep) and content_text != ""
                wrapped = _wrap(content_text, budget) if (has_output and expanded) else []
                if foldable and not expanded and has_output:
                    has = True  # 折叠时 marker 提示有可展开内容
                title_part = f"{_marker(has, expanded)} {title}"
                title_w = gw + _display_width(title_part)
                if has_output and expanded:
                    first = wrapped[0] if wrapped else ""
                    rest = wrapped[1:]
                    row = (gseg + self.c("tool_title") + _clip(title_part, max(1, width - gw)) + self.RESET
                           + self.c("tool_content") + _clip(" · " + first, max(8, width - title_w - 3)) + self.RESET)
                    if abs_i == self.tree_cursor and tree_focus:
                        rows.append(self.C_HL + _pad(f"{indent}{title_part} · {first}", width) + self.RESET)
                    else:
                        rows.append(row)
                    for wline in rest:
                        rows.append(gseg + self.c("tool_content") + _clip("  " + wline, max(1, width - gw)) + self.RESET)
                else:
                    if abs_i == self.tree_cursor and tree_focus:
                        rows.append(self.C_HL + _pad(f"{indent}{title_part}", width) + self.RESET)
                    else:
                        rows.append(gseg + self.c("tool_title") + _clip(title_part, max(1, width - gw)) + self.RESET)
                spans.append((span_start, len(rows) - span_start))
                continue
            if node.kind == "stream_thinking":
                # 思考过程：过长折叠为尾部 3 行动态窗口（随流式追加滚动）
                win = _wrap(label, max(8, width - gw - 2))
                for wline in win[-3:]:
                    rows.append(gseg + color + _clip("  " + wline, max(1, width - gw)) + self.RESET)
                spans.append((span_start, len(rows) - span_start))
                continue
            # 消息类正文：全文折行；可折叠节点收起时最多 3 行，超出以指示行提示手动展开
            # 用户/Agent 消息双色：徽标（饱和加粗）+ 正文（浅色变体）；其余节点单色
            badge = ""
            body_text = label
            if node.kind in content_of:
                b, sep, body = label.partition(" · ")
                if sep:
                    badge, body_text = b, body
            content_c = content_of.get(node.kind, color)
            badge_plain = f"{badge} · " if badge else ""
            budget = max(8, width - gw - 2 - _display_width(badge_plain))
            vis = _wrap(body_text, budget)
            total_rows = len(vis)
            truncated = foldable and not expanded and total_rows > _TREE_INLINE_CAP
            if foldable and total_rows > _TREE_INLINE_CAP:
                has = True
            marker = _marker(has, expanded)
            first_prefix = f"{indent}{marker} "
            if truncated:
                vis = vis[:_TREE_INLINE_CAP]
            piece_no = 0
            for wline in vis:
                if abs_i == self.tree_cursor and tree_focus and piece_no == 0:
                    raw = first_prefix + badge_plain + wline
                    rows.append(self.C_HL + _pad(_ANSI_RE.sub("", raw), width) + self.RESET)
                elif piece_no == 0:
                    seg = gseg + color + marker + " " + self.RESET
                    if badge:
                        seg += "\033[1m" + color + badge + self.RESET + sep_c + " · " + self.RESET
                    seg += content_c + _clip(wline, budget) + self.RESET
                    if not expanded and node.summary:
                        seg += sep_c + _clip(
                            f" · {node.summary}",
                            max(0, width - gw - 2 - _display_width(badge_plain) - _display_width(wline)),
                        ) + self.RESET
                    rows.append(seg)
                else:
                    rows.append(gseg + content_c + _clip("  " + wline, max(1, width - gw)) + self.RESET)
                piece_no += 1
            if truncated:
                hint = f"… (+{total_rows - _TREE_INLINE_CAP} 行)"
                rows.append(gseg + self.c("dim") + _clip("  " + hint, max(1, width - gw)) + self.RESET)
            # 计划/帮助等仍可能有 detail：仅在无子节点时追加（消息类 inline 已含全文）
            if expanded and node.detail and not node.children and node.kind not in inline_kinds:
                for dline in _wrap(str(node.detail), max(10, width - gw - 2))[:8]:
                    rows.append(gseg + self.c("dim") + _clip("  " + dline, max(1, width - gw)) + self.RESET)
            spans.append((span_start, len(rows) - span_start))
        self._node_spans = spans
        self._tree_rows_key = key
        self._tree_rows_cache = rows
        return rows

    def _rotating_tip(self) -> str:
        interval = float(self.keys.get("tip_interval") or 5)
        return TIPS[int(time.time() // interval) % len(TIPS)]

    def _compose_plain(self, width: int, height: int) -> List[str]:
        w = max(40, width)
        h = max(20, height)
        if self.settings_mode:
            return self._compose_settings(w, h)
        if self.sessions_mode:
            return self._compose_sessions(w, h)

        raw = self.buffer.to_text()
        inner_w = max(10, w - _display_width(self._input_prompt) - 1)

        # 输入 wrap：逻辑行增量（Layout）
        if self._input_layout_width != inner_w:
            self._input_layout.set_width(inner_w)
            self._input_layout_width = inner_w
        self._input_layout.apply_edit(raw, self.cursor)
        wrapped = self._input_layout.visual_rows() or [""]
        text_h = min(3, max(1, len(wrapped)))
        cand_show = min(3, len(self.candidates)) if self.candidates else 0
        input_zone_h = min(6, text_h + cand_show)
        # 确认面板：取代输入框位置，行数更多时向上拓展（压缩会话区）
        # 确认/询问面板：取代输入框位置，行数更多时向上拓展（压缩会话区）
        if self.ask_mode:
            confirm_panel = self._compose_ask(w)
        elif self.confirm_mode:
            confirm_panel = self._compose_confirm(w)
        else:
            confirm_panel = None
        if confirm_panel is not None:
            input_zone_h = len(confirm_panel)

        bottom_fixed = 1 + 2 + 1 + 1  # rule + info/mode+tip + rule + status
        phase_fixed = 1               # 树底阶段提示行：思考中/工具调用中/完成空行占位
        top_fixed = 1 + 1
        tree_h = max(4, h - top_fixed - bottom_fixed - input_zone_h - phase_fixed)
        self._row_meta["tree_h"] = tree_h
        tree_focus = self.focus == "tree"
        mode_label = policy.MODE_LABELS.get(self.mode, self.mode)

        lines = []
        # 会话
        lines.append(self.C_HL + _pad(_clip(" 会话", w), w) + self.RESET if tree_focus else self.c("title") + "会话" + self.RESET)
        # 空会话：Logo 占位会话区；有内容后切回树
        if self._show_splash_logo():
            body = self._compose_logo_body(w, tree_h)
            self._row_meta["scrollbar_on"] = 0
        else:
            # 右侧固定预留 1 列滚动条（内容不足一屏时该列留空，宽度稳定不跳变）
            rows = self._tree_rows(w - 1)
            total = len(rows)
            # 内容超出可视区时：默认贴底显示最新会话；用户上滚后取消跟随
            self._clamp_tree_scroll(total, tree_h)
            visible = rows[self.scroll : self.scroll + tree_h]
            body = list(visible) + [""] * (tree_h - len(visible))
            body = body[:tree_h]
            sel = self._selection_range()
            if sel is not None:
                r0, c0, r1, c1 = sel
                body = [
                    self._apply_row_selection(
                        ln,
                        c0 if self.scroll + i == r0 else 0,
                        c1 if self.scroll + i == r1 else w - 2,
                        w - 1,
                    )
                    if r0 <= self.scroll + i <= r1
                    else ln
                    for i, ln in enumerate(body)
                ]
            if total > tree_h:
                thumb_h, thumb_top = _scrollbar_geometry(total, tree_h, self.scroll)
                # 几何快照：鼠标拖拽事件按此换算（每帧刷新）
                meta = self._row_meta
                meta["scrollbar_on"] = 1
                meta["scrollbar_x"] = w - 1
                meta["body_top"] = 1
                meta["scroll_total"] = total
                meta["thumb_h"] = thumb_h
                meta["thumb_top"] = thumb_top
                body = self._with_scrollbar(body, total, tree_h, w - 1)
            else:
                self._row_meta["scrollbar_on"] = 0
        lines.extend(body)
        # 树底阶段提示（用户规格）：用户消息发出后常驻显示，直至本轮 Agent 结束；
        # 各阶段共用转轮+轮换机制，仅文案池不同（base 匹配 _PHASE_POOLS，后缀保留）；
        # 确认面板/对话框（confirm/ask/choose/line）期间隐藏，空行占位
        hint = (self.phase_hint or "").strip()
        if hint and not self.confirm_mode and not self._dialog_active:
            frame = _SPINNER_FRAMES[int(time.time() * 12) % len(_SPINNER_FRAMES)]
            base, sep, detail = hint.partition(" · ")
            pool = _PHASE_POOLS.get(base)
            if pool:
                slot = int(time.time() // _HINT_ROTATE_SECONDS) % len(pool)
                text = pool[slot] + (f" · {detail}" if sep and detail else "")
            else:
                text = hint
            lines.append(self.c("warn") + _pad(_clip(f" {frame} {text}", w), w) + self.RESET)
        else:
            lines.append("")
        lines.append(self._rule(w))

        # 输入区
        cursor_row, before_last_w = self._input_layout.cursor_visual(raw, self.cursor)
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
        # 输入框：显示缓冲与光标（确认面板模式下跳过，行位由面板取代）
        if confirm_panel is None and not raw.strip():
            input_rows.append(
                f"{accent}>{self.RESET} {self.c('dim')}{_clip('输入消息或 / 命令 · Enter 提交 · Tab 补全', inner_w)}{self.RESET}{accent}▌{self.RESET}"
            )
        elif confirm_panel is None:
            ink = self.c("ink")
            mark = f"{self.c('accent')}▌{self.RESET}"
            for i, part in enumerate(vis):
                abs_row = self.input_scroll + i
                prefix = self._input_prompt if abs_row == 0 else " " * _display_width(self._input_prompt)
                head = f"{accent}{prefix}{self.RESET}{ink}"
                if self.focus == "input" and abs_row == cursor_row:
                    # 显示光标必须插在逻辑光标列，不能固定贴在行尾
                    left, right = split_at_cells(part, before_last_w)
                    input_rows.append(f"{head}{left}{mark}{ink}{_clip(right, inner_w)}{self.RESET}")
                else:
                    input_rows.append(f"{head}{_clip(part, inner_w)}{self.RESET}")
        if cand_show and confirm_panel is None:
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
        if confirm_panel is not None:
            # 面板行取代输入框行位（高度已在 tree_h 计算中向上拓展）
            lines.extend(confirm_panel)
        else:
            lines.extend(input_rows[:input_zone_h])
        lines.append(self._rule(w))

        # 底部：项目 · git · 模型 · 模式（同一行；分段配色，模式按语义着色）
        mode_c = {
            policy.MODE_AUTO: self.c("ok"),
            policy.MODE_MANUAL: self.c("warn"),
            policy.MODE_FULL: self.c("err"),
        }.get(self.mode, self.c("accent"))
        info = (
            self.c("accent") + f"→{self.project_name}" + self.RESET
            + self.c("dim") + f" · git:({self.git_branch}) · " + self.RESET
            + self.c("ink") + self.model_name + self.RESET
            + self.c("dim") + " · " + self.RESET
            + mode_c + mode_label + self.RESET
        )
        lines.append(_clip_keep_ansi(info, w))
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
        input_start = None
        # 从后往前找「→项目」信息行，其前为输入区
        for i, ln in enumerate(lines):
            plain = _ANSI_RE.sub("", ln)
            if plain.startswith("→"):
                self._row_meta["info"] = i
                self._row_meta["status"] = min(h - 1, i + 3)
                self._row_meta["tip"] = min(h - 1, i + 1)
                input_start = max(0, i - input_zone_h - 2)
                break
        if input_start is None:
            input_start = max(0, h - input_zone_h - 5)
        self._row_meta["input_start"] = input_start
        self._row_meta["input_h"] = input_zone_h
        return lines[:h]

    def render(self, *args, **kwargs) -> None:
        """整帧渲染：rich Live 接管输出（TMP app.py:1632 同构）。

        _compose_plain 继续产出带 ANSI 的行字符串（复用全部现有着色/裁剪/
        换行逻辑），Text.from_ansi 桥接为 rich renderable 后交 Live.update——
        rich 负责帧缓冲 diff、光标与终端写，应用每帧无条件调用本方法。
        """
        w, h = _term_size()
        lines = self._compose_plain(w, h)
        if len(lines) < h:
            lines = list(lines) + [""] * (h - len(lines))
        lines = lines[:h]
        if self._live is not None:
            renderable = Group(*[Text.from_ansi(_clip_keep_ansi(l, w)) for l in lines])
            self._live.update(renderable, refresh=True)
        else:
            # Live 未启动（理论不达：render 均发生在 enter 之后）；兜底直写
            parts = [f"\033[{i + 1};1H\033[2K{_clip_keep_ansi(l, w)}" for i, l in enumerate(lines)]
            sys.stdout.write("".join(parts))
            sys.stdout.flush()

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
            self._tick_paste_async()
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
            line = self._buf_text()
            if line.startswith("/"):
                # 命令行不展开粘贴占位（占位机制只服务对话消息）
                self.paste.reset()
            else:
                line, images = self.paste.expand(line)
                if images:
                    self.pending_images.append(images)
            if line.strip():
                self.input_history.append(line)
                if self._llm_retry_gate is not None:
                    # 等待重试时提交新消息：自动放弃当前错误，消息经 busy 队列接力
                    self._llm_retry_gate.give_up.set()
            self.hist_index = len(self.input_history)
            self._buf_clear()
            self.candidates = []
            return line

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

    def _selection_range(self) -> Optional[Tuple[int, int, int, int]]:
        """归一化选区 (r0, c0, r1, c1)：文档行号 + 单元格，两端含端点；失效返回 None

        失效比对用帧内已算好的 _tree_sig（_tree_rows 每帧先于叠加更新，且缓存
        命中时与当前签名等值），避免每帧重算 _tree_signature（内含 mcp.status_line）。"""
        if self._sel_anchor is None or self._sel_focus is None:
            return None
        if self._tree_sig is not None and self._sel_sig != self._tree_sig:
            self._sel_anchor = self._sel_focus = None
            self._sel_edge = 0
            return None
        (r0, c0), (r1, c1) = self._sel_anchor, self._sel_focus
        if (r1, c1) < (r0, c0):
            r0, c0, r1, c1 = r1, c1, r0, c0
        return r0, c0, r1, c1

    def _selection_tick(self) -> None:
        """拖拽选区顶/底缘的帧级自动滚动：motion 事件只在鼠标移动时到达，
        鼠标停在边缘时由帧循环持续滚（每帧 2 行 @20fps ≈ 40 行/秒）。"""
        if not self._sel_dragging or not self._sel_edge or self._sel_anchor is None:
            return
        self._set_scroll(self.scroll + 2 * self._sel_edge)
        last = max(0, len(self._tree_rows_cache or []) - 1)
        tree_h = int(self._row_meta.get("tree_h") or 4)
        if self._sel_edge < 0:
            row = self.scroll
        else:
            row = min(self.scroll + tree_h - 1, last)
        cell = self._sel_focus[1] if self._sel_focus else 0
        self._sel_focus = (row, cell)

    @staticmethod
    def _cell_to_col(plain: str, cell: int, end: bool = False) -> int:
        """显示单元格 → 字符下标（宽字符跨界按包含处理）。

        end=False 取覆盖 cell 的首个字符；end=True 取其后（右边界含端）。"""
        used = 0
        for i, ch in enumerate(plain):
            w = _char_width(ch)
            if used + w > cell:
                return i + 1 if end else i
            used += w
        return len(plain)

    def _apply_row_selection(self, row: str, c0: int, c1: int, width: int) -> str:
        """带 ANSI 行上叠加选区背景：[c0, c1] 单元格（含端点）。行先补齐到 width。

        分段发射：仅在「进入/离开选区、SGR 变化」的边界发射一次 SGR（选中段
        = cur+底色，未选中段 = cur），而非逐字符重放——拖拽时每帧多出上万字节
        转义序列会拉爆 rich 的整帧重写，表现为闪烁。"""
        sel_bg = _bg(self.colors["selection_bg"])
        padded = _pad(row, width)
        out = []
        cur = ""  # 自上个 RESET 起累积生效的 SGR
        emitting_sel = None  # 上个可见字符是否处于选区（None=尚未发射可见字符）
        used = 0
        i = 0
        while i < len(padded):
            ch = padded[i]
            m = _ANSI_RE.match(padded, i)
            if m:
                seq = m.group(0)
                out.append(seq)
                if seq == "\033[0m":
                    cur = ""
                    emitting_sel = None  # RESET 后下个可见字符前需重新声明状态
                elif seq.endswith("m"):
                    cur += seq
                    if emitting_sel is True:
                        out.append(sel_bg)  # 新 SGR 落在选中段中段：底色重声明
                i = m.end()
                continue
            w = _char_width(ch)
            selected = used + w > c0 and used <= c1
            if selected != emitting_sel and (emitting_sel is not None or selected):
                # 状态切换：RESET 后重放完整 SGR（cur）。只重放 cur 不够——
                # 它通常只含 fg 分量，选区底色会残留到行尾（未选中尾段被误高亮）
                out.append("\033[0m" + cur + (sel_bg if selected else ""))
                emitting_sel = selected
            out.append(ch)
            used += w
            i += 1
        if cur or emitting_sel:
            out.append(self.RESET)  # 有未闭合状态才补；行已自带 RESET 则不重复
        return "".join(out)

    def _selection_text(self) -> Optional[str]:
        """选区纯文本：按单元格切首尾行，中间行取整行，去行尾填充空白"""
        rng = self._selection_range()
        if rng is None:
            return None
        r0, c0, r1, c1 = rng
        cache = self._tree_rows_cache or []
        lines = []
        for r in range(max(0, r0), min(r1, len(cache) - 1) + 1):
            plain = _ANSI_RE.sub("", cache[r])
            if r == r0 and r == r1:
                plain = plain[self._cell_to_col(plain, c0): self._cell_to_col(plain, c1, end=True)]
            elif r == r0:
                plain = plain[self._cell_to_col(plain, c0):]
            elif r == r1:
                plain = plain[: self._cell_to_col(plain, c1, end=True)]
            lines.append(plain.rstrip())
        return ("\n".join(lines)).strip("\n") or None

    def _copy_selection(self) -> None:
        text = self._selection_text()
        if not text:
            return
        if _copy_to_clipboard(text):
            self.status = f"已复制选区 {text.count(chr(10)) + 1} 行 · {len(text)} 字符"

    def _with_scrollbar(self, body: List[str], total: int, tree_h: int, width: int) -> List[str]:
        """会话区右侧 1 列滚动条：█ 滑块(accent) + │ 轨道(dim) + ▲▼ 端点(dim)。

        body 各行已按显示宽度 ≤ width；此处补齐到 width 再拼滚动条字符，
        合计恰好占满终端宽。"""
        thumb_h, thumb_top = _scrollbar_geometry(total, tree_h, self.scroll)
        accent, dim = self.c("accent"), self.c("dim")
        out: List[str] = []
        for i, row in enumerate(body):
            if i == 0 and self.scroll > 0:
                glyph, color = "▲", dim
            elif i == tree_h - 1 and self.scroll + tree_h < total:
                glyph, color = "▼", dim
            elif thumb_top <= i < thumb_top + thumb_h:
                glyph, color = "█", accent
            else:
                glyph, color = "│", dim
            out.append(_pad(row, width) + color + glyph + self.RESET)
        return out

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

    def _compose_confirm(self, w: int) -> List[str]:
        """确认面板内容：显示在输入框位置，行数多时向上拓展（压缩会话区）。"""
        tool = self.confirm_tool or {}
        name = str(tool.get("name") or "?")
        args = tool.get("arguments") or {}
        try:
            arg_line = json.dumps(args, ensure_ascii=False)
        except Exception:
            arg_line = str(args)
        rows: List[str] = []
        rows.append(self.c("warn") + " ⚠ " + self.RESET + self.c("accent") + _clip(f"工具确认: {name}", w - 4) + self.RESET)
        if arg_line and arg_line != "{}":
            rows.append(self.c("dim") + " " + _clip(_oneline(arg_line, w - 3), w - 2) + self.RESET)
        if self.confirm_reason_mode:
            # 拒绝原因输入：块光标跟随文本
            text = self._confirm_buf.to_text()
            cur = max(0, min(self._confirm_buf.cursor, len(text)))
            left, right = text[:cur], text[cur:]
            prompt = " 拒绝原因> "
            inner = max(4, w - _display_width(prompt) - 3)
            left_c = _clip(left, inner)
            used = _display_width(left_c)
            right_c = _clip(right, max(0, inner - used))
            rows.append(
                f"{self.c('accent')}{prompt}{self.RESET}"
                f"{self.c('ink')}{left_c}{self.C_HL} {self.RESET}{right_c}{self.RESET}"
            )
            rows.append(self.c("dim") + _clip(" Enter 提交（空=默认理由） · Esc 返回选项", w - 1) + self.RESET)
            return rows
        labels = (
            ("allow_once", "允许执行一次"),
            ("allow_always", "在本项目中始终允许该类指令"),
            ("reject", "拒绝（默认理由）"),
            ("reason", "拒绝并输入原因"),
        )
        idx = self.confirm_index % len(labels)
        for i, (_act, label) in enumerate(labels):
            line = f" {'❯' if i == idx else ' '} {label}"
            if i == idx:
                rows.append(self.C_HL + _pad(_clip(line, w - 1), w - 1) + self.RESET)
            else:
                rows.append(self.c("ink") + _clip(line, w - 1) + self.RESET)
        rows.append(self.c("dim") + _clip(" ↑↓ 选择 · Enter 确认 · Esc 拒绝", w - 1) + self.RESET)
        return rows

    def _compose_ask(self, w: int) -> List[str]:
        """询问用户面板内容：问题+选项（概述+描述）+末位自由输入+倒计时"""
        spec = self.ask_spec or {}
        options = list(spec.get("options") or [])
        rows: List[str] = []
        rows.append(
            self.c("accent") + " ❓ " + self.RESET
            + self.c("warn") + _clip(f"询问用户: {spec.get('question') or ''}", w - 6) + self.RESET
        )
        if self.ask_input_mode:
            # 自由输入态：块光标跟随文本（同 confirm 拒绝原因输入）
            text = self._ask_buf.to_text()
            cur = max(0, min(self._ask_buf.cursor, len(text)))
            left, right = text[:cur], text[cur:]
            prompt = " 你的意见> "
            inner = max(4, w - _display_width(prompt) - 3)
            left_c = _clip(left, inner)
            used = _display_width(left_c)
            right_c = _clip(right, max(0, inner - used))
            rows.append(
                f"{self.c('accent')}{prompt}{self.RESET}"
                f"{self.c('ink')}{left_c}{self.C_HL} {self.RESET}{right_c}{self.RESET}"
            )
            rows.append(self.c("dim") + _clip(" Enter 提交 · Esc 返回选项", w - 1) + self.RESET)
            return rows
        for i, opt in enumerate(options):
            title = str(opt.get("title") or "")
            desc = str(opt.get("description") or "")
            line = f" {'❯' if i == self.ask_index else ' '} {i + 1}. {title}"
            if i == self.ask_index:
                rows.append(self.C_HL + _pad(_clip(line, w - 1), w - 1) + self.RESET)
            else:
                rows.append(self.c("ink") + _clip(line, w - 1) + self.RESET)
            if desc:
                dline = f"      {desc}"
                color = self.c("dim") if i != self.ask_index else self.c("ink")
                rows.append(color + _clip(dline, w - 1) + self.RESET)
        free_i = len(options)
        line = f" {'❯' if self.ask_index == free_i else ' '} ✎ 其他（自行输入意见）"
        if self.ask_index == free_i:
            rows.append(self.C_HL + _pad(_clip(line, w - 1), w - 1) + self.RESET)
        else:
            rows.append(self.c("ink") + _clip(line, w - 1) + self.RESET)
        hint = " ↑↓ 选择 · 数字快选 · Enter 确认 · Esc 跳过"
        if self.ask_deadline is not None:
            rem = max(0, int(self.ask_deadline - time.monotonic()))
            hint += f" · ⏱ {rem // 60}:{rem % 60:02d} 后自动跳过"
        rows.append(self.c("dim") + _clip(hint, w - 1) + self.RESET)
        return rows

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
                if ftype == "plugin":
                    # 树状插件行：展开箭头 + 开关状态；可展开项附 Enter 提示
                    state = "开" if field.get("current") is True else "关"
                    arrow = "▾" if field.get("expanded") else ("▸" if field.get("expandable") else "·")
                    extra = " · Enter配置" if field.get("expandable") else ""
                    display = _clip(f"{arrow} {label:20} < {state} > ←→{extra}", w - 4)
                elif ftype == "pbool":
                    # 勾选框模式（缩进于插件行下）
                    mark = "[x]" if field.get("current") in (True, "true", "1", 1) else "[ ]"
                    display = _clip(f"    {mark} {label}", w - 4)
                elif ftype == "pchoice":
                    show = "" if field.get("current") is None else str(field.get("current"))
                    display = _clip(f"    {label:18} < {show} > ←→", w - 4)
                elif ftype == "ptext":
                    show = "" if field.get("current") is None else str(field.get("current"))
                    display = _clip(f"    {label:18} {show}", w - 4)
                elif ftype == "choice":
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
    settings_tabs = ["提供商", "模型", "系统", "快捷键", "插件"]

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
        "newline": ["shift+enter", "enter"],
        "complete": ["tab"],
        "scroll_up": ["pageup", "ctrl+up"],
        "scroll_down": ["pagedown", "ctrl+down"],
        "expand": ["right", "l"],
        "collapse": ["left", "h"],
        "rollback": ["ctrl+z"],
    }

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

    def _settings_model_names(self, config) -> List[str]:
        names = config.list_model_names() or [config.model_name]
        return names

    @staticmethod
    def _settings_workflow_names(config) -> List[str]:
        """已识别的工作流列表（data/workflows/*.json），至少含当前激活项"""
        from core import workflow as workflow_mod

        names = workflow_mod.list_workflows(config)
        active = workflow_mod.active_name(config)
        if active not in names:
            names.append(active)
        return names or ["default"]

    @staticmethod
    def _settings_workflow_current(config) -> str:
        from core import workflow as workflow_mod

        return workflow_mod.active_name(config)

    # 焦点离开时才重建模型列表的文本字段
    _MODEL_LIST_TEXT_KEYS = frozenset({"provider_id", "model_id"})

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
            "log_level": s.get("log_level"),
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

    def _immediate_settings_refresh(self, config) -> None:
        """立刻重建（切标签/保存/切换提供商等）"""
        self._build_settings_fields(config)

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
                ("send", "发送", "Enter 恒提交；此为额外发送键"),
                ("newline", "换行", "默认 Shift+Enter；Enter 不作换行"),
                ("complete", "命令补全", "有候选时优先补全"),
                ("expand", "树展开", "焦点在会话树时"),
                ("collapse", "树折叠", "焦点在会话树时"),
                ("rollback", "树回退", "会话树 Ctrl+Z 回退到选中节点；再按取消"),
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
        elif tab == "插件":
            # 插件列表 + 声明式配置（树状缩进）：插件行 ←→ 启停（写盘+重载）、
            # Enter 展开/收起；配置行按声明类型渲染（bool 勾选框 / list 左右切换 /
            # str·int·float 行编辑），所有改动即时写透 config.plugins_config
            from core import plugins as plugins_mod

            rows = plugins_mod.statuses()
            fields = []
            if not rows:
                fields.append(
                    {
                        "key": "_plugin_none",
                        "label": "（未发现插件 · 放置 data/plugins/<id>/ 后 /plugin reload）",
                        "type": "sep",
                    }
                )
            else:
                expanded_ids = getattr(self, "_plugin_expanded", set())
                ftype_map = {"bool": "pbool", "list": "pchoice", "str": "ptext", "int": "ptext", "float": "ptext"}
                for row in rows:
                    pid = row["id"]
                    decls = plugins_mod.declared_configs(pid)
                    expanded = pid in expanded_ids
                    fields.append(
                        {
                            "key": f"plugin_{pid}",
                            "label": f"{row['name']} ({pid})",
                            "type": "plugin",
                            "pid": pid,
                            "status": row["status"],
                            "current": row["status"] == "loaded",
                            "expandable": bool(decls),
                            "expanded": expanded,
                            "hint": row.get("error") or row.get("description") or "←→ 开/关 · Enter 展开/收起配置",
                        }
                    )
                    if not expanded:
                        continue
                    for d in decls:
                        fields.append(
                            {
                                "key": f"pcfg_{pid}__{d['key']}",
                                "label": d.get("label") or d["key"],
                                "type": ftype_map[d["type"]],
                                "pid": pid,
                                "ckey": d["key"],
                                "decl": d,
                                "options": d.get("options"),
                                "current": plugins_mod.get_setting(pid, d["key"], d.get("default"), config),
                                "hint": d.get("hint")
                                or {"list": "←→ 切换选项", "bool": "Enter/←→ 勾选"}.get(d["type"], "Enter 编辑"),
                            }
                        )
        else:
            ui_cfg = (config.data or {}).get("ui") or {}
            mem_cfg = (config.data or {}).get("memory") or {}
            llm_cfg = (config.data or {}).get("llm") or {}
            ctx_cfg = (config.data or {}).get("context") or {}
            wf_cfg = (config.data or {}).get("workflow") or {}
            tools_cfg = (config.data or {}).get("tools") or {}
            log_level = str((config.data or {}).get("log", {}).get("level") or "info").strip().lower()
            if log_level not in ("debug", "info", "warn", "error", "off"):
                log_level = "info"
            themes = ["dark"] + [t for t in config.list_themes() if t != "dark"]
            fields = [
                {"key": "theme", "label": "主题", "type": "choice", "options": themes, "current": config.theme, "hint": "←→ 切换主题"},
                {"key": "mode", "label": "访问模式", "type": "choice", "options": list(policy.MODES), "current": config.mode, "hint": "auto/manual/full"},
                {"key": "busy_send_mode", "label": "等待时新消息", "type": "choice", "options": ["queue", "interrupt"], "current": str(ui_cfg.get("busy_send_mode") or "queue"), "hint": "queue=排队等本轮结束 · interrupt=中断插入"},
                {"key": "logo", "label": "会话区Logo", "type": "bool", "current": bool(ui_cfg.get("logo", True)), "hint": "←→ 开/关"},
                {"key": "font_size", "label": "字体大小", "type": "text", "current": getattr(config, "font_size", 16)},
                {"key": "tip_interval", "label": "提示间隔秒", "type": "text", "current": ui_cfg.get("tip_interval", 5)},
                {"key": "retry_times", "label": "LLM重试次数", "type": "text", "current": llm_cfg.get("retry_times", 3)},
                {"key": "retry_delay", "label": "LLM重试延迟", "type": "text", "current": llm_cfg.get("retry_delay", 1.0)},
                {"key": "scroll_v0", "label": "滚动v0", "type": "text", "current": ui_cfg.get("scroll_v0", 1.0)},
                {"key": "scroll_hold_ms", "label": "长按阈值ms", "type": "text", "current": ui_cfg.get("scroll_hold_ms", 150)},
                {"key": "scroll_max_step", "label": "滚动最大步长", "type": "text", "current": ui_cfg.get("scroll_max_step", 20)},
                {"key": "auto_compress", "label": "自动压缩上下文", "type": "bool", "current": bool(mem_cfg.get("auto_compress", True)), "hint": "←→"},
                {"key": "compress_threshold", "label": "压缩阈值", "type": "text", "current": mem_cfg.get("compress_threshold", 0.8)},
                {"key": "compress_keep_recent_tokens", "label": "压缩保留尾段token", "type": "text", "current": mem_cfg.get("compress_keep_recent_tokens", 16384)},
                {"key": "compress_summary_max_tokens", "label": "摘要上限token", "type": "text", "current": mem_cfg.get("compress_summary_max_tokens", 1024)},
                {"key": "tool_whitelist", "label": "工具白名单", "type": "text", "current": ", ".join(str(x) for x in (ctx_cfg.get("tool_whitelist") or [])), "hint": "逗号分隔 · 白名单工具结果跨回合保留"},
                {"key": "max_tool_rounds", "label": "最大工具轮数", "type": "text", "current": wf_cfg.get("max_rounds", 0), "hint": "0=用工作流文件值 · 超限后仍有进展会自动续期"},
                {"key": "spin_kill_count", "label": "空转计数上限", "type": "text", "current": wf_cfg.get("spin_kill_count", 12), "hint": "重复输出计满即终止回合；未满期间续期不限次"},
                {"key": "max_tool_timeout", "label": "命令超时上限秒", "type": "text", "current": tools_cfg.get("max_timeout", 600), "hint": "execute_command/run_program 传入超时的钳制上限（1-∞）"},
                {"key": "ask_user_timeout", "label": "询问自动超时", "type": "bool", "current": bool(tools_cfg.get("ask_user_timeout", True)), "hint": "←→ 开/关 · 开启后询问框 5 分钟未选择自动跳过"},
                {"key": "workflow_enabled", "label": "启用工作流", "type": "bool", "current": bool(wf_cfg.get("enabled", False)), "hint": "←→ 关=直接对话 · 开=按工作流节点链运行（下一回合生效）"},
                {"key": "workflow_active", "label": "工作流", "type": "choice", "options": self._settings_workflow_names(config), "current": self._settings_workflow_current(config), "hint": "←→ 选择处理管线（仅在启用工作流时生效）"},
                {"key": "log_level", "label": "日志等级", "type": "choice", "options": ["debug", "info", "warn", "error", "关闭"], "current": "关闭" if log_level == "off" else log_level, "hint": "←→ 关闭=不记录任何日志（含写盘）"},
                {"key": "active_model_name", "label": "全局默认模型", "type": "choice", "options": self._settings_model_names(config), "current": config.model_name, "hint": "←→ 切换当前模型"},
            ]
        self.settings_fields = fields
        # 初始化 scratch：保留同 key 已编辑值（插件页字段写透持久化，不经 scratch）
        for field in fields:
            if field.get("type") in ("plugin", "pbool", "pchoice", "ptext"):
                scratch_keep.pop(field["key"], None)
                continue
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
        self.settings_mode = True
        self.settings_tab = 0
        self.settings_index = 0
        self._settings_scroll = 0
        self.settings_scratch = {}
        self._plugin_expanded = set()
        self.settings_provider_id = config.data.get("active_provider_id", "")
        self.settings_model_name = config.model_name
        self.settings_notice = "↑↓ 选择 · ←→ 修改 · Enter保存退出 · Esc放弃"
        self.settings_model_work = getattr(self, "settings_model_work", {}) or {}
        self._build_settings_fields(config)
        self._settings_baseline = self._settings_snapshot(config)
        self._settings_esc_stage = 0
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
                self.settings_tab = (self.settings_tab + 1) % len(self.settings_tabs)
                self.settings_index = 0
                self._settings_scroll = 0
                self._immediate_settings_refresh(config)
                self._clamp_settings_index()
                self.render()
                continue
            if kind in ("submit", "submit_ctrl"):
                self._clamp_settings_index()
                field = self.settings_fields[self.settings_index] if self.settings_fields else {}
                ftype = field.get("type")
                # 插件页：Enter 展开/收起插件行；勾选 bool；行编辑 str/int/float
                if ftype == "plugin":
                    self._settings_plugin_expand(field)
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                    self.render()
                    continue
                if ftype == "pbool":
                    self._settings_plugin_toggle_bool(config, field)
                    self._immediate_settings_refresh(config)
                    self.render()
                    continue
                if ftype == "ptext":
                    self._settings_plugin_edit_text(config, field)
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                    self.render()
                    continue
                # 执行动作 / 文本编辑 / 开关
                if ftype == "action":
                    if field.get("key") == "save_provider":
                        msg = self._settings_save_provider(config)
                        self.settings_notice = msg
                        self._immediate_settings_refresh(config)
                        self._clamp_settings_index()
                        self.render()
                        continue
                    self._settings_run_action(config, field.get("key"))
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                    self.render()
                    continue
                if ftype == "text":
                    key_name = field.get("key")
                    label = field.get("label", key_name)
                    cur = self.settings_scratch.get(key_name, field.get("current"))
                    self.settings_mode = False
                    self.render()
                    try:
                        raw = _paused_input(f"{label} [{cur}]: ")
                    except (EOFError, KeyboardInterrupt):
                        raw = ""
                    self.settings_mode = True
                    if raw != "":
                        self.settings_scratch[key_name] = raw
                    if key_name in self._MODEL_LIST_TEXT_KEYS:
                        self._immediate_settings_refresh(config)
                    else:
                        self._build_settings_fields(config)
                    self.settings_notice = f"{label} 已更新 · Enter 保存退出"
                    self._clamp_settings_index()
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
                self.bind_config(config)
                self.render()
                return updates
            dirn = _key_direction(kind, value)
            if dirn == "up":
                self._settings_move_selection(-1)
                self.render()
            elif dirn == "down":
                self._settings_move_selection(1)
                self.render()
            elif dirn == "left":
                if self._settings_plugin_adjust(config, -1):
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                else:
                    self._settings_cycle_choice(-1, config)
                self.render()
            elif dirn == "right":
                if self._settings_plugin_adjust(config, 1):
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                else:
                    self._settings_cycle_choice(1, config)
                self.render()
            else:
                # 其它键（含 backspace/char——文本编辑统一走 Enter 后的行输入）仅重绘
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

    # ----- 历史会话面板 -----

    def _compose_sessions(self, w: int, h: int) -> List[str]:
        """历史会话：列表选择；Enter 切换 / 新建，d 删除，Esc 取消"""
        items = self.sessions_items
        lines = []
        lines.append(self.c("title") + _pad(_clip(f" {self.project_name} · 历史会话", w), w) + self.RESET)
        lines.append(self.c("dim") + _clip(" ↑↓ 选择 · Enter 切换 · d 删除 · Esc 取消", w) + self.RESET)
        lines.append(self._rule(w))

        list_h = max(6, h - 7)
        if self.sessions_index < self._sessions_scroll:
            self._sessions_scroll = self.sessions_index
        if self.sessions_index >= self._sessions_scroll + list_h:
            self._sessions_scroll = self.sessions_index - list_h + 1
        self._sessions_scroll = max(0, min(self._sessions_scroll, max(0, len(items) - list_h)))

        body = []
        if not items:
            body.append(self.c("dim") + "  （暂无历史会话，对话后自动保存）" + self.RESET)
        else:
            visible = items[self._sessions_scroll : self._sessions_scroll + list_h]
            for offset, item in enumerate(visible):
                i = self._sessions_scroll + offset
                is_new = item.get("action") == "new"
                is_cur = item.get("id") == self._sessions_current_id
                if is_new:
                    text = _pad(_clip(item.get("title") or "＋ 新建会话", w - 4), w - 4)
                else:
                    title = (item.get("title") or "（无标题会话）") + (" *" if is_cur else "")
                    meta = f"{item.get('message_count', 0)}条 · {str(item.get('updated_at') or '')[:16].replace('T', ' ')}"
                    text = _clip(title, max(8, w - 6 - _display_width(meta)))
                    text += " " * max(1, w - 6 - _display_width(text) - _display_width(meta)) + meta
                    text = _pad(text, w - 4)
                prefix = f"▸ {text}" if i == self.sessions_index else f"  {text}"
                if i == self.sessions_index:
                    body.append(self.C_HL + _pad(prefix, w) + self.RESET)
                elif is_new:
                    body.append(self.c("accent") + prefix + self.RESET)
                else:
                    body.append(self.c("ink") + prefix + self.RESET)
        body.extend([""] * (list_h - len(body)))
        lines.extend(body[:list_h])
        lines.append(self._rule(w))
        if self.sessions_notice:
            lines.append(self.c("ok") + _clip(" " + self.sessions_notice, w) + self.RESET)
        else:
            cur = items[self.sessions_index] if 0 <= self.sessions_index < len(items) else {}
            hint = f" {cur.get('title', '')}" if cur else " （空）"
            if cur and cur.get("action") != "new":
                hint += f" · {cur.get('message_count', 0)}条消息 · {cur.get('updated_at', '')}"
            lines.append(self.c("dim") + _clip(hint, w) + self.RESET)
        lines.append(self._rule(w))
        lines.append(
            self.c("accent")
            + _pad(_clip(f"→{self.project_name} · {self.model_name} · {policy.MODE_LABELS.get(self.mode, self.mode)}", w), w)
            + self.RESET
        )
        while len(lines) < h:
            lines.append(" ")
        return lines[:h]

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
        cur = self.settings_scratch.get(key, field.get("current"))
        idx = options.index(cur) if cur in options else 0
        self.settings_scratch[key] = options[(idx + direction) % len(options)]
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
            # 即时同步策略源（config.mode 读 data.ui.mode）；落盘随设置页"保存"完成
            if self.config is not None:
                self.config.mode = str(self.settings_scratch[key])

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

    # ----- 设置：插件标签页（改动即时写透 config.plugins_config，不经 scratch）-----

    def _plugin_current_field(self) -> dict:
        fields = self.settings_fields or []
        if not fields:
            return {}
        self._clamp_settings_index()
        return fields[self.settings_index]

    def _settings_plugin_adjust(self, config, direction: int) -> bool:
        """←→：插件行=启停（写盘+重载）、pbool=勾选、pchoice=切换选项。
        处理了插件类字段返回 True（调用方跳过通用 choice 循环并重建字段）。"""
        field = self._plugin_current_field()
        ftype = field.get("type")
        if ftype == "plugin":
            self._settings_plugin_toggle_enable(config, field)
            return True
        if ftype == "pbool":
            self._settings_plugin_toggle_bool(config, field)
            return True
        if ftype == "pchoice":
            self._settings_plugin_cycle_choice(config, field, direction)
            return True
        return False

    def _settings_plugin_toggle_enable(self, config, field) -> None:
        from core import plugins as plugins_mod

        pid = field.get("pid")
        target = field.get("current") is not True
        if not plugins_mod.set_enabled(pid, target, config):
            self.settings_notice = f"插件 {pid} 状态写入失败"
            return
        counts = plugins_mod.reload(config)
        row = next((r for r in plugins_mod.statuses() if r["id"] == pid), {})
        status = row.get("status")
        if target and status != "loaded":
            self.settings_notice = f"插件 {pid} 启用后仍为[{status}]：{row.get('error') or '未知原因'}"
        else:
            self.settings_notice = (
                f"插件 {pid} 已{'启用' if target else '禁用'} · 重载："
                f"装载{counts['loaded']} 禁用{counts['disabled']} 失败{counts['failed']}"
            )

    def _settings_plugin_toggle_bool(self, config, field) -> None:
        from core import plugins as plugins_mod

        pid, key = field.get("pid"), field.get("ckey")
        cur = bool(plugins_mod.get_setting(pid, key, None, config) in (True, "true", "1", 1))
        val = plugins_mod.set_setting(pid, key, not cur, config)
        self.settings_notice = f"{field.get('label', key)} = {'开' if val else '关'}"

    def _settings_plugin_cycle_choice(self, config, field, direction: int) -> None:
        from core import plugins as plugins_mod

        options = [str(o) for o in (field.get("options") or [])]
        if not options:
            return
        pid, key = field.get("pid"), field.get("ckey")
        cur = plugins_mod.get_setting(pid, key, None, config)
        cur_s = "" if cur is None else str(cur)
        idx = options.index(cur_s) if cur_s in options else 0
        nxt = options[(idx + direction) % len(options)]
        plugins_mod.set_setting(pid, key, nxt, config)
        self.settings_notice = f"{field.get('label', key)} = {nxt}"

    def _settings_plugin_expand(self, field) -> None:
        pid = field.get("pid")
        expanded = getattr(self, "_plugin_expanded", set())
        if pid in expanded:
            expanded.discard(pid)
            self.settings_notice = f"已收起 {field.get('label', pid)} 的配置"
        else:
            expanded.add(pid)
            self.settings_notice = f"已展开 {field.get('label', pid)} 的配置"
        self._plugin_expanded = expanded

    def _settings_plugin_edit_text(self, config, field) -> None:
        """str/int/float 配置：Enter 占用输入行编辑；int/float 解析失败保留原值"""
        from core import plugins as plugins_mod

        pid, key = field.get("pid"), field.get("ckey")
        decl = field.get("decl") or {}
        label = field.get("label", key)
        cur = plugins_mod.get_setting(pid, key, None, config)
        self.settings_mode = False
        self.render()
        try:
            raw = _paused_input(f"{label} [{'' if cur is None else cur}]: ")
        except (EOFError, KeyboardInterrupt):
            raw = ""
        self.settings_mode = True
        if raw == "":
            self.settings_notice = f"{label} 未修改"
            return
        ctype = decl.get("type", "str")
        try:
            coerced = int(float(raw)) if ctype == "int" else (float(raw) if ctype == "float" else raw)
        except (TypeError, ValueError):
            self.settings_notice = f"{label} 需要{'整数' if ctype == 'int' else '小数'}，已保留原值 {cur}"
            return
        try:
            val = plugins_mod.set_setting(pid, key, coerced, config)
        except ValueError as e:
            self.settings_notice = str(e)
            return
        self.settings_notice = f"{label} 已保存 = {val}"

    def _settings_run_action(self, config, action: str) -> None:
        """添加提供商/模型：占用输入行读入 id"""
        if action == "add_provider":
            self.settings_mode = False
            self.render()
            pid = _paused_input("新 provider_id: ").strip()
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
            mid = _paused_input(f"新 model_id ({pid}): ").strip()
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
        config.add_or_update_provider(provider)
        # 激活提供商
        self._settings_apply_active_provider(config)
        config.apply_active()
        # 模型
        select_model = s.get("select_model") or self.settings_model_name
        row = config.find_model(select_model) if select_model else None
        if row:
            model_updates = {
                "model_id": str(s.get("model_id") or row.get("model_id")),
                "modalities": [x.strip() for x in str(s.get("modalities") or "text").split(",") if x.strip()],
                "thinking_effort": str(s.get("thinking_effort") or "none"),
            }
            try:
                model_updates["context_window"] = int(float(s.get("context_window", row.get("context_window", 0))))
                model_updates["max_tokens"] = int(float(s.get("max_tokens", row.get("max_tokens", 0))))
                model_updates["temperature"] = float(s.get("model_temperature", row.get("temperature", 1.0)))
            except (TypeError, ValueError):
                pass
            config.update_model(row["provider_id"], row["model_id"], model_updates)
            config.data["active_provider_id"] = row["provider_id"]
            config.data["active_model_id"] = model_updates.get("model_id", row["model_id"])
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
        if "busy_send_mode" in s and str(s["busy_send_mode"]) in ("queue", "interrupt"):
            ui_cfg["busy_send_mode"] = str(s["busy_send_mode"])
        if "logo" in s:
            ui_cfg["logo"] = bool(s["logo"] in (True, "true", "1", 1) if isinstance(s["logo"], str) else bool(s["logo"]))
        for key, cast in (
            ("font_size", int),
            ("tip_interval", float),
            ("scroll_v0", float),
            ("scroll_hold_ms", int),
            ("scroll_max_step", int),
        ):
            if key in s and s[key] is not None and str(s[key]) != "":
                try:
                    ui_cfg[key] = cast(float(s[key]))
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
        if "compress_keep_recent_tokens" in s:
            try:
                mem_cfg["compress_keep_recent_tokens"] = max(0, int(float(s["compress_keep_recent_tokens"])))
            except (TypeError, ValueError):
                pass
        if "compress_summary_max_tokens" in s:
            try:
                mem_cfg["compress_summary_max_tokens"] = max(100, int(float(s["compress_summary_max_tokens"])))
            except (TypeError, ValueError):
                pass
        if "tool_whitelist" in s:
            ctx_cfg = config.data.setdefault("context", {})
            names = [p.strip() for p in str(s["tool_whitelist"] or "").split(",") if p.strip()]
            ctx_cfg["tool_whitelist"] = names
        wf_cfg = config.data.setdefault("workflow", {})
        if "workflow_enabled" in s:
            raw = s["workflow_enabled"]
            wf_cfg["enabled"] = bool(raw in (True, "true", "1", 1) if isinstance(raw, str) else bool(raw))
        if "workflow_active" in s and s["workflow_active"]:
            from core import workflow as workflow_mod

            workflow_mod.set_active(config, str(s["workflow_active"]))
        if "max_tool_rounds" in s:
            try:
                wf_cfg["max_rounds"] = max(0, int(float(s["max_tool_rounds"])))
            except (TypeError, ValueError):
                pass
        if "spin_kill_count" in s:
            try:
                wf_cfg["spin_kill_count"] = max(1, int(float(s["spin_kill_count"])))
            except (TypeError, ValueError):
                pass
        if "max_tool_timeout" in s:
            tools_cfg = config.data.setdefault("tools", {})
            try:
                tools_cfg["max_timeout"] = max(1, int(float(s["max_tool_timeout"])))
            except (TypeError, ValueError):
                pass
        if "ask_user_timeout" in s:
            tools_cfg = config.data.setdefault("tools", {})
            raw = s["ask_user_timeout"]
            tools_cfg["ask_user_timeout"] = bool(raw in (True, "true", "1", 1) if isinstance(raw, str) else bool(raw))
        if "log_level" in s and s["log_level"]:
            level = str(s["log_level"]).strip()
            level = {"关闭": "off"}.get(level, level.lower())
            if level in ("debug", "info", "warn", "error", "off"):
                log_cfg = config.data.setdefault("log", {})
                if log_cfg.get("level") != level:
                    log_cfg["level"] = level
                    init_logging(config.data.get("log") or {})
        config.apply_active()
        config.save()
        updates["model_name"] = config.model_name
        updates["task_models"] = config.task_models()
        return updates


# ----- 输入栈：core.keyinput（prompt_toolkit 后端，旧实现在 _recycle/） -----

def _flush_input():
    from core import keyinput

    keyinput.flush_input()


def _paused_input(prompt: str) -> str:
    """暂停输入泵并恢复行缓冲后用内建 input() 读一行（设置页文本项等场景）。

    新输入栈 raw_mode 关闭了行缓冲/回显，且后台泵线程会抽干控制台输入，
    内建 input() 必须在 keyinput.paused() 内使用。"""
    from core import keyinput

    with keyinput.paused():
        return input(prompt)


def _key_direction(kind: str, value: str) -> Optional[str]:
    """归一化方向键事件：kind=up… 或 hotkey=ctrl+up 均映射为 up/down/left/right。"""
    from core import keyinput

    return keyinput.direction_of(kind, value)


def _read_key():
    """输入统一走 core.keyinput"""
    from core import keyinput

    return keyinput.read_key_event()


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
