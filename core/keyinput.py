from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, List, NamedTuple, Optional, Tuple

try:
    import termios
    import tty

    _UNIX = True
except ImportError:
    _UNIX = False

try:
    import msvcrt

    _WINDOWS = True
except ImportError:
    _WINDOWS = False


class KeyEvent(NamedTuple):
    kind: str
    value: str = ""

    def __iter__(self):
        yield self.kind
        yield self.value


TICK = KeyEvent("tick", "")
Event = KeyEvent  # 别名

# 空闲恢复：不完整 VT / 半截多字节在无后续数据时决断（非主路径）
IDLE_FLUSH = 0.12
PASTE_IDLE_TIMEOUT = 1.0
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"
BURST_DRAIN_LIMIT = 65536
# 批内中部出现 CR/LF/Tab 且批足够大时合成 paste
BATCH_PASTE_MIN = 4

_INPUT_RECORD_KEY_EVENT = 0x0001
_VK_RETURN = 0x0D
_SHIFT_PRESSED = 0x0010
_CTRL_PRESSED = 0x0008 | 0x0004
_ALT_PRESSED = 0x0002 | 0x0001

_LOG_ON = os.environ.get("BAW_LOG_KEYINPUT") == "1"
_LOG_PATH = Path(__file__).resolve().parent.parent / "develop" / "keyinput.log"

_WIN_SCAN = {
    72: "up", 80: "down", 75: "left", 77: "right",
    83: "delete", 71: "home", 79: "end",
    73: "scroll_up", 81: "scroll_down", 82: "hotkey_insert",
    59: "hotkey_f1", 60: "hotkey_f2", 61: "hotkey_f3", 62: "hotkey_f4",
    63: "hotkey_f5", 64: "hotkey_f6", 65: "hotkey_f7", 66: "hotkey_f8",
    67: "hotkey_f9", 68: "hotkey_f10", 87: "hotkey_f11", 88: "hotkey_f12",
    15: "mode_switch",
}

_CTRL = {
    "\x01": "ctrl+a", "\x02": "ctrl+b", "\x04": "ctrl+d", "\x05": "ctrl+e",
    "\x06": "ctrl+f", "\x07": "ctrl+g", "\x0b": "ctrl+k", "\x0c": "ctrl+l",
    "\x0e": "ctrl+n", "\x0f": "ctrl+o", "\x10": "ctrl+p", "\x12": "ctrl+r",
    "\x13": "ctrl+s", "\x14": "ctrl+t", "\x17": "ctrl+w", "\x18": "ctrl+x",
    "\x19": "ctrl+y", "\x1a": "ctrl+z",
}

_CSI_FINAL = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end", "Z": "mode_switch",
}

_CSI_TILDE = {
    "1": "home", "2": "hotkey_insert", "3": "delete", "4": "end",
    "5": "scroll_up", "6": "scroll_down",
    "11": "hotkey_f1", "12": "hotkey_f2", "13": "hotkey_f3", "14": "hotkey_f4",
    "15": "hotkey_f5", "17": "hotkey_f6", "18": "hotkey_f7", "19": "hotkey_f8",
    "20": "hotkey_f9", "21": "hotkey_f10", "23": "hotkey_f11", "24": "hotkey_f12",
    "200": "paste_start", "201": "paste_end",
}

_MOD = {"2": "shift+", "3": "alt+", "5": "ctrl+", "6": "ctrl+shift+", "7": "ctrl+alt+"}

# Win VK → 语义（结构化路径，不经扫描码）
_VK_MAP = {
    0x08: KeyEvent("backspace", ""),
    0x09: KeyEvent("tab", ""),
    0x0D: KeyEvent("submit", ""),
    0x1B: KeyEvent("escape", ""),
    0x21: KeyEvent("scroll_up", ""),
    0x22: KeyEvent("scroll_down", ""),
    0x23: KeyEvent("end", ""),
    0x24: KeyEvent("home", ""),
    0x25: KeyEvent("left", ""),
    0x26: KeyEvent("up", ""),
    0x27: KeyEvent("right", ""),
    0x28: KeyEvent("down", ""),
    0x2D: KeyEvent("hotkey", "insert"),
    0x2E: KeyEvent("delete", ""),
    0x70: KeyEvent("hotkey", "f1"),
    0x71: KeyEvent("hotkey", "f2"),
    0x72: KeyEvent("hotkey", "f3"),
    0x73: KeyEvent("hotkey", "f4"),
    0x74: KeyEvent("hotkey", "f5"),
    0x75: KeyEvent("hotkey", "f6"),
    0x76: KeyEvent("hotkey", "f7"),
    0x77: KeyEvent("hotkey", "f8"),
    0x78: KeyEvent("hotkey", "f9"),
    0x79: KeyEvent("hotkey", "f10"),
    0x7A: KeyEvent("hotkey", "f11"),
    0x7B: KeyEvent("hotkey", "f12"),
}


