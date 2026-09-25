"""显示宽度、逻辑行增量软换行、光标映射。

复杂度：char_width 备忘 O(1)；单键重折 O(该逻辑行)；光标映射 O(行首到光标)。
"""
from __future__ import annotations

import unicodedata
from typing import Dict, List, Optional, Tuple

_WIDTH_CACHE: Dict[str, int] = {}


def char_width(ch: str) -> int:
    if not ch:
        return 0
    w = _WIDTH_CACHE.get(ch)
    if w is not None:
        return w
    if unicodedata.combining(ch):
        w = 0
    elif unicodedata.east_asian_width(ch) in ("F", "W"):
        w = 2
    else:
        w = 1
    # 缓存有界：简单字典足够（输入框字符集有限）
    if len(_WIDTH_CACHE) > 4096:
        _WIDTH_CACHE.clear()
    _WIDTH_CACHE[ch] = w
    return w


def display_width(text: str) -> int:
    return sum(char_width(c) for c in (text or ""))


def split_at_cells(text: str, cut: int) -> Tuple[str, str]:
    """按显示列宽切开；cut 为左段应占单元格数。宽字符不折半。"""
    if cut <= 0:
        return "", text or ""
    src = text or ""
    used = 0
    for i, ch in enumerate(src):
        w = char_width(ch)
        if used + w > cut:
            return src[:i], src[i:]
        used += w
        if used == cut:
            return src[: i + 1], src[i + 1 :]
    return src, ""


def wrap_line(text: str, width: int) -> List[str]:
    """单条逻辑行按显示宽度贪心折断；宽字符不折半。"""
    if width <= 0:
        return [""]
    if not text:
        return [""]
    lines: List[str] = []
    cur, used = "", 0
    for ch in text:
        w = char_width(ch)
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
    """多行输入布局：按逻辑行缓存 visual 行，编辑只重折脏行。"""

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
                self._visuals[i] = wrap_line(self._paras[i], self.width)
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
        wrapped = self._visuals[para_index] if para_index < len(self._visuals) else wrap_line(para, self.width)
        if not wrapped:
            wrapped = [""]
        # 重建前缀折行以定位（仅短前缀）
        if prefix:
            pre_wrapped = wrap_line(prefix, self.width)
            row_in_para = len(pre_wrapped) - 1
            cells = display_width(pre_wrapped[-1]) if pre_wrapped else 0
        else:
            row_in_para = 0
            cells = 0
        # 绝对 visual 行 = 前面逻辑行的 visual 行数 + row_in_para
        visual_row = 0
        for k in range(para_index):
            vs = self._visuals[k] if k < len(self._visuals) else [""]
            visual_row += max(1, len(vs))
        visual_row += row_in_para
        # 校正：wrap_line 与 pre_wrapped 在恰好贴边时可能差一行
        if wrapped and row_in_para >= len(wrapped):
            row_in_para = len(wrapped) - 1
            visual_row = sum(max(1, len(self._visuals[k] if k < len(self._visuals) else [""])) for k in range(para_index)) + row_in_para
            cells = display_width(prefix) - display_width("".join(wrapped[:row_in_para]))
            cells = max(0, cells)
        return visual_row, cells

    def window(self, start_row: int, height: int) -> List[str]:
        rows = self.visual_rows()
        start = max(0, min(start_row, max(0, len(rows) - 1)))
        vis = rows[start : start + max(1, height)]
        return list(vis) + [""] * max(0, height - len(vis))

    def total_rows(self) -> int:
        return sum(max(1, len(vs)) for vs in self._visuals) if self._visuals else 1
