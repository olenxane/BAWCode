# -*- coding: utf-8 -*-
"""终端输入栈：prompt_toolkit 输入后端。

公开契约：read_key_event / read_events / read_key / flush_input /
direction_of + KeyEvent NamedTuple。

数据源：Windows 下强制 ConsoleInputReader 事件记录路径（pt 默认经
_is_win_vt100_input_enabled 试探切到 Vt100ConsoleInputReader，该路径丢弃
MOUSE_EVENT 记录——鼠标滚轮/拖拽全断）；其他平台走 pt 的 Vt100 输入路径
（键盘可用，鼠标不支持）。翻译层 KeyPress → KeyEvent；读取循环后台线程 +
队列，read_events(0) 非阻塞供拍帧泵。

物理键位：
  Enter        → submit（直接发送）
  Ctrl+Enter   → newline（pt 形态为 Escape+ControlM 序列，此处折叠）
  Ctrl+J       → newline（同形 ControlJ；VT 路径下 Ctrl+Enter 落为 \n 亦兼容）
  Shift+Enter  → 与 Enter 同形 → submit（事件流固有限制，无法区分）
  Alt+Enter    → newline（同形折叠）
  submit_ctrl 事件不产生：keymap 的 send=ctrl+enter 绑定闲置；
  如需独立发送键可配置 send=f5 等（Enter 始终发送）。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from core.log import get_logger


class KeyEvent(NamedTuple):
    kind: str
    value: str = ""


TICK = KeyEvent("tick", "")
Event = KeyEvent  # 别名

log = get_logger("keyinput")

_WINDOWS = sys.platform == "win32"
# 输入层取证日志开关：环境变量 BAW_LOG_KEYINPUT=1 开启（默认关闭）
_LOG_ON = os.environ.get("BAW_LOG_KEYINPUT") == "1"
_LOG_PATH = Path(__file__).resolve().parent.parent / "develop" / "pt_input.log"


def _flog(msg: str) -> None:
    if _LOG_ON:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as fh:
                import datetime

                fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S.%f} {msg}\n")
        except OSError:
            pass


# ----- 翻译层（纯函数，无 TTY 依赖，可直接单测） -----

def _hotkey(name: str, ctrl: bool, alt: bool, shift: bool) -> KeyEvent:
    mods = ""
    if ctrl:
        mods += "ctrl+"
    if alt:
        mods += "alt+"
    if shift:
        mods += "shift+"
    return KeyEvent("hotkey", mods + name)


def _mouse_event(data: str) -> Optional[KeyEvent]:
    """WindowsMouseEvent data = "button;event_type;X;Y"（0-based 单元格）。

    pt 枚举值为大写（如 'NONE;SCROLL_UP;76;19'），统一 lower 后匹配；
    MOUSE_UP 的 button 为 NONE，不参与按下态判定。"""
    try:
        button, et, xs, ys = (data or "").split(";")
    except ValueError:
        return None
    button = button.strip().lower()
    et = et.strip().lower()
    if et == "scroll_up":
        return KeyEvent("mouse_wheel", "up")
    if et == "scroll_down":
        return KeyEvent("mouse_wheel", "down")
    pos = f"{xs},{ys}"
    if et == "mouse_down":
        # value 携带按键前缀（left/right/middle）：会话树自绘选区需要右键复制
        return KeyEvent("mouse_down", f"{button}:{pos}")
    if et == "mouse_up":
        return KeyEvent("mouse_up", pos)
    if et == "mouse_move":
        return KeyEvent("mouse_move", pos)
    return None


def _is_char_press(key, data: str) -> bool:
    return isinstance(key, str) and len(key) == 1 and ord(key) >= 32


# 最近一次 paste 折叠时刻（monotonic 秒）：长粘贴被 20fps 帧泵劈成多批时，
# 后续批次的残余字符段据此降低折叠阈值续接为 paste（模块级，测试可直接复位）
_LAST_PASTE = [0.0]
_PASTE_CONT_WINDOW = 0.3  # 续接窗口秒数


def _fold_paste_run(presses: List, i: int, n: int, cont: bool = False) -> Optional[Tuple[str, int]]:
    """conhost 粘贴折叠启发式：presses[i:] 起的连续段满足粘贴特征时折叠为单 paste。

    终端拦截 Ctrl+V 后，剪贴板文本以按键记录流到达，与逐键打字同形。批次内
    连续段满足以下任一特征判为粘贴：
      - 段内出现"中部"回车/Tab（非批尾）→ 多行粘贴（否则会逐行提交）
      - 纯可打印字符连续 ≥ _PASTE_BURST_MIN（单行长文本；打字/自动重复在帧泵
        批次里到不了这个量级）
      - cont=True（距上次折叠不足 _PASTE_CONT_WINDOW）：任意非空段即折叠——
        长粘贴的跨批次残余不再以逐字符形态漏过
    段折叠为单个 paste 事件（回车→\\n、Tab→\\t）；段尾紧随的批尾回车视为剪贴板
    尾部换行并入。≤320 字符折叠后与逐键插入等价，无行为回归面。
    返回 (paste文本, 消费后的下一索引)；不满足特征返回 None。"""
    j = i
    chars = 0
    interior_break = False
    while j < n:
        k2, d2 = presses[j].key, presses[j].data or ""
        if _is_char_press(k2, d2):
            chars += 1
        elif k2 in _PASTE_BREAK_KEYS and j + 1 < n:
            interior_break = True  # 中部回车/Tab 入段；批尾的保持原语义
        else:
            break
        j += 1
    if not (interior_break or chars >= (_PASTE_BURST_MIN if not cont else 1)) or j == i:
        return None
    text_parts = []
    for p in presses[i:j]:
        k2 = p.key
        if k2 in (_K.Enter, _K.ControlM):
            text_parts.append("\n")
        elif k2 in (_K.Tab, _K.ControlI):
            text_parts.append("\t")
        else:
            text_parts.append(k2)
    if j < n and presses[j].key in (_K.Enter, _K.ControlM):
        text_parts.append("\n")
        j += 1
    return "".join(text_parts), j


def translate_key_presses(presses: List) -> List[KeyEvent]:
    """一批 KeyPress → KeyEvent 列表。

    序列折叠：Escape+ControlM → newline（Ctrl+Enter）；
    Escape+可见字符 → 该字符（legacy 的 alt 前缀忽略行为）。
    粘贴：BracketedPaste（自带 data 或后随同批可见字符）合成单个 paste 事件。
    """
    if _K is None:  # pragma: no cover - 无 prompt_toolkit 时后端不可选
        return []
    out: List[KeyEvent] = []
    i, n = 0, len(presses)
    while i < n:
        press = presses[i]
        key, data = press.key, press.data or ""
        nxt = presses[i + 1] if i + 1 < n else None

        # Ctrl+Enter（pt 形态 Escape+ControlM）/ Alt+Enter 前缀折叠 → 换行
        # （Enter 直接发送；Shift+Enter 与 Enter 同形，无法区分）
        if key == _K.Escape and nxt is not None:
            if nxt.key == _K.ControlM:
                out.append(KeyEvent("newline", ""))
                _flog("fold: esc+c-m -> newline (ctrl+enter)")
                i += 2
                continue
            if _is_char_press(nxt.key, nxt.data):
                out.append(KeyEvent("char", nxt.key))
                _flog("fold: esc+%r -> char" % nxt.key)
                i += 2
                continue

        # 粘贴合成
        if key == _K.BracketedPaste:
            if data:
                out.append(KeyEvent("paste", data))
                _flog("paste data len=%d" % len(data))
                i += 1
                continue
            text: List[str] = []
            j = i + 1
            while j < n and _is_char_press(presses[j].key, presses[j].data):
                text.append(presses[j].key)
                j += 1
            out.append(KeyEvent("paste", "".join(text)))
            _flog("paste assembled len=%d" % len("".join(text)))
            i = j
            continue

        # conhost 粘贴折叠启发式：与逐键打字同形的剪贴板按键段折叠为单 paste 事件；
        # 刚发生过折叠时残余段按续接窗口降低阈值（长粘贴跨批次劈开的收尾）
        cont = (time.monotonic() - _LAST_PASTE[0]) < _PASTE_CONT_WINDOW
        folded = _fold_paste_run(presses, i, n, cont=cont)
        if folded is not None:
            text, j = folded
            out.append(KeyEvent("paste", text))
            _LAST_PASTE[0] = time.monotonic()
            _flog("paste folded len=%d cont=%s" % (len(text), cont))
            i = j
            continue

        ev = _press_to_event(key, data)
        if ev is not None:
            out.append(ev)
        else:
            _flog("drop key=%r data=%r" % (key, data))
        i += 1
    return out


def _press_to_event(key, data: str) -> Optional[KeyEvent]:
    # 鼠标
    if key == _K.WindowsMouseEvent:
        _flog("mouse data=%r" % (data,))
        return _mouse_event(data)

    # 枚举键优先（Keys 是 str 混入枚举，必须先于 isinstance(key, str) 判断）
    mapped = _KEY_MAP.get(key)
    if mapped is not None:
        return KeyEvent(*mapped)
    if key in _COMBO_MAP:
        name = _COMBO_MAP[key]
        name_s = str(key).lower()
        ctrl, alt, shift = "control" in name_s, "escape" in name_s, "shift" in name_s
        if not (ctrl or alt or shift):
            return KeyEvent(name, "")
        return _hotkey(name, ctrl, alt, shift)
    if key in _CTRL_LETTERS:
        return KeyEvent("hotkey", "ctrl+" + _CTRL_LETTERS[key])

    # 可见字符（含 CJK/IME 上屏）
    if isinstance(key, str):
        if _is_char_press(key, data):
            return KeyEvent("char", key)
    return None


# 枚举映射表在模块级构建（Windows 下必然可导入 prompt_toolkit）
try:
    from prompt_toolkit.keys import Keys as _K

    _KEY_MAP = {
        _K.Backspace: ("backspace", ""),
        _K.Delete: ("delete", ""),
        _K.Insert: ("hotkey", "insert"),
        _K.Tab: ("tab", ""),
        _K.BackTab: ("mode_switch", ""),  # Shift+Tab
        _K.Home: ("home", ""),
        _K.End: ("end", ""),
        _K.PageUp: ("scroll_up", ""),  # PageUp/PageDown 与 legacy 同名语义
        _K.PageDown: ("scroll_down", ""),
        _K.Escape: ("escape", ""),
        _K.ControlM: ("submit", ""),  # Enter
        _K.ControlJ: ("newline", ""),  # Ctrl+J：换行候选（legacy 路径丢弃该键）
        _K.ControlC: ("interrupt", ""),
        _K.ControlU: ("clear", ""),
    }

    # 方向/导航键及其修饰组合：枚举 → 语义名（修饰位从枚举名推导）
    _COMBO_MAP = {}
    for _base, _names in {
        "up": (_K.Up, _K.ShiftUp, _K.ControlUp, _K.ControlShiftUp),
        "down": (_K.Down, _K.ShiftDown, _K.ControlDown, _K.ControlShiftDown),
        "left": (_K.Left, _K.ShiftLeft, _K.ControlLeft, _K.ControlShiftLeft),
        "right": (_K.Right, _K.ShiftRight, _K.ControlRight, _K.ControlShiftRight),
        "home": (_K.Home, _K.ShiftHome, _K.ControlHome, _K.ControlShiftHome),
        "end": (_K.End, _K.ShiftEnd, _K.ControlEnd, _K.ControlShiftEnd),
    }.items():
        for _k in _names:
            if _k is not None:
                _COMBO_MAP[_k] = _base

    # Ctrl+字母组合（C/U/M/J 已在 _KEY_MAP 单列）
    _CTRL_LETTERS = {
        _K.ControlA: "a", _K.ControlB: "b", _K.ControlD: "d", _K.ControlE: "e",
        _K.ControlF: "f", _K.ControlG: "g", _K.ControlH: "h", _K.ControlI: "i",
        _K.ControlK: "k", _K.ControlL: "l", _K.ControlN: "n", _K.ControlO: "o",
        _K.ControlP: "p", _K.ControlQ: "q", _K.ControlR: "r", _K.ControlS: "s",
        _K.ControlT: "t", _K.ControlV: "v", _K.ControlW: "w", _K.ControlX: "x",
        _K.ControlY: "y", _K.ControlZ: "z",
    }
    # 粘贴折叠启发式（translate_key_presses）：分段键与爆发阈值
    _PASTE_BREAK_KEYS = {_K.Enter, _K.ControlM, _K.Tab, _K.ControlI}
    _PASTE_BURST_MIN = 20
    # F1-F24
    for _idx in range(1, 25):
        _fkey = getattr(_K, "F%d" % _idx, None)
        if _fkey is not None:
            _KEY_MAP[_fkey] = ("hotkey", "f%d" % _idx)
        _sfkey = getattr(_K, "ShiftF%d" % _idx, None)
        if _sfkey is not None:
            _KEY_MAP[_sfkey] = ("hotkey", "shift+f%d" % _idx)
        _cfkey = getattr(_K, "ControlF%d" % _idx, None)
        if _cfkey is not None:
            _COMBO_MAP[_cfkey] = "f%d" % _idx
except ImportError:  # pragma: no cover - 无 prompt_toolkit 时翻译层不可用
    _K = None
    _KEY_MAP, _COMBO_MAP, _CTRL_LETTERS = {}, {}, {}
    # 折叠启发式兜底常量（_K is None 时 translate 提前返回，永不触达；补全只为模块属性一致）
    _PASTE_BREAK_KEYS = set()
    _PASTE_BURST_MIN = 0


# ----- 强制事件记录路径（鼠标修复） -----

def _baw_win32_input_cls():
    """构造强制事件记录路径的 Win32Input 子类。

    pt 默认经 _is_win_vt100_input_enabled() 试探（临时设置 ENABLE_VIRTUAL_
    TERMINAL_INPUT）选择 Vt100ConsoleInputReader——该路径的 _get_keys 只解码
    KEY_EVENT，MOUSE_EVENT 记录被丢弃（滚轮/滑块拖拽全断）；VT 路径的鼠标
    需应用主动发 \\x1b[?1000h 上报序列，裸 reader 没有。此处强制回到
    ConsoleInputReader 事件记录路径：MOUSE_EVENT → WindowsMouseEvent →
    翻译层；raw_mode 也不再设置输入 VT。
    """
    try:
        from prompt_toolkit.input.win32 import ConsoleInputReader, Win32Input
    except ImportError:  # pragma: no cover
        return None

    class BawWin32Input(Win32Input):
        def __init__(self, stdin=None):
            super().__init__(stdin)
            self._use_virtual_terminal_input = False
            self.console_input_reader = ConsoleInputReader()

    return BawWin32Input


# ----- 读取循环（后台线程 + 队列，阻塞读键不阻塞 20fps 帧泵） -----

class _PtReader:
    def __init__(self) -> None:
        self._q: "queue.Queue[List]" = queue.Queue()
        self._input = None
        self._raw = None
        self._started = False
        self._lock = threading.Lock()
        # 暂停协议：内建 input() 临时接管控制台时停泵 + 恢复 cooked 模式
        self._pause_req = threading.Event()
        self._parked = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            from prompt_toolkit.input import create_input

            if _WINDOWS:
                cls = _baw_win32_input_cls()
                try:
                    self._input = (cls or create_input)()
                except Exception:
                    self._input = create_input()
            else:
                # POSIX：pt 的 Vt100 输入路径（键盘可用；无 Win32 事件记录，鼠标不支持）
                self._input = create_input()
            self._raw = self._input.raw_mode()
            self._raw.__enter__()  # raw 模式与 paused() 的暂挂/恢复配对
            threading.Thread(target=self._pump, daemon=True, name="bawcode-pt-input").start()
            log.info("pt 输入后端启动: %s", type(self._input).__name__)
            _flog("started reader=%s" % type(self._input).__name__)
        except Exception as exc:
            log.error("pt 输入后端启动失败: %r", exc)
            _flog("start failed: %r" % (exc,))

    def _pump(self) -> None:
        # POSIX 的 read_keys(timeout=None) 阻塞到有键，传小超时保证暂停协议与帧泵响应；
        # Windows 的 ConsoleInputReader 保持默认（timeout=None）不变
        timeout = None if _WINDOWS else 0.05
        try:
            while True:
                if self._pause_req.is_set():
                    self._parked.set()
                    time.sleep(0.02)
                    continue
                presses = self._input.read_keys(timeout)
                if presses:
                    self._q.put(presses)
                else:
                    # pt 的 read() 非阻塞（wait_for_handles timeout=0），
                    # 空转时休眠防占满 CPU 核
                    time.sleep(0.01)
        except Exception as exc:  # noqa: B014 - 后台线程兜底
            log.error("pt 读键线程退出: %r", exc)
            _flog("pump exit: %r" % (exc,))

    def read_events(self, timeout: float = 0.02) -> List[KeyEvent]:
        self.start()
        if self._input is None:
            return []
        out: List[KeyEvent] = []
        try:
            batch = self._q.get(timeout=timeout)
        except queue.Empty:
            return []
        out.extend(translate_key_presses(batch))
        while True:
            try:
                out.extend(translate_key_presses(self._q.get_nowait()))
            except queue.Empty:
                break
        return out

    def read_event(self, timeout: float = 0.02) -> KeyEvent:
        evs = self.read_events(timeout)
        return evs[0] if evs else TICK

    def flush_input(self) -> None:
        if self._input is not None:
            try:
                self._input.flush_keys()
            except Exception:
                pass
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return


_reader: Optional[_PtReader] = None


def get_reader() -> "_PtReader":
    global _reader
    if _reader is None:
        _reader = _PtReader()
    return _reader


# ----- 公开契约（与 keyinput 函数签名一致） -----

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


@contextmanager
def paused():
    """暂停输入泵并恢复行缓冲模式：内建 input() 临时接管控制台。

    raw_mode 关闭了 ENABLE_LINE_INPUT/ECHO 且泵线程会抽干输入缓冲，
    任何绕过本模块的内建 input() 都必须在 paused() 内使用。
    退出时恢复 raw 模式并清空 input() 期间的残留输入。"""
    r = get_reader()
    r.start()
    if r._input is None or r._raw is None:
        yield
        return
    r._pause_req.set()
    r._parked.wait(1.0)
    try:
        try:
            r._raw.__exit__()
        except Exception:
            pass
        try:
            yield
        finally:
            try:
                r._raw.__enter__()
            except Exception:
                pass
    finally:
        r._parked.clear()
        r._pause_req.clear()
        try:
            flush_input()
        except Exception:
            pass


def direction_of(kind: str, value: str) -> Optional[str]:
    if kind in ("up", "down", "left", "right"):
        return kind
    if kind == "hotkey":
        v = str(value or "").lower().strip()
        for d in ("up", "down", "left", "right"):
            if v == d or v.endswith("+" + d):
                return d
    return None