def _log(msg: str) -> None:
    if not _LOG_ON:
        return
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.time():.3f}\t{msg}\n")
    except Exception:
        pass


def _kind_event(name: str) -> KeyEvent:
    if name.startswith("hotkey_"):
        return KeyEvent("hotkey", name.split("_", 1)[1])
    return KeyEvent(name, "")


def _scan_event(code: int) -> KeyEvent:
    name = _WIN_SCAN.get(code)
    return _kind_event(name) if name else TICK


def _kbhit() -> bool:
    return bool(msvcrt.kbhit()) if _WINDOWS and msvcrt is not None else False


def _console_cp() -> int:
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleCP())
    except Exception:
        return 0


def _encoding_candidates() -> List[str]:
    encs: List[str] = []
    cp = _console_cp()
    if cp == 65001:
        encs.append("utf-8")
    elif cp:
        encs.append(f"cp{cp}")
    for fb in ("utf-8", "gbk"):
        if fb not in encs:
            encs.append(fb)
    return encs


def _decode_bytes(data: bytes) -> Optional[str]:
    if not data:
        return None
    for enc in _encoding_candidates():
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


class EncodingStream:
    """流式多字节重组：完整序列立刻产出；半截挂缓冲，空闲 latin-1 落地。"""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.at: Optional[float] = None

    def reset(self) -> None:
        self.buf.clear()
        self.at = None

    def __bool__(self) -> bool:
        return bool(self.buf)

    def feed_byte(self, b: int) -> Optional[str]:
        """喂入 0..255 字节；序列完整则返回解码文本。"""
        self.buf.append(b & 0xFF)
        if self.at is None:
            self.at = time.time()
        text = self._try_complete()
        if text is not None:
            return text
        if len(self.buf) > 8:
            return self._force()
        return None

    def feed_bytes(self, data: bytes) -> str:
        out: List[str] = []
        for b in data:
            t = self.feed_byte(b)
            if t:
                out.append(t)
        return "".join(out)

    def _try_complete(self) -> Optional[str]:
        data = bytes(self.buf)
        if not data:
            return None
        lead = data[0]
        cp = _console_cp()
        if lead < 0x80:
            self.reset()
            return chr(lead)
        if cp == 65001:
            if lead >= 0xF0:
                need = 4
            elif lead >= 0xE0:
                need = 3
            elif lead >= 0xC0:
                need = 2
            else:
                need = 1
            if len(data) < need:
                return None
            try:
                text = data.decode("utf-8")
                self.reset()
                return text
            except UnicodeDecodeError:
                # 非 UTF-8 完整序列时回退 DBCS
                if len(data) < 2:
                    return None
        else:
            need = 2
            if len(data) < need:
                return None
        for enc in _encoding_candidates():
            try:
                text = data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
            self.reset()
            return text
        return None

    def _force(self, strict: bool = False) -> Optional[str]:
        data = bytes(self.buf)
        self.reset()
        decoded = _decode_bytes(data)
        if decoded is not None:
            return decoded
        if strict:
            return None
        return "".join(chr(b) for b in data)

    def timeout_flush(self) -> Optional[str]:
        if not self.buf:
            self.at = None
            return None
        overdue = self.at is None or (time.time() - self.at) > IDLE_FLUSH
        if not overdue:
            return None
        out = self._force(strict=False)
        _log(f"enc timeout flush {out!r}")
        return out


