# -*- coding: utf-8 -*-
"""inline 渲染：已完成内容只打印一次进终端 scrollback，底部动态区按帧原地重画。

与全屏模式的区别：全屏每帧覆盖整屏并自绘滚动条；inline 把历史交给终端原生
滚动/选字，只维护底部若干行（流式尾部、阶段提示、输入框、状态栏）。
窗口尺寸变化、回退、切换会话时清屏并清 scrollback，再按当前消息重印全部历史。
设置面板与会话列表是全屏表单，打开期间临时进入备用屏。
"""
from __future__ import annotations

import sys
from typing import Callable, List, Optional, Sequence, Tuple

from core import memory as memory_mod
from core.render import TreeNode, _MD_FENCE_RE, _clip, _term_size, safe_text

SYNC_BEGIN = "\033[?2026h"
SYNC_END = "\033[?2026l"
RESET_SCREEN = "\033[2J\033[3J\033[H"
ALT_ENTER = "\033[?1049h\033[2J\033[H"
ALT_LEAVE = "\033[?1049l"

# 直播类节点只在动态区显示；fallback 节点会随新事件换位置，inline 下不显示
_DYNAMIC_PREFIXES = ("stream:", "subagent:live:", "mcp:")
_SKIP_PREFIXES = ("fallback:",)
_NO_DESCEND = {"subagent", "step"}
_COLLAPSED = {"thinking", "subagent", "step", "stream_thinking"}

# 默认折叠时工具节点打印进 scrollback 的行数上限，超出以指示行代替；全局展开不受限
TOOL_ROW_CAP = 8

Entry = Tuple[TreeNode, int, bool, str]


def stream_commit_point(text: str) -> int:
    """流式正文可提交前缀的字符数：止于围栏外空行或代码块结束行，未完成的末行不计"""
    in_fence = False
    pos = 0
    cut = 0
    for line in text.split("\n")[:-1]:
        pos += len(line) + 1
        if _MD_FENCE_RE.match(line):
            in_fence = not in_fence
            if not in_fence:
                cut = pos - 1
            continue
        if not in_fence and not line.strip():
            cut = pos - 1
    return cut


def message_sig(message: dict) -> tuple:
    return (
        message.get("role"),
        message.get("type"),
        message.get("tool_call_id"),
        memory_mod.content_text(message.get("content"))[:64],
    )


def prefix_intact(marks: Sequence[Tuple[dict, tuple]], timeline: Sequence[dict]) -> bool:
    """已打印消息是否仍是当前时间线的前缀；同一对象或签名一致都算未变"""
    if len(timeline) < len(marks):
        return False
    for (obj, sig), cur in zip(marks, timeline):
        if cur is not obj and message_sig(cur) != sig:
            return False
    return True


def redraw_sequence(prev_h: int, commit: Sequence[str], live: Sequence[str]) -> str:
    """回到动态区顶部清屏尾，先追加提交行进 scrollback，再画新的动态区；光标停在末行"""
    parts = [SYNC_BEGIN]
    if prev_h > 0:
        parts.append("\r")
        if prev_h > 1:
            parts.append(f"\033[{prev_h - 1}A")
        parts.append("\033[J")
    for row in commit:
        parts.append(row + "\033[0m\r\n")
    parts.append("\r\n".join(row + "\033[0m" for row in live))
    parts.append(SYNC_END)
    return "".join(parts)


def fit_live(rows: List[str], limit: int) -> List[str]:
    """动态区必须矮于屏幕，否则光标上移够不到顶行；超出时裁掉顶部保留输入区"""
    limit = max(1, limit)
    return rows[-limit:] if len(rows) > limit else rows


