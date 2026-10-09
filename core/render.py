# -*- coding: utf-8 -*-
"""渲染器：主题/ANSI 工具、会话树与分区布局、滚动条、选区、代码高亮。
渲染方法以混入类 RenderMixin。TuiApp 的状态由 ui.py 持有，本模块只读取/产出屏幕内容。
"""
from __future__ import annotations

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

from pygments.lexers import get_lexer_for_filename as _pyg_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound as _PygClassNotFound
HAS_PYGMENTS = True
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
from core.log import get_logger

log = get_logger("render")

from core.llm import CANCELLED as CANCEL_RESULT

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
    "脑子加载中...", "整理上下文", "翻找思路...", "灵感加载中...",
)

_HINT_ROTATE_SECONDS = 4.0

# 树底阶段文案池：key=「 · 」前的阶段基名，值=按时间槽轮换的文案元组（帧循环无状态、线程安全）；
# 带后缀的阶段（如「工具调用中 · read」）轮换基名文案、保留后缀；未登记的阶段按原样显示
_PHASE_POOLS = {
    "思考中": _THINKING_PHRASES,
    "工具调用中": ("调用工具中", "等待工具返回", "处理工具输出"),
    "待确认": ("等待确认", "等待你的决定"),
}

# 控制台鼠标 y → compose 行号偏移
_MOUSE_Y_OFFSET = 0

# 工具显示分类：读取类保持概要；命令类显示输入命令 + 折叠输出；写入/编辑类显示高亮全文/diff
_TOOL_CMD = {"execute_command", "run_program"}

_TOOL_WRITE = {"write"}

_TOOL_EDIT = {"edit_file", "multi_edit"}

_TOOL_CMD_OUTPUT_LINES = 3  # 命令类展开时显示的输出行数（超出折叠为「还有 N 行」）

# diff 行底色（叠在关键词高亮前景之上）：红删 / 绿增
_DIFF_DEL_BG = (58, 26, 30)

_DIFF_ADD_BG = (26, 54, 34)

# 流式思考滚动窗口行数；完成后落库思考固定折叠为一行
_STREAM_THINKING_LINES = 5

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

def _wrap_keep_ansi(text: str, width: int) -> List[str]:
    """按显示宽度折行并保留 ANSI（续行重放当前 SGR 前景色）；供语法高亮/diff 行折行使用"""
    if width <= 0:
        return [""]
    src = (text or "").replace("\r", "").replace("\n", " ")
    rows: List[str] = []
    cur: List[str] = []
    used = 0
    active = ""  # 当前前景色 SGR
    i = 0
    while i < len(src):
        if src[i] == "\x1b":
            m = _ANSI_RE.match(src, i)
            if m:
                code = m.group(0)
                cur.append(code)
                if code.endswith("m"):
                    if code in ("\x1b[0m", "\x1b[m", "\x1b[39m", "\x1b[49m"):
                        active = ""
                    else:
                        active = code
                i = m.end()
                continue
        ch = src[i]
        w = _char_width(ch)
        if used + w > width and cur:
            cur.append("\033[0m")
            rows.append("".join(cur))
            cur = [active] if active else []
            used = 0
        cur.append(ch)
        used += w
        i += 1
    cur.append("\033[0m")
    rows.append("".join(cur))
    return rows or [""]

def _lexer_for(filename: str):
    """按文件名/扩展名取 pygments 词法；无 pygments 或未知类型返回 None（降级纯文本）"""
    if not HAS_PYGMENTS or not filename:
        return None
    try:
        return _pyg_lexer_for_filename(filename)
    except _PygClassNotFound:
        return None
    except Exception:
        return None

@functools.lru_cache(maxsize=128)
def _kw_lines(filename: str, code: str, kw_fg: str, base_fg: str) -> Tuple[str, ...]:
    """代码 → 每行仅关键词着色的字符串；其余字符不着色，由外层主题色决定。

    关键词取 pygments 的 Token.Keyword，着色后立即恢复 base_fg；行数与 splitlines 对齐。"""
    if not code:
        return ()
    want = code.splitlines() or [""]
    lexer = _lexer_for(filename)
    if lexer is None:
        return tuple(want)
    try:
        parts: List[str] = []
        for ttype, value in lexer.get_tokens(code):
            if not value:
                continue
            if ttype in Token.Keyword:
                parts.append(kw_fg + value + base_fg)
            else:
                parts.append(value)
        lines = "".join(parts).split("\n")
    except Exception:
        return tuple(want)
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != len(want):
        return tuple(want)  # 行数错位：宁可不带高亮也不串行
    return tuple(lines)