class EscapeFSM:
    """单遍 CSI/SS3/独立 Esc。不完整序列挂缓冲，idle/显式 flush 决断。"""

    GROUND = 0
    ESC = 1
    SS3 = 2
    CSI_PARAM = 3

    def __init__(self) -> None:
        self.state = self.GROUND
        self.params = ""
        self.started_at = 0.0
        self._carry: List[str] = []

    def reset(self) -> None:
        self.state = self.GROUND
        self.params = ""
        self.started_at = 0.0
        self._carry.clear()

    def feed(self, ch: str) -> Tuple[Optional[KeyEvent], List[str]]:
        """返回 (事件|None, 回退字符列表)。"""
        if self.state == self.GROUND:
            if ch == "\x1b":
                self.state = self.ESC
                self.started_at = time.time()
                self.params = ""
                return None, []
            return KeyEvent("raw", ch), []

        if self.state == self.ESC:
            if ch == "[":
                self.state = self.CSI_PARAM
                self.params = ""
                return None, []
            if ch == "O":
                self.state = self.SS3
                return None, []
            self.reset()
            return KeyEvent("escape", ""), [ch]

        if self.state == self.SS3:
            self.reset()
            name = _CSI_FINAL.get(ch)
            return (_kind_event(name) if name else TICK), []

        if ch.isdigit() or ch == ";" or ch == "<":
            self.params += ch
            return None, []
        params = self.params
        self.reset()
        return self._csi_finish(ch, params), []

    def _mouse_sgr(self, params: str, final: str) -> KeyEvent:
        body = params[1:] if params.startswith("<") else params
        parts = body.split(";")
        try:
            cb = int(parts[0])
        except (ValueError, IndexError):
            return TICK
        if cb & 64:
            wheel = cb & 3
            if wheel == 0:
                return KeyEvent("mouse_wheel", "up")
            if wheel == 1:
                return KeyEvent("mouse_wheel", "down")
        return KeyEvent("mouse", f"{cb};{final}")

    def _csi_finish(self, final: str, params: str) -> KeyEvent:
        if params.startswith("<") and final in ("M", "m"):
            return self._mouse_sgr(params, final)
        if final == "~":
            num = params.split(";")[0] if params else ""
            name = _CSI_TILDE.get(num)
            if not name:
                return TICK
            if name == "paste_start":
                return KeyEvent("paste_start", "")
            if name == "paste_end":
                return KeyEvent("paste_end", "")
            ev = _kind_event(name)
            parts = [p for p in params.split(";") if p] if params else []
            if len(parts) >= 2 and ev.kind == "hotkey":
                mod = _MOD.get(parts[-1], "")
                if mod:
                    return KeyEvent("hotkey", mod + ev.value)
            return ev
        if final == "Z":
            return KeyEvent("mode_switch", "")
        if final == "u":
            parts = [p for p in params.split(";") if p] if params else []
            if parts and parts[0] == "13":
                mod = _MOD.get(parts[-1], "") if len(parts) >= 2 else ""
                if mod == "shift+":
                    return KeyEvent("newline", "")
                if mod == "ctrl+":
                    return KeyEvent("submit_ctrl", "")
                return KeyEvent("submit", "")
            return TICK
        if final in _CSI_FINAL:
            name = _CSI_FINAL[final]
            if name == "mode_switch":
                return KeyEvent("mode_switch", "")
            parts = [p for p in params.split(";") if p] if params else []
            mod = _MOD.get(parts[-1], "") if len(parts) >= 2 else ""
            if name in ("up", "down", "left", "right", "home", "end"):
                return KeyEvent("hotkey", mod + name) if mod else KeyEvent(name, "")
            return _kind_event(name)
        return TICK

    def timeout_event(self) -> Optional[KeyEvent]:
        if self.state == self.GROUND:
            return None
        if self.started_at and time.time() - self.started_at <= IDLE_FLUSH:
            return None
        self.reset()
        return KeyEvent("escape", "")

    def flush_partial(self) -> Optional[KeyEvent]:
        if self.state == self.GROUND:
            return None
        self.reset()
        return KeyEvent("escape", "")


class PasteAssembler:
    """bracketed 粘贴 + 突发粘贴。结束标记增量匹配，体内 CR/LF/Tab 不是按键。"""

    def __init__(self) -> None:
        self.pasting = False
        self.chunks: List[str] = []
        self.at = 0.0
        self.burst: Optional[bytearray] = None
        self.burst_text: Optional[str] = None
        self._end_matched = 0  # PASTE_END 前缀已匹配长度

    def reset(self) -> None:
        self.pasting = False
        self.chunks.clear()
        self.at = 0.0
        self.burst = None
        self.burst_text = None
        self._end_matched = 0

    def start_bracketed(self) -> None:
        self.pasting = True
        self.at = time.time()
        self.chunks.clear()
        self._end_matched = 0

    def flush(self) -> KeyEvent:
        raw = "".join(self.chunks)
        if self._end_matched:
            raw += PASTE_END[: self._end_matched]
        self.chunks.clear()
        self.pasting = False
        self.at = 0.0
        self._end_matched = 0
        return KeyEvent("paste", _redecode(raw))

    def feed_body(self, ch: str) -> List[KeyEvent]:
        if ch == "\x03":
            ev = self.flush()
            return [ev, KeyEvent("interrupt", "")]
        # 增量匹配 PASTE_END
        if ch == PASTE_END[self._end_matched]:
            self._end_matched += 1
            if self._end_matched >= len(PASTE_END):
                self._end_matched = 0
                return [self.flush()]
            self.at = time.time()
            return []
        if self._end_matched:
            # 失配：已匹配前缀回灌为正文
            prefix = PASTE_END[: self._end_matched]
            self._end_matched = 0
            self.chunks.append(prefix)
            # 当前字符重新走匹配（可能是新前缀起点）
            return self.feed_body(ch)
        self.chunks.append(ch)
        self.at = time.time()
        return []

    def idle_timeout(self) -> bool:
        return self.pasting and self.chunks and (time.time() - self.at) > PASTE_IDLE_TIMEOUT

    def start_burst(self, first: str = "") -> None:
        self.burst = bytearray(first.encode("latin-1", "replace"))

    def drain_burst(self) -> None:
        if self.burst is None:
            return
        try:
            while _kbhit() and len(self.burst) < BURST_DRAIN_LIMIT:
                raw = msvcrt.getch()
                ch = chr(raw[0]) if isinstance(raw, (bytes, bytearray)) and raw else (raw if isinstance(raw, str) else "")
                if ch:
                    self.burst += ch.encode("latin-1", "replace")
        except Exception:
            pass
        if not _kbhit() or len(self.burst) >= BURST_DRAIN_LIMIT:
            raw = bytes(self.burst)
            self.burst = None
            self.burst_text = _redecode(raw.decode("latin-1", "replace"))

    def pop_burst_text(self) -> Optional[KeyEvent]:
        if self.burst_text is None:
            return None
        text = self.burst_text
        self.burst_text = None
        return KeyEvent("paste", text)


