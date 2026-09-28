"""键位：token 规范化 + 绑定编译倒排表 + O(1) resolve。

token 形如 enter / shift+enter / ctrl+enter / tab / up / pageup / f5 / a
Action 为语义动作；热路径禁止线性扫绑定列表。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


class Action(str, Enum):
    SUBMIT = "submit"
    NEWLINE = "newline"
    SEND = "send"
    COMPLETE = "complete"
    FOCUS_NEXT = "focus_next"
    MODE_CYCLE = "mode_cycle"
    EXPAND = "expand"
    COLLAPSE = "collapse"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"
    CARET_UP = "caret_up"
    CARET_DOWN = "caret_down"
    CARET_LEFT = "caret_left"
    CARET_RIGHT = "caret_right"
    CARET_HOME = "caret_home"
    CARET_END = "caret_end"
    BACKSPACE = "backspace"
    DELETE = "delete"
    ESCAPE = "escape"
    INTERRUPT = "interrupt"
    CLEAR = "clear"
    PASTE = "paste"
    INSERT = "insert"
    IGNORE = "ignore"


class Context(str, Enum):
    INPUT = "input"
    TREE = "tree"
    SETTINGS = "settings"


_EVENT_TOKEN = {
    "mode_switch": "shift+tab",
    "tab": "tab",
    "submit": "enter",
    "submit_ctrl": "ctrl+enter",
    "newline": "shift+enter",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "backspace": "backspace",
    "delete": "delete",
    "escape": "escape",
    "scroll_up": "pageup",
    "scroll_down": "pagedown",
    "interrupt": "ctrl+c",
    "clear": "ctrl+u",
    "paste": "paste",
}

# 产品契约：Enter 恒提交（即使误配为换行）
_FIXED_TOKENS = {
    "enter": Action.SUBMIT,
    "ctrl+enter": Action.SEND,
    "shift+enter": Action.NEWLINE,
    "shift+tab": Action.MODE_CYCLE,
}

_DEFAULT_BINDINGS: Dict[str, List[str]] = {
    "send": ["ctrl+enter", "f5", "ctrl+s"],
    "newline": ["shift+enter"],
    "switch_focus": ["tab", "f4", "ctrl+o"],
    "switch_mode": ["shift+tab"],
    "complete": ["tab"],
    "scroll_up": ["pageup", "ctrl+up"],
    "scroll_down": ["pagedown", "ctrl+down"],
    "expand": ["right", "l"],
    "collapse": ["left", "h"],
}

_ACTION_OF_SETTING = {
    "send": Action.SEND,
    "newline": Action.NEWLINE,
    "switch_focus": Action.FOCUS_NEXT,
    "switch_mode": Action.MODE_CYCLE,
    "complete": Action.COMPLETE,
    "scroll_up": Action.SCROLL_UP,
    "scroll_down": Action.SCROLL_DOWN,
    "expand": Action.EXPAND,
    "collapse": Action.COLLAPSE,
}

_EDIT_DEFAULTS: Dict[str, Action] = {
    "up": Action.CARET_UP,
    "down": Action.CARET_DOWN,
    "left": Action.CARET_LEFT,
    "right": Action.CARET_RIGHT,
    "home": Action.CARET_HOME,
    "end": Action.CARET_END,
    "backspace": Action.BACKSPACE,
    "delete": Action.DELETE,
    "escape": Action.ESCAPE,
    "clear": Action.CLEAR,
    "paste": Action.PASTE,
}


def normalize_token(value: Any) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return ""
    # 修饰键顺序归一：ctrl+alt+shift+key
    if "+" in s:
        *mods, key = s.split("+")
        order = {"ctrl": 0, "alt": 1, "shift": 2, "meta": 3, "super": 3}
        mods = sorted({m.strip() for m in mods if m.strip()}, key=lambda m: order.get(m, 9))
        key = key.strip()
        if not key:
            return ""
        return "+".join(mods + [key]) if mods else key
    return s


def event_token(kind: str, value: Any) -> str:
    if kind == "mouse_wheel":
        v = str(value or "").strip().lower()
        if v == "up":
            return "wheel_up"
        if v == "down":
            return "wheel_down"
        return ""
    if kind in _EVENT_TOKEN:
        return _EVENT_TOKEN[kind]
    if kind == "hotkey":
        return normalize_token(value)
    if kind == "char":
        s = str(value or "")
        return s.lower() if len(s) == 1 else ""
    return ""


def binding_tokens(binding: Any) -> List[str]:
    if binding is None:
        return []
    items: Iterable[Any]
    if isinstance(binding, (list, tuple, set)):
        items = binding
    else:
        items = [binding]
    out: List[str] = []
    for b in items:
        t = normalize_token(b)
        if t:
            out.append(t)
    return out


def direction_of(kind: str, value: str) -> Optional[str]:
    tok = event_token(kind, value)
    for d in ("up", "down", "left", "right"):
        if tok == d or tok.endswith("+" + d):
            return d
    return None


class Keymap:
    """(context, token) -> Action 的编译倒排表。"""

    __slots__ = ("_table", "_names")

    def __init__(self) -> None:
        self._table: Dict[Tuple[str, str], Action] = {}
        self._names: Dict[str, List[str]] = {}

    def compile(self, settings: Optional[Mapping[str, Any]] = None) -> None:
        merged: Dict[str, List[str]] = {k: list(v) for k, v in _DEFAULT_BINDINGS.items()}
        if settings:
            for k, v in settings.items():
                if k in _ACTION_OF_SETTING and v not in (None, ""):
                    merged[k] = binding_tokens(v)
        # 丢掉空绑定
        for k in list(merged):
            if not merged[k]:
                merged[k] = list(_DEFAULT_BINDINGS.get(k, []))

        self._names = merged
        table: Dict[Tuple[str, str], Action] = {}

        # 1) 编辑默认（输入上下文）
        for tok, act in _EDIT_DEFAULTS.items():
            table[(Context.INPUT.value, tok)] = act

        # 鼠标滚轮 → 会话区滚动（全上下文）
        for ctx in Context:
            table[(ctx.value, "wheel_up")] = Action.SCROLL_UP
            table[(ctx.value, "wheel_down")] = Action.SCROLL_DOWN

        # 2) 可配置绑定（全上下文；树/设置按需覆盖）
        for setting, tokens in merged.items():
            act = _ACTION_OF_SETTING[setting]
            for tok in tokens:
                for ctx in (Context.INPUT.value, Context.TREE.value, Context.SETTINGS.value):
                    # complete 与 switch_focus 同键（tab）：输入框优先 complete，树优先 focus
                    if tok == "tab" and act == Action.COMPLETE and ctx == Context.TREE.value:
                        continue
                    if tok == "tab" and act == Action.FOCUS_NEXT and ctx == Context.INPUT.value:
                        continue
                    # 单字符键（h/l/` 等）不得进输入上下文：否则打字被劫持为
                    # 树/模式动作（如 expand:l 使输入框无法键入 l）
                    if len(tok) == 1 and ctx == Context.INPUT.value:
                        continue
                    table.setdefault((ctx, tok), act)

        # 3) 固定契约压过可配置
        for tok, act in _FIXED_TOKENS.items():
            table[(Context.INPUT.value, tok)] = act
            if tok == "enter":
                # 树上裸 Enter = 展开/折叠
                table[(Context.TREE.value, tok)] = Action.EXPAND
            if tok == "shift+enter":
                table[(Context.TREE.value, tok)] = Action.NEWLINE
            if tok == "ctrl+enter":
                table[(Context.TREE.value, tok)] = Action.SEND
            if tok == "shift+tab":
                for ctx in Context:
                    table[(ctx.value, tok)] = Action.MODE_CYCLE

        # 4) 树上下文：空格折叠、单字符 h/l
        table[(Context.TREE.value, " ")] = Action.COLLAPSE  # toggle 时由 handler 决定
        for tok in merged.get("expand", []):
            if len(tok) == 1:
                table[(Context.TREE.value, tok)] = Action.EXPAND
        for tok in merged.get("collapse", []):
            if len(tok) == 1:
                table[(Context.TREE.value, tok)] = Action.COLLAPSE

        self._table = table

    def resolve(self, context: Context | str, kind: str, value: Any = "") -> Action:
        ctx = context.value if isinstance(context, Context) else str(context)
        tok = event_token(kind, value)
        if not tok:
            return Action.IGNORE
        act = self._table.get((ctx, tok))
        if act is not None:
            return act
        if kind == "char" and ctx == Context.INPUT.value:
            return Action.INSERT
        if kind == "submit":
            return Action.SUBMIT
        return Action.IGNORE

    def label(self, setting: str) -> str:
        toks = self._names.get(setting) or _DEFAULT_BINDINGS.get(setting) or []
        return toks[0] if toks else ""

    def tokens(self, setting: str) -> List[str]:
        return list(self._names.get(setting) or _DEFAULT_BINDINGS.get(setting) or [])


_default_keymap: Optional[Keymap] = None


def get_keymap(settings: Optional[Mapping[str, Any]] = None) -> Keymap:
    global _default_keymap
    if _default_keymap is None:
        _default_keymap = Keymap()
        _default_keymap.compile(settings)
    elif settings is not None:
        _default_keymap.compile(settings)
    return _default_keymap