@functools.lru_cache(maxsize=64)
def _hl_rows_wrapped(filename: str, code: str, width: int, kw_fg: str, base_fg: str, dim_fg: str) -> Tuple[str, ...]:
    """代码全文 → 行号 + 仅关键词高亮 + 按宽度折行的渲染行。

    行号右对齐定宽、后接 │ 分隔；折行续行留等宽空白，内容列与行号列对齐。"""
    src = code.splitlines() or [""]
    num_w = len(str(len(src)))
    lead_w = num_w + 3  # "NN │ "
    code_w = max(1, width - lead_w)
    pad = " " * lead_w
    rows: List[str] = []
    for i, line in enumerate(_kw_lines(filename, code, kw_fg, base_fg) or tuple(src), 1):
        pieces = _wrap_keep_ansi(line, code_w)
        for k, piece in enumerate(pieces):
            head = f"{str(i).rjust(num_w)} {dim_fg}│{base_fg} " if k == 0 else pad
            rows.append(head + piece)
    return tuple(rows)


@functools.lru_cache(maxsize=64)
def _diff_wrapped(filename: str, old: str, new: str, code_w: int, kw_fg: str, base_fg: str) -> Tuple[Tuple[str, int, Tuple[str, ...]], ...]:
    """old→new 统一 diff → ((标记, 行号, 折行后的行元组), ...)。

    标记 ' ' / '-' / '+'；行号删除行取旧文件、其余取新文件；代码仅关键词高亮。
    code_w 为内容折行宽度，不含行号与标记列。"""
    old_l = old.splitlines() if old else []
    new_l = new.splitlines() if new else []
    old_hl = list(_kw_lines(filename, old, kw_fg, base_fg)) if old else []
    new_hl = list(_kw_lines(filename, new, kw_fg, base_fg)) if new else []
    if len(old_hl) != len(old_l):
        old_hl = old_l
    if len(new_hl) != len(new_l):
        new_hl = new_l
    items: List[Tuple[str, int, Tuple[str, ...]]] = []

    def _emit(marker: str, hl: List[str], idx: int, lineno: int) -> None:
        text = hl[idx] if 0 <= idx < len(hl) else ""
        items.append((marker, lineno, tuple(_wrap_keep_ansi(text, code_w))))

    sm = difflib.SequenceMatcher(a=old_l, b=new_l, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                _emit(" ", new_hl, j1 + (k - i1), j1 + (k - i1) + 1)
        elif tag == "delete":
            for k in range(i1, i2):
                _emit("-", old_hl, k, k + 1)
        elif tag == "insert":
            for k in range(j1, j2):
                _emit("+", new_hl, k, k + 1)
        elif tag == "replace":
            for k in range(i1, i2):
                _emit("-", old_hl, k, k + 1)
            for k in range(j1, j2):
                _emit("+", new_hl, k, k + 1)
    return tuple(items)

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
    __slots__ = ("id", "label", "kind", "children", "default_expanded", "summary", "detail", "meta")

    def __init__(self, id, label, kind="text", children=None, default_expanded=True, summary="", detail="", meta=None):
        self.id = id
        self.label = label
        self.kind = kind
        self.children = children or []
        self.default_expanded = default_expanded
        self.summary = summary
        self.detail = detail
        self.meta = meta


class RenderMixin:
    """渲染器混入：会话树/分区布局/滚动条/选区/代码高亮等（由 TuiApp 继承）。"""

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

        # 工具调用索引：结果消息不带参数，按 tool_call_id 关联到 tool_call 信封。
        # cmd_heads：execute_command/run_program 的简洁命令头（结果/信封节点显示）；
        # call_info：完整参数（命令/写入内容/编辑 old-new），供命令/写入/编辑类渲染正文
        cmd_heads: Dict[str, str] = {}
        call_info: Dict[str, dict] = {}
        for item in self.messages[-200:]:
            if item.get("type") != "tool_call":
                continue
            for c in item.get("tool_calls") or []:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or "")
                if not cid:
                    continue
                name = str(c.get("name") or "")
                args = c.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                call_info[cid] = {"name": name, "args": args}
                head = ""
                if name == "execute_command":
                    head = _command_head(memory_mod.content_text(args.get("command")))
                elif name == "run_program":
                    rest = " ".join(str(a) for a in (args.get("args") or [])[:2])
                    head = _command_head((str(args.get("program") or "") + " " + rest).strip())
                if head:
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
                    cid = str(item.get("tool_call_id") or "")
                    info = call_info.get(cid)
                    # 命令/写入/编辑类：附元数据，展开时按类型渲染（命令+输出 / 高亮全文 / 高亮 diff）
                    meta = None
                    if info is not None:
                        meta = self._tool_display_meta(
                            str(info.get("name") or tool_name), info.get("args") or {}, content, failed
                        )
                    if meta is not None:
                        base = tool_name or str((info or {}).get("name") or "") or "tool"
                        if meta.get("tool_kind") == "cmd":
                            label = f"⚙ {cmd_heads.get(cid) or base} {icon}"
                        else:
                            path = str(meta.get("path") or "")
                            label = f"⚙ {base} {path} {icon}" if path else f"⚙ {base} {icon}"
                        node = TreeNode(
                            f"msg:{index}",
                            label,
                            "tool",
                            default_expanded=True,
                            detail="",
                            meta=meta,
                        )
                    else:
                        # 读取类等：保持概要显示（标题 + 结果正文，默认折叠可展开）
                        result_text = content.strip()
                        display = cmd_heads.get(cid) or tool_name or "tool"
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
                # 已完成思考：落库 thinking 字段固定折叠为一行，排在本轮正文/工具节点之前
                think = self._thinking_node(index, item)
                if think is not None:
                    _parent().children.append(think)
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
                # 已完成思考（无工具调用的最终回复）：固定一行摘要，排在回复节点之前
                think = self._thinking_node(index, item)
                if think is not None:
                    _parent().children.append(think)
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

    def _thinking_node(self, index: int, item: dict) -> Optional[TreeNode]:
        """已完成思考 → 摘要节点：收起为一行摘要，展开显示完整思考内容"""
        thinking = str(item.get("thinking") or "").strip()
        if not thinking:
            return None
        summary = next((ln.strip() for ln in thinking.splitlines() if ln.strip()), thinking)
        return TreeNode(
            f"think:{index}",
            f"💭 思考 · {_oneline(summary, 60)}",
            "thinking",
            default_expanded=False,
            detail=thinking,
        )

    def _tool_display_meta(self, name: str, args: dict, content: str, failed: bool) -> Optional[dict]:
        """命令/写入/编辑类工具 → 展开正文元数据；读取类或结构缺失返回 None（保持概要）

        写入/编辑失败时返回 None，回落显示结果正文（避免展示未生效的内容/diff）。"""
        if name in _TOOL_CMD:
            if name == "execute_command":
                command = memory_mod.content_text(args.get("command"))
            else:  # run_program
                prog = str(args.get("program") or "")
                extra = " ".join(str(a) for a in (args.get("args") or []))
                command = (prog + " " + extra).strip()
            return {"tool_kind": "cmd", "command": command, "output": content}
        if name in _TOOL_WRITE and not failed:
            body = args.get("content")
            return {
                "tool_kind": "write",
                "path": str(args.get("file_path") or ""),
                "content": body if isinstance(body, str) else str(body or ""),
            }
        if name in _TOOL_EDIT and not failed:
            items: List[Tuple[str, str]] = []
            if name == "multi_edit":
                for e in args.get("edits") or []:
                    if isinstance(e, dict):
                        items.append((str(e.get("old_str") or ""), str(e.get("new_str") or "")))
            else:
                items.append((str(args.get("old_str") or ""), str(args.get("new_str") or "")))
            if not items:
                return None
            return {
                "tool_kind": "edit",
                "path": str(args.get("file_path") or ""),
                "items": items,
            }
        return None

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
            "thinking": self.c("thinking"),
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
                meta = node.meta
                if meta and meta.get("tool_kind") in ("cmd", "write", "edit"):
                    # 命令/写入/编辑类：标题行 + 展开正文（命令+折叠输出 / 高亮全文 / 高亮 diff）
                    title_part = f"{_marker(True, expanded)} {label}"
                    if abs_i == self.tree_cursor and tree_focus:
                        rows.append(self.C_HL + _pad(f"{indent}{title_part}", width) + self.RESET)
                    else:
                        rows.append(gseg + self.c("tool_title") + _clip(title_part, max(1, width - gw)) + self.RESET)
                    if expanded:
                        rows.extend(self._tool_detail_rows(meta, gseg, width, gw))
                    spans.append((span_start, len(rows) - span_start))
                    continue
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
                # 思考过程（流式进行中）：滚动显示尾部 N 行动态窗口（随追加上滚）
                win = _wrap(label, max(8, width - gw - 2))
                for wline in win[-_STREAM_THINKING_LINES:]:
                    rows.append(gseg + color + _clip("  " + wline, max(1, width - gw)) + self.RESET)
                spans.append((span_start, len(rows) - span_start))
                continue
            if node.kind == "thinking":
                # 已完成思考：默认收起为一行摘要，展开显示完整思考内容折行
                has = bool(node.detail)
                marker = _marker(has, expanded)
                if abs_i == self.tree_cursor and tree_focus:
                    rows.append(self.C_HL + _pad(_clip(f"{indent}{marker} {label}", width), width) + self.RESET)
                else:
                    rows.append(gseg + color + _clip(f"{marker} {label}", max(1, width - gw)) + self.RESET)
                if expanded and node.detail:
                    for dline in _wrap(str(node.detail), max(10, width - gw - 2)):
                        rows.append(gseg + self.c("dim") + _clip("  " + dline, max(1, width - gw)) + self.RESET)
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

    def _tool_detail_rows(self, meta: dict, gseg: str, width: int, gw: int) -> List[str]:
        """工具节点展开正文：命令（命令 + 折叠输出）/ 写入（行号 + 仅关键词高亮全文）/ 编辑（行号 + 仅关键词高亮 diff）

        gseg 为树引导线前缀，gw 为其显示宽度；正文再缩进 2 列。"""
        kind = meta.get("tool_kind")
        body_w = max(1, width - gw)
        ind = "  "
        out: List[str] = []
        if kind == "cmd":
            command = str(meta.get("command") or "")
            for k, ln in enumerate(_wrap(command, max(1, body_w - 2)) or [""]):
                prefix = "$ " if k == 0 else "  "
                out.append(gseg + self.c("dim") + _clip(ind + prefix + ln, body_w) + self.RESET)
            out_lines = str(meta.get("output") or "").strip().splitlines()
            for ln in out_lines[:_TOOL_CMD_OUTPUT_LINES]:
                out.append(gseg + self.c("tool_content") + _clip(ind + ln, body_w) + self.RESET)
            extra = len(out_lines) - _TOOL_CMD_OUTPUT_LINES
            if extra > 0:
                out.append(gseg + self.c("dim") + _clip(ind + f"… 还有 {extra} 行输出", body_w) + self.RESET)
        elif kind == "write":
            path = str(meta.get("path") or "")
            code = str(meta.get("content") or "")
            base_fg = self.c("tool_content")
            if not code:
                out.append(gseg + self.c("dim") + _clip(ind + "（空内容）", body_w) + self.RESET)
            else:
                rows = _hl_rows_wrapped(path, code, max(1, body_w - len(ind)), self.c("accent"), base_fg, self.c("dim"))
                for row in rows:
                    out.append(gseg + base_fg + ind + row + self.RESET)
        elif kind == "edit":
            path = str(meta.get("path") or "")
            items = meta.get("items") or []
            multi = len(items) > 1
            base_fg = self.c("tool_content")
            maxno = 1
            for old, new in items:
                maxno = max(maxno, len(str(old).splitlines()) or 1, len(str(new).splitlines()) or 1)
            num_w = len(str(maxno))
            lead_w = num_w + 5  # "NN │ + "
            pad = " " * lead_w
            code_w = max(1, body_w - len(ind) - lead_w)
            for gi, (old, new) in enumerate(items):
                if multi:
                    out.append(gseg + self.c("dim") + _clip(ind + f"—— 第 {gi + 1} 处 ——", body_w) + self.RESET)
                for marker, lineno, pieces in _diff_wrapped(path, str(old), str(new), code_w, self.c("accent"), base_fg):
                    if marker == "+":
                        mcol, bg = self.c("ok"), _bg(_DIFF_ADD_BG)
                    elif marker == "-":
                        mcol, bg = self.c("err"), _bg(_DIFF_DEL_BG)
                    else:
                        mcol, bg = self.c("dim"), ""
                    for k, piece in enumerate(pieces):
                        if k == 0:
                            lead = f"{str(lineno).rjust(num_w)} {self.c('dim')}│{base_fg} {mcol}{marker}{self.RESET} "
                        else:
                            lead = pad
                        out.append(gseg + base_fg + ind + lead + bg + piece + self.RESET)
        return out

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
        """整帧渲染：行级 diff 直写（替代 rich Live 整帧重写）。

        _compose_plain 产出带 ANSI 的行字符串（复用全部现有着色/裁剪/换行
        逻辑），与上帧按行比对，仅重写变化行——rich Live 的 LiveRender 无
        diff 每帧真实写整帧，输入/流式期整帧擦写是闪烁根因。
        """
        w, h = _term_size()
        lines = self._compose_plain(w, h)
        if len(lines) < h:
            lines = list(lines) + [""] * (h - len(lines))
        lines = lines[:h]
        last = self._last_lines
        if last is None or len(last) != h or self._last_w != w:
            # 首帧/尺寸变化：清屏后整帧重写（低频事件，resize 缩小时残留行必须清）
            parts = [f"\033[2J\033[H"] + [f"\033[{i + 1};1H\033[2K{_clip_keep_ansi(l, w)}" for i, l in enumerate(lines)]
        else:
            parts = [f"\033[{i + 1};1H\033[2K{_clip_keep_ansi(l, w)}"
                     for i, (o, l) in enumerate(zip(last, lines)) if o != l]
        if parts:
            sys.stdout.write("".join(parts))
            sys.stdout.flush()
        self._last_lines = lines
        self._last_w = w

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