def _redecode(s: str) -> str:
    if not any(0x80 <= ord(c) <= 0xFF for c in s):
        return s
    try:
        data = s.encode("latin-1")
    except UnicodeEncodeError:
        return s
    decoded = _decode_bytes(data)
    return decoded if decoded is not None else s


def _trailing_partial(s: str, marker: str) -> int:
    for keep in range(min(len(s), len(marker) - 1), 0, -1):
        if marker.startswith(s[-keep:]):
            return keep
    return 0


class Decoder:
    """字符单元 → KeyEvent。可注入单元做纯单测。"""

    def __init__(self) -> None:
        self.fsm = EscapeFSM()
        self.enc = EncodingStream()
        self.paste = PasteAssembler()
        self.pending: List[KeyEvent] = []
        self.chars: List[str] = []
        self.half_at: Optional[float] = None
        self._prefix: Optional[str] = None

    @property
    def dbcs(self) -> EncodingStream:
        return self.enc

    def reset(self) -> None:
        self.fsm.reset()
        self.enc.reset()
        self.paste.reset()
        self.pending.clear()
        self.chars.clear()
        self.half_at = None
        self._prefix = None

    def push_pending(self, ev: KeyEvent) -> None:
        self.pending.append(ev)

    def feed_char(self, ch: str) -> List[KeyEvent]:
        """喂一个逻辑字符单元，产出 0..n 事件。"""
        if self.paste.pasting:
            return self.paste.feed_body(ch)

        if self._prefix is not None:
            prefix, self._prefix = self._prefix, None
            code = ord(ch) if ch else -1
            if prefix == "\xe0" and code != 15 and code not in _WIN_SCAN:
                out = self._classify_char(prefix)
                out.extend(self._classify_char(ch))
                return out
            if code == 15 or ch == "\x0f":
                return [KeyEvent("mode_switch", "")]
            if code in _WIN_SCAN:
                return [_scan_event(code)]
            return []
        if ch in ("\x00", "\xe0"):
            self._prefix = ch
            return []

        out: List[KeyEvent] = []
        ev, back = self.fsm.feed(ch)
        if ev is not None:
            if ev.kind == "raw":
                out.extend(self._classify_char(ev.value))
            elif ev.kind == "paste_start":
                self.paste.start_bracketed()
            elif ev.kind == "paste_end":
                pass
            elif ev.kind != "tick":
                out.append(ev)
        for b in back:
            out.extend(self._classify_char(b))
        return out

    def feed_str(self, s: str) -> List[KeyEvent]:
        out: List[KeyEvent] = []
        for ch in s:
            out.extend(self.feed_char(ch))
        return out

    def _classify_char(self, ch: str) -> List[KeyEvent]:
        if ch in ("\x00", "\xe0"):
            self._prefix = ch
            return []
        lead = ord(ch)
        if 0x80 <= lead <= 0xFF:
            text = self.enc.feed_byte(lead)
            return [KeyEvent("char", text)] if text else []
        if ch == "\r":
            return [KeyEvent("submit", "")]
        if ch == "\n":
            return [KeyEvent("submit_ctrl", "")]
        if ch == "\t":
            return [KeyEvent("tab", "")]
        if ch in ("\x08", "\x7f"):
            return [KeyEvent("backspace", "")]
        if ch == "\x03":
            return [KeyEvent("interrupt", "")]
        if ch == "\x15":
            return [KeyEvent("clear", "")]
        ctrl = _CTRL.get(ch)
        if ctrl:
            return [KeyEvent("hotkey", ctrl)]
        if ch and ord(ch) >= 32:
            return [KeyEvent("char", ch)]
        return []

    def feed_printable_run(self, s: str) -> List[KeyEvent]:
        return [KeyEvent("char", s)] if s else []

    def timeout_tick(self) -> List[KeyEvent]:
        out: List[KeyEvent] = []
        flushed = self.enc.timeout_flush()
        if flushed:
            out.append(KeyEvent("char", flushed))
        tev = self.fsm.timeout_event()
        if tev is not None:
            out.append(tev)
        if self.paste.idle_timeout():
            out.append(self.paste.flush())
        return out