class InlineRenderer:
    def __init__(self, app, out=None, size: Callable[[], Tuple[int, int]] = _term_size) -> None:
        self.app = app
        self.out = out or sys.stdout
        self.size = size
        self._alt = False
        self._size_key: Optional[Tuple[int, int]] = None
        self._reprint = False
        self._expand_all = False
        self._last_live: Optional[List[str]] = None
        self._stream_cache: Tuple[str, int, List[str]] = ("", 0, [])
        self._reset_state()

    def _reset_state(self) -> None:
        self._live_h = 0
        self._committed = 0
        self._last_id = ""
        self._marks: List[Tuple[dict, tuple]] = []
        self._session_id: Optional[str] = None
        self._printed_any = False
        self._stream_text = ""
        self._stream_rows = 0

    def request_reprint(self) -> None:
        self._reprint = True

    def toggle_expand_all(self) -> bool:
        """Alt+E：展开全部折叠节点并重印历史；再按一次回到默认折叠"""
        self._expand_all = not self._expand_all
        self.request_reprint()
        return self._expand_all

    def close(self) -> str:
        """退出时的收尾序列：离开备用屏并擦掉动态区，scrollback 中的对话保留"""
        parts = []
        if self._alt:
            parts.append(ALT_LEAVE)
            self._alt = False
        if self._live_h:
            parts.append("\r" + (f"\033[{self._live_h - 1}A" if self._live_h > 1 else "") + "\033[J")
        self._live_h = 0
        return "".join(parts)

    def render(self) -> None:
        app = self.app
        w, h = self.size()
        if app.settings_mode or app.sessions_mode:
            if not self._alt:
                self.out.write(ALT_ENTER)
                self._alt = True
                app._last_lines = None
            app._render_fullscreen()
            return
        parts: List[str] = []
        if self._alt:
            parts.append(ALT_LEAVE)
            self._alt = False
            self._last_live = None
        width = max(40, w - 1)
        if self._size_key is not None and self._size_key != (w, h):
            self._reprint = True
        self._size_key = (w, h)

        timeline = self._timeline()
        static, dynamic = self._entries()
        session_id = str(getattr(getattr(app, "_tree_session", None), "session_id", "") or "")
        if not self._reprint and not self._history_intact(timeline, static, session_id):
            self._reprint = True
        if self._reprint:
            parts.append(RESET_SCREEN)
            self._reset_state()
            self._reprint = False
            self._last_live = None
        self._session_id = session_id

        commit = self._commit_static(static, width)
        self._marks.extend((m, message_sig(m)) for m in timeline[len(self._marks):])
        stream_commit, stream_live = self._stream(width)
        commit.extend(stream_commit)

        think = [e for e in dynamic if e[0].id == "stream:thinking"]
        others = [e for e in dynamic if e[0].id not in ("stream:thinking", "stream:content")]
        footer, _ = app._compose_footer(width, h)
        live = self._rows(think, width, cap=False) + stream_live + self._rows(others, width, cap=False) + footer
        live = fit_live(live, h - 1)

        if not commit and not parts and live == self._last_live:
            return
        parts.append(redraw_sequence(self._live_h, commit, live))
        self.out.write("".join(parts))
        self.out.flush()
        self._live_h = len(live)
        self._last_live = live

    def _timeline(self) -> List[dict]:
        app = self.app
        archive = [item for _, _, item in getattr(app, "_tree_archive_messages", []) if item.get("type") != "summary"]
        return archive + [m for m in app.messages if m.get("type") != "summary"]

    def _history_intact(self, timeline: List[dict], static: List[Entry], session_id: str) -> bool:
        if self._session_id is not None and session_id != self._session_id:
            return False
        if self._committed > len(static):
            return False
        if self._committed and static[self._committed - 1][0].id != self._last_id:
            return False
        return prefix_intact(self._marks, timeline)

    def _entries(self) -> Tuple[List[Entry], List[Entry]]:
        """按固定展开规则展平会话树：每条消息产出的节点数稳定，已打印的节点可按计数跳过"""
        app = self.app
        app._flatten_tree()
        roots = app._tree_nodes or []
        static: List[Entry] = []
        dynamic: List[Entry] = []

        def walk(node: TreeNode, depth: int, dyn: bool) -> None:
            if node.id.startswith(_SKIP_PREFIXES):
                return
            dyn = dyn or node.id.startswith(_DYNAMIC_PREFIXES)
            if self._expand_all:
                expanded = True
            elif node.kind == "tool":
                expanded = not node.summary
            else:
                expanded = node.kind not in _COLLAPSED
            (dynamic if dyn else static).append((node, depth, expanded, "  " * max(0, depth - 1)))
            if self._expand_all or node.kind not in _NO_DESCEND:
                for child in node.children:
                    walk(child, depth + 1, dyn)

        if roots:
            for child in roots[0].children:
                walk(child, 1, False)
            for extra in roots[1:]:
                walk(extra, 1, True)
        return static, dynamic

    def _rows(self, entries: List[Entry], width: int, cap: bool = True) -> List[str]:
        if not entries:
            return []
        app = self.app
        rows, spans = app._rows_for_flat(entries, width, force_open=self._expand_all)
        # 全局展开时不再截工具输出的行数
        tool_cap = TOOL_ROW_CAP if (cap and not self._expand_all) else 0
        out: List[str] = []
        for (node, depth, _expanded, _guide), (start, count) in zip(entries, spans):
            chunk = rows[start : start + count]
            if tool_cap and node.kind == "tool" and len(chunk) > tool_cap + 1:
                hidden = len(chunk) - tool_cap
                chunk = chunk[:tool_cap] + [app.c("dim") + _clip(f"    … (+{hidden} 行)", width) + app.RESET]
            if cap and depth == 1 and node.kind == "user" and self._printed_any:
                chunk = [""] + chunk
            if cap and chunk:
                self._printed_any = True
            out.extend(chunk)
        return out

    def _commit_static(self, static: List[Entry], width: int) -> List[str]:
        out: List[str] = []
        for entry in static[self._committed :]:
            node, depth = entry[0], entry[1]
            if node.kind == "user" and depth == 1:
                self._stream_text, self._stream_rows = "", 0
            if self._stream_text:
                # 已分段打印过的流式回复：跳过其思考摘要，正文只补打未提交的部分
                if node.kind == "thinking":
                    continue
                if node.kind == "assistant":
                    final = node.label.partition(" · ")[2].lstrip()
                    # 已提交前缀带换行结尾，最终消息常被去掉尾部空白，比较时忽略
                    if final.startswith(self._stream_text.rstrip()):
                        # 用流式同一渲染器出全文再切掉已打印行，保证折行与已提交部分对齐
                        rows = self._stream_render(final, width)[self._stream_rows :]
                    else:
                        rows = self._rows([entry], width)
                    self._stream_text, self._stream_rows = "", 0
                    out.extend(rows)
                    continue
            out.extend(self._rows([entry], width))
        self._committed = len(static)
        if static:
            self._last_id = static[-1][0].id
        return out

    def _stream_render(self, text: str, width: int) -> List[str]:
        node = TreeNode("stream:inline", f"Agent · {text}", "assistant")
        rows, _ = self.app._rows_for_flat([(node, 2, True, "  ")], width)
        return rows

    def _stream(self, width: int) -> Tuple[List[str], List[str]]:
        """流式正文：完成的块提交进 scrollback，未完成的尾部留在动态区"""
        msg = self.app.streaming_msg if isinstance(self.app.streaming_msg, dict) else None
        text = safe_text(str(msg.get("content") or "")).lstrip() if msg else ""
        if not text:
            return [], []
        if self._stream_text and not text.startswith(self._stream_text):
            self._stream_text, self._stream_rows = "", 0
        commit: List[str] = []
        cut = stream_commit_point(text)
        if cut > len(self._stream_text):
            prefix_rows = self._stream_render(text[:cut], width)
            commit = prefix_rows[self._stream_rows :]
            self._stream_text, self._stream_rows = text[:cut], len(prefix_rows)
            if commit:
                self._printed_any = True
        cached_text, cached_w, cached_rows = self._stream_cache
        if cached_text != text or cached_w != width:
            cached_rows = self._stream_render(text, width)
            self._stream_cache = (text, width, cached_rows)
        return commit, cached_rows[self._stream_rows :]
