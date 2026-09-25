"""单光标多行文本：Gap Buffer。

    [ left | ........gap........ | right ]
    光标 = gap 左沿（字符下标 = gap_start）

复杂度：光标处插/删 O(1) 摊还；批量粘贴 O(n)；to_text 缓存后 O(1)。
"""
from __future__ import annotations

from typing import List, Optional, Tuple


class TextBuffer:
    __slots__ = ("_data", "_gap_start", "_gap_end", "_version", "_text_cache")

    def __init__(self, text: str = "") -> None:
        gap = max(64, len(text))
        self._data: List[str] = [""] * gap + list(text)
        self._gap_start = 0
        self._gap_end = gap
        self._version = 0
        self._text_cache: Optional[str] = None

    # ----- 基本查询 -----
    @property
    def version(self) -> int:
        return self._version

    @property
    def cursor(self) -> int:
        return self._gap_start

    @property
    def length(self) -> int:
        return len(self._data) - (self._gap_end - self._gap_start)

    def __len__(self) -> int:
        return self.length

    def __bool__(self) -> bool:
        return self.length > 0

    def __str__(self) -> str:
        return self.to_text()

    def char_at(self, index: int) -> str:
        return self._data[self._to_raw(index)]

    def to_text(self) -> str:
        if self._text_cache is not None:
            return self._text_cache
        s = "".join(self._data[: self._gap_start]) + "".join(self._data[self._gap_end :])
        self._text_cache = s
        return s

    def to_list(self) -> List[str]:
        return list(self.to_text())

    def set_text(self, text: str, cursor: Optional[int] = None) -> None:
        gap = max(64, len(text))
        self._data = [""] * gap + list(text)
        self._gap_start = 0
        self._gap_end = gap
        if cursor is not None:
            self.set_cursor(cursor)
        self._bump()

    def clear(self) -> None:
        self.set_text("")

    # ----- 光标 -----
    def set_cursor(self, index: int) -> None:
        index = max(0, min(self.length, int(index)))
        self._move_gap(index)

    def move_left(self, n: int = 1) -> None:
        self.set_cursor(self._gap_start - max(0, n))

    def move_right(self, n: int = 1) -> None:
        self.set_cursor(self._gap_start + max(0, n))

    def move_home(self) -> None:
        """行首（当前逻辑行）"""
        text = self.to_text()
        i = self._gap_start
        while i > 0 and text[i - 1] != "\n":
            i -= 1
        self.set_cursor(i)

    def move_end(self) -> None:
        """行尾（当前逻辑行）"""
        text = self.to_text()
        i = self._gap_start
        n = self.length
        while i < n and text[i] != "\n":
            i += 1
        self.set_cursor(i)

    def move_doc_home(self) -> None:
        self.set_cursor(0)

    def move_doc_end(self) -> None:
        self.set_cursor(self.length)

    def word_left(self) -> None:
        text = self.to_text()
        i = self._gap_start
        while i > 0 and text[i - 1].isspace():
            i -= 1
        while i > 0 and not text[i - 1].isspace():
            i -= 1
        self.set_cursor(i)

    def word_right(self) -> None:
        text = self.to_text()
        i = self._gap_start
        n = self.length
        while i < n and not text[i].isspace():
            i += 1
        while i < n and text[i].isspace():
            i += 1
        self.set_cursor(i)

    def word_start(self) -> int:
        """光标前词起点（补全用）"""
        text = self.to_text()
        i = self._gap_start
        while i > 0 and not text[i - 1].isspace():
            i -= 1
        return i

    def word_end(self) -> int:
        text = self.to_text()
        i = self._gap_start
        n = self.length
        while i < n and not text[i].isspace():
            i += 1
        return i

    # ----- 编辑 -----
    def insert(self, s: str) -> None:
        if not s:
            return
        self._ensure_gap(len(s))
        for i, ch in enumerate(s):
            self._data[self._gap_start + i] = ch
        self._gap_start += len(s)
        self._bump()

    def backspace(self, n: int = 1) -> bool:
        n = min(max(0, n), self._gap_start)
        if n == 0:
            return False
        self._gap_start -= n
        self._bump()
        return True

    def delete(self, n: int = 1) -> bool:
        n = min(max(0, n), len(self._data) - self._gap_end)
        if n == 0:
            return False
        self._gap_end += n
        self._bump()
        return True

    def delete_word_left(self) -> bool:
        start = self.word_start()
        n = self._gap_start - start
        return self.backspace(n) if n else False

    def delete_line(self) -> bool:
        """删除到行首"""
        text = self.to_text()
        i = self._gap_start
        while i > 0 and text[i - 1] != "\n":
            i -= 1
        n = self._gap_start - i
        return self.backspace(n) if n else False

    def replace_range(self, start: int, end: int, s: str) -> None:
        start = max(0, min(self.length, start))
        end = max(start, min(self.length, end))
        self.set_cursor(start)
        self.delete(end - start)
        self.insert(s)

    # ----- 逻辑行 -----
    def line_bounds(self) -> Tuple[int, int]:
        text = self.to_text()
        i = self._gap_start
        start = i
        while start > 0 and text[start - 1] != "\n":
            start -= 1
        end = i
        n = self.length
        while end < n and text[end] != "\n":
            end += 1
        return start, end

    def lines(self) -> List[str]:
        return self.to_text().split("\n")

    # ----- 内部 -----
    def _to_raw(self, index: int) -> int:
        index = max(0, min(self.length, index))
        if index < self._gap_start:
            return index
        return index + (self._gap_end - self._gap_start)

    def _bump(self) -> None:
        self._version += 1
        self._text_cache = None

    def _move_gap(self, index: int) -> None:
        if index == self._gap_start:
            return
        if index < self._gap_start:
            n = self._gap_start - index
            self._data[self._gap_end - n : self._gap_end] = self._data[index : self._gap_start]
            self._gap_start = index
            self._gap_end -= n
        else:
            n = index - self._gap_start
            self._data[self._gap_start : self._gap_start + n] = self._data[self._gap_end : self._gap_end + n]
            self._gap_start = index
            self._gap_end += n

    def _ensure_gap(self, need: int) -> None:
        gap = self._gap_end - self._gap_start
        if gap >= need:
            return
        grow = max(need - gap, len(self._data) // 2 + 1, 64)
        self._data[self._gap_end : self._gap_end] = [""] * grow
        self._gap_end += grow