def _merge_chars(evs: List[KeyEvent]) -> List[KeyEvent]:
    """相邻 char 合并为一条（IME 整句上屏）。"""
    if not evs:
        return []
    out: List[KeyEvent] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            out.append(KeyEvent("char", "".join(buf)))
            buf.clear()

    for ev in evs:
        if ev.kind == "char":
            buf.append(ev.value)
        else:
            flush()
            out.append(ev)
    flush()
    return out


def _classify_batch_text(text: str) -> List[KeyEvent]:
    """整批文本分类；中部 CR/LF/Tab 且批足够大 → 单条 paste。"""
    if len(text) >= BATCH_PASTE_MIN:
        for i, ch in enumerate(text[:-1]):
            if ch in ("\r", "\n", "\t") and any(c not in "\r\n\t" for c in text[i + 1 :]):
                return [KeyEvent("paste", text)]
    return _merge_chars(Decoder().feed_str(text))


class EventQueue:
    """FIFO 事件队列。"""

    def __init__(self) -> None:
        self._q: List[KeyEvent] = []

    def push(self, ev: KeyEvent) -> None:
        self._q.append(ev)

    def push_many(self, evs: List[KeyEvent]) -> None:
        self._q.extend(evs)

    def pop(self) -> Optional[KeyEvent]:
        return self._q.pop(0) if self._q else None

    def clear(self) -> None:
        self._q.clear()

    def __len__(self) -> int:
        return len(self._q)

    def __bool__(self) -> bool:
        return bool(self._q)


# ----- Win 结构化句柄缓存 -----
_k32 = None
_INPUT_RECORD_ARR = None


def _win_console():
    global _k32, _INPUT_RECORD_ARR
    if not _WINDOWS:
        return None, None
    if _k32 is not None:
        return _k32, _INPUT_RECORD_ARR
    try:
        import ctypes
        from ctypes import wintypes

        class KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [
                ("bKeyDown", wintypes.BOOL),
                ("wRepeatCount", ctypes.c_ushort),
                ("wVirtualKeyCode", ctypes.c_ushort),
                ("wVirtualScanCode", ctypes.c_ushort),
                ("uChar", ctypes.c_wchar),
                ("dwControlKeyState", wintypes.DWORD),
            ]

        class _INPUT_UNION(ctypes.Union):
            _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]

        class INPUT_RECORD(ctypes.Structure):
            _fields_ = [("EventType", ctypes.c_ushort), ("Event", _INPUT_UNION)]

        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        _k32 = k32
        _INPUT_RECORD_ARR = INPUT_RECORD
        return k32, INPUT_RECORD
    except Exception:
        return None, None


def _vk_event(vk: int, uchar: str, state: int) -> Optional[KeyEvent]:
    ctrl = bool(state & _CTRL_PRESSED)
    shift = bool(state & _SHIFT_PRESSED)
    alt = bool(state & _ALT_PRESSED)

    if vk == _VK_RETURN:
        if ctrl:
            return KeyEvent("submit_ctrl", "")
        if shift:
            return KeyEvent("newline", "")
        return KeyEvent("submit", "")
    if vk == 0x09 and shift:
        return KeyEvent("mode_switch", "")
    if vk == 0x03:
        return KeyEvent("interrupt", "")
    if vk == 0x15:
        return KeyEvent("clear", "")

    base = _VK_MAP.get(vk)
    if base is None:
        if uchar and ord(uchar) >= 32:
            return KeyEvent("char", uchar)
        if uchar == "\r":
            return KeyEvent("submit", "")
        if uchar == "\t":
            return KeyEvent("tab", "")
        if uchar in ("\x08", "\x7f"):
            return KeyEvent("backspace", "")
        return None

    if base.kind == "hotkey":
        mods = ""
        if ctrl:
            mods += "ctrl+"
        if alt:
            mods += "alt+"
        if shift and base.value.startswith("f"):
            mods += "shift+"
        return KeyEvent("hotkey", mods + base.value if mods else base.value)

    if base.kind in ("up", "down", "left", "right", "home", "end", "delete", "scroll_up", "scroll_down"):
        if ctrl or alt:
            mods = ""
            if ctrl:
                mods += "ctrl+"
            if alt:
                mods += "alt+"
            if shift:
                mods += "shift+"
            name = base.kind
            if name == "scroll_up":
                name = "up"
            if name == "scroll_down":
                name = "down"
            return KeyEvent("hotkey", mods + name)
        return base

    return base


def _peek_enter_mod() -> Optional[str]:
    if not _WINDOWS:
        return None
    k32, INPUT_RECORD = _win_console()
    if not k32:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        handle = k32.GetStdHandle(-10)
        if not handle:
            return None
        buf = (INPUT_RECORD * 16)()
        count = wintypes.DWORD(0)
        if not k32.PeekConsoleInputW(handle, buf, 16, ctypes.byref(count)):
            return None
        for i in range(count.value):
            rec = buf[i]
            if rec.EventType != _INPUT_RECORD_KEY_EVENT:
                continue
            ke = rec.Event.KeyEvent
            if not ke.bKeyDown:
                continue
            if ke.wVirtualKeyCode != _VK_RETURN:
                return None
            state = int(ke.dwControlKeyState)
            if state & _CTRL_PRESSED:
                return "ctrl+enter"
            if state & _SHIFT_PRESSED:
                return "shift+enter"
            return "enter"
        return None
    except Exception:
        return None


def _queue_depth() -> int:
    if not _WINDOWS:
        return 0
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        handle = k32.GetStdHandle(-10)
        count = ctypes.c_uint32()
        if k32.GetNumberOfConsoleInputEvents(handle, ctypes.byref(count)):
            return int(count.value)
    except Exception:
        pass
    return 0


class KeyReader:
    """控制台读取器：整批抽干 + 流式解析。公开 read_event / flush_input。"""

    def __init__(self) -> None:
        self._fd = None
        self._old_settings = None
        self.dec = Decoder()
        self.queue = EventQueue()
        self._use_win_records = _WINDOWS

    # --- 兼容属性（旧测试） ---
    @property
    def _out(self) -> List[KeyEvent]:
        return self.queue._q

    @property
    def _pend(self) -> Any:
        """输出队列 / 预分类 / 半包解析未决。"""
        if self.queue:
            return self.queue._q
        if self.dec.pending:
            return self.dec.pending
        if self.dec.fsm.state != EscapeFSM.GROUND or self.dec._prefix or self.dec.enc:
            return True
        return self.dec.paste.pasting or self.dec.paste.burst is not None

    @property
    def _pasting(self) -> bool:
        return self.dec.paste.pasting or self.dec.paste.burst is not None

    @property
    def _buf(self) -> List[str]:
        return self.dec.chars

    def __enter__(self) -> "KeyReader":
        if _UNIX and sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        if _UNIX and self._old_settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            self._old_settings = None

    def read_key(self, timeout: float = 0.0) -> Optional[Any]:
        ev = self.read_event(timeout)
        return None if ev == TICK or ev.kind == "tick" else ev

    def read_event(self, timeout: float = 0.02) -> KeyEvent:
        if self.dec.pending:
            return self.dec.pending.pop(0)
        if self.queue:
            ev = self.queue.pop()
            return ev if ev is not None else TICK
        idle = self.dec.timeout_tick()
        if idle:
            self.queue.push_many(idle[1:])
            return idle[0]
        collected = self._read_windows(timeout) if _WINDOWS else self._read_unix(timeout)
        if self.dec.pending:
            ev = self.dec.pending.pop(0)
            if collected:
                self.queue.push_many(collected)
            return ev
        if collected:
            self.queue.push_many(collected[1:])
            return collected[0]
        return TICK

    def read_events(self, timeout: float = 0.02) -> List[KeyEvent]:
        """整批抽干：返回本帧全部逻辑事件（空列表=无事件）。不含 tick。"""
        out: List[KeyEvent] = []
        while self.dec.pending:
            out.append(self.dec.pending.pop(0))
        while self.queue:
            ev = self.queue.pop()
            if ev is not None and ev.kind != "tick":
                out.append(ev)
        if out:
            return out
        out.extend(self.dec.timeout_tick())
        if out:
            return [e for e in out if e.kind != "tick"]
        collected = self._read_windows(timeout) if _WINDOWS else self._read_unix(timeout)
        out.extend(self.dec.pending)
        self.dec.pending.clear()
        for ev in collected:
            if ev.kind != "tick":
                out.append(ev)
        return out

    def flush_input(self) -> None:
        self.dec.reset()
        self.queue.clear()
        if _WINDOWS:
            try:
                while msvcrt.kbhit():
                    msvcrt.getch()
            except Exception:
                pass

    def _getch_char(self) -> str:
        raw = msvcrt.getch()
        if isinstance(raw, (bytes, bytearray)):
            return chr(raw[0]) if raw else ""
        return raw or ""

    def _drain_win_records(self) -> List[KeyEvent]:
        """ReadConsoleInputW 整批抽干 → 结构化 KeyEvent。"""
        k32, INPUT_RECORD = _win_console()
        if not k32 or INPUT_RECORD is None:
            return None  # type: ignore
        try:
            import ctypes
            from ctypes import wintypes

            handle = k32.GetStdHandle(-10)
            if not handle:
                return None  # type: ignore
            out: List[KeyEvent] = []
            while True:
                buf = (INPUT_RECORD * 32)()
                count = wintypes.DWORD(0)
                if not k32.PeekConsoleInputW(handle, buf, 32, ctypes.byref(count)) or count.value == 0:
                    break
                # 读走全部
                got = wintypes.DWORD(0)
                if not k32.ReadConsoleInputW(handle, buf, 32, ctypes.byref(got)):
                    break
                if got.value == 0:
                    break
                for i in range(got.value):
                    rec = buf[i]
                    if rec.EventType != _INPUT_RECORD_KEY_EVENT:
                        continue
                    ke = rec.Event.KeyEvent
                    if not ke.bKeyDown:
                        continue
                    ev = _vk_event(int(ke.wVirtualKeyCode), ke.uChar, int(ke.dwControlKeyState))
                    if ev is not None:
                        out.append(ev)
                if got.value < 32:
                    break
            return out
        except Exception:
            self._use_win_records = False
            return None  # type: ignore

    def _read_windows(self, timeout: float) -> List[KeyEvent]:
        try:
            # msvcrt 字节整批优先（ConPTY / 可 monkeypatch）；否则结构化 KEY_EVENT
            if _kbhit():
                return self._drain_msvcrt()
            if self._use_win_records:
                recs = self._drain_win_records()
                if recs:
                    return recs
            if timeout <= 0:
                return []
            time.sleep(timeout)
            if _kbhit():
                return self._drain_msvcrt()
            if self._use_win_records:
                recs = self._drain_win_records()
                if recs:
                    return recs
            return []
        except Exception:
            self.dec.chars.clear()
            return []

    def _drain_msvcrt(self) -> List[KeyEvent]:
        raw = bytearray()
        while _kbhit() and len(raw) < BURST_DRAIN_LIMIT:
            b = msvcrt.getch()
            if isinstance(b, (bytes, bytearray)):
                if b:
                    raw.append(b[0])
            elif b:
                raw.append(ord(b) & 0xFF)
        return self._parse_byte_batch(bytes(raw))

    def _parse_byte_batch(self, raw: bytes) -> List[KeyEvent]:
        """字节批 → 事件。完整序列单遍解出；扫描码/前缀在批内配对。"""
        if not raw:
            return []
        if len(raw) >= 32 or (len(raw) >= BATCH_PASTE_MIN and self._looks_like_paste_bytes(raw)):
            self.dec.paste.start_burst("")
            self.dec.paste.burst = bytearray(raw)
            if not _kbhit():
                self.dec.paste.drain_burst()
            pe = self.dec.paste.pop_burst_text()
            return [pe] if pe else []

        out: List[KeyEvent] = []
        char_run: List[str] = []

        def flush_run() -> None:
            if char_run:
                out.append(KeyEvent("char", "".join(char_run)))
                char_run.clear()

        i = 0
        n = len(raw)
        while i < n:
            b = raw[i]
            # 粘贴体：字节只进 paste，不当按键
            if self.dec.paste.pasting:
                if b >= 0x80:
                    text = self.dec.enc.feed_byte(b)
                    if text:
                        for ch in text:
                            out.extend(self.dec.paste.feed_body(ch))
                else:
                    out.extend(self.dec.feed_char(chr(b)))
                i += 1
                continue

            if b in (0x00, 0xE0) and i + 1 < n:
                nxt = raw[i + 1]
                if nxt == 15:
                    flush_run()
                    out.append(KeyEvent("mode_switch", ""))
                    i += 2
                    continue
                if nxt in _WIN_SCAN:
                    flush_run()
                    out.append(_scan_event(nxt))
                    i += 2
                    continue
                if b == 0xE0 and nxt >= 0x80:
                    text = self.dec.enc.feed_byte(b)
                    if text:
                        char_run.append(text)
                    i += 1
                    continue
                if b == 0xE0:
                    flush_run()
                    out.append(KeyEvent("char", chr(0xE0)))
                    i += 1
                    continue
                flush_run()
                i += 2  # 无效扩展键丢弃
                continue
            if b in (0x00, 0xE0):
                flush_run()
                i += 1
                continue
            if b >= 0x80:
                text = self.dec.enc.feed_byte(b)
                if text:
                    char_run.append(text)
                i += 1
                continue
            flush_run()
            out.extend(self.dec.feed_char(chr(b)))
            i += 1

        flush_run()
        # 批内中部 CR/Tab 启发式（无 bracketed 时）
        if out and not self.dec.paste.pasting:
            if out[0].kind in ("submit", "tab") and len(out) > 1:
                rebuilt = self._events_to_paste(out)
                if rebuilt is not None:
                    return [rebuilt]
        return out

    def _looks_like_paste_bytes(self, raw: bytes) -> bool:
        # ESC[200~ 开头 → bracketed，交给 FSM
        if raw.startswith(b"\x1b[200~"):
            return False
        mid = False
        for idx, b in enumerate(raw):
            if b in (0x0D, 0x0A, 0x09) and idx < len(raw) - 1:
                # 后面还有非空
                if any(x not in (0x0D, 0x0A, 0x09) for x in raw[idx + 1 :]):
                    mid = True
                    break
        return mid and len(raw) >= BATCH_PASTE_MIN

    def _events_to_paste(self, evs: List[KeyEvent]) -> Optional[KeyEvent]:
        parts: List[str] = []
        for ev in evs:
            if ev.kind == "char":
                parts.append(ev.value)
            elif ev.kind == "submit":
                parts.append("\r")
            elif ev.kind == "submit_ctrl":
                parts.append("\n")
            elif ev.kind == "tab":
                parts.append("\t")
            else:
                return None
        return KeyEvent("paste", "".join(parts))

    def _read_unix(self, timeout: float) -> List[KeyEvent]:
        import select

        if not _UNIX or not sys.stdin.isatty():
            return []
        try:
            r, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout))
            if not r:
                return []
            data = b""
            try:
                data = os.read(sys.stdin.fileno(), 4096)
            except Exception:
                ch = sys.stdin.read(1)
                return self.dec.feed_str(ch) if ch else []
            if not data:
                return []
            # 解码为字符再走 VT
            text = self.dec.enc.feed_bytes(data)
            leftover = self.dec.enc.timeout_flush()
            if leftover:
                text = (text or "") + leftover
            out: List[KeyEvent] = []
            if text:
                # bracketed / 中部控制 → paste
                if len(text) >= BATCH_PASTE_MIN and self._looks_like_paste_text(text):
                    return [KeyEvent("paste", text)]
                out.extend(self.dec.feed_str(text))
            return out
        except Exception:
            return []

    def _looks_like_paste_text(self, text: str) -> bool:
        if text.startswith(PASTE_START):
            return False
        for i, ch in enumerate(text[:-1]):
            if ch in ("\r", "\n", "\t") and any(c not in "\r\n\t" for c in text[i + 1 :]):
                return len(text) >= BATCH_PASTE_MIN
        return False


_default_reader: Optional[KeyReader] = None


def get_reader() -> KeyReader:
    global _default_reader
    if _default_reader is None:
        _default_reader = KeyReader()
        _default_reader.__enter__()
    return _default_reader


def read_key_event(timeout: float = 0.02) -> Event:
    return get_reader().read_event(timeout)


def read_events(timeout: float = 0.02) -> List[Event]:
    """一次整批：返回逻辑事件列表；无事件返回 []（不含 tick）。"""
    return get_reader().read_events(timeout)


def read_key(timeout: float = 0.0) -> Optional[Event]:
    ev = read_key_event(timeout)
    return None if ev == TICK or ev.kind == "tick" else ev


def flush_input() -> None:
    get_reader().flush_input()


def direction_of(kind: str, value: str) -> Optional[str]:
    if kind in ("up", "down", "left", "right"):
        return kind
    if kind == "hotkey":
        v = str(value or "").lower().strip()
        for d in ("up", "down", "left", "right"):
            if v == d or v.endswith("+" + d):
                return d
    return None


def supported_kinds() -> tuple:
    return (
        "tick", "char", "submit", "submit_ctrl", "newline", "tab", "mode_switch",
        "backspace", "delete", "up", "down", "left", "right", "home", "end",
        "scroll_up", "scroll_down", "escape", "interrupt", "clear", "hotkey",
        "paste", "paste_start", "paste_end", "mouse_wheel", "mouse",
    )
