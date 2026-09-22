"""键盘输入统一出口：ui 通过 read_events / read_key_event 读键。

=============================================================================
架构（Windows 主路径，TMP terminal-music-player 同形）
=============================================================================
  ① 采集层  _fill_win —— 滴灌：每次调用至多消费一个逻辑单元
     msvcrt.getch() 字节读取。0x00/0xE0 前缀立即守卫配对（无配对零等待
     丢弃——IME 删除时的握手字节）；0x80-0xFF 字节（IME 上屏中文按
     console CP 拆成逐字节投递）按 TMP 同款守卫读法立即读齐尾字节后统一
     解码（GBK 优先）。**绝不一帧抽干**：上屏批次按 50ms/字符逐帧消费——
     realreader 二分实证，瞬间抽干上屏批次会让 conhost/TSF 把紧随的退格
     扣押到下次上屏才放行（表现：删不掉、打新字时所有积压退格一次生效）。
     粘贴爆发例外（队列事件数 ≥ PASTE_BURST_EVENTS）：整批快速收流。
  ② 解析层  _parse_win_buffer
     前缀+扫描码 → 方向键/功能键；ESC[ → CSI（ConPTY）；控制键；可打印
     字符。粘贴判定改流式：**批次中部的 \r \n \t 是粘贴内容**（收流合成
     paste 事件，绝不触发 submit，教训 4），批次末尾的 \r 才是提交——
     「IME 上屏后立即回车」的回车必为批次末尾，不受影响。
  ③ 事件层  read_events / read_key_event
     read_events 每帧只碰一次控制台队列；_buf/爆发收流中已消费的内容
     同帧交付（那不是队列消费，无扣押风险）。

=============================================================================
历史教训（改动前必读，详见 review.md）
=============================================================================
1. 读键 API 必须 getch/getwch 二选一并全文件一致（含 flush_input）——
   混用会导致控制台队列 ANSI/宽字符记录错位；
2. 对"等另一半序列"的任何等待都必须有超时或零等待出口：IME 删除上屏字符
   时会注入孤立 \x00 握手字节，等待会挂起输入（实测 1.3s+）；
3. 中文上屏可能被 conhost 按 console CP 拆成逐字节（GBK 2 字节/UTF-8 3 字节），
   必须重组后统一解码——中途解码会让 GBK 在 UTF-8 多字节中途抢跑解出乱码；
4. bracketed-paste（ESC[200~/201~）与流式爆发收流的内容是数据不是按键，
   绝不能触发 submit；
5. timeout<=0 必须真非阻塞（固定帧循环由帧尾 sleep 控制节拍）；
6. 读键必须滴灌（TMP 形）：每帧至多一个逻辑单元。抽干/批量消费会触发
   conhost/TSF 对"上屏后退格"的扣押（2026-09-21 realreader 二分实证）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

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

Event = Tuple[str, str]
TICK: Event = ("tick", "")

# Windows 控制台 Enter 修饰键（Shift+Enter → newline）
_INPUT_RECORD_KEY_EVENT = 0x0001
_VK_RETURN = 0x0D
_SHIFT_PRESSED = 0x0010
_CTRL_PRESSED = 0x0008 | 0x0004  # LEFT|RIGHT_CTRL
_ALT_PRESSED = 0x0002 | 0x0001  # LEFT|RIGHT_ALT

# 半包/缓冲超时出口：任何"等另一半序列"的逻辑都必须有超时（教训 2）
HALF_TIMEOUT = 0.2      # CSI / 前缀半包保留上限，超时强制决断
PEND_MAX = 8            # DBCS 重组缓冲字节上限
PEND_TIMEOUT = 0.1      # DBCS 重组超时，超时按 latin-1 落地不丢字
PASTE_IDLE_TIMEOUT = 1.0  # bracketed-paste end 丢失时强制 flush 的空闲超时
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"
PASTE_BURST_EVENTS = 32   # 队列积压事件数达到此值判为粘贴爆发（整批收流）
BURST_DRAIN_LIMIT = 65536  # 爆发收流单次抽干字节上限

# ---- 输入诊断日志：BAW_LOG_KEYINPUT=1 时写入 develop/keyinput.log ----
_LOG_ON = os.environ.get("BAW_LOG_KEYINPUT") == "1"
_LOG_PATH = Path(__file__).resolve().parent.parent / "develop" / "keyinput.log"


def _log(msg: str) -> None:
    if not _LOG_ON:
        return
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.time():.3f}\t{msg}\n")
    except Exception:
        pass


def _log_console_info() -> None:
    if not _LOG_ON or sys.platform != "win32":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        _log(
            f"console ACP={k32.GetACP()} OEM={k32.GetOEMCP()} "
            f"inCP={k32.GetConsoleCP()} outCP={k32.GetConsoleOutputCP()}"
        )
        _log(
            f"python stdout={sys.stdout.encoding!r} stdin={sys.stdin.encoding!r} "
            f"utf8_mode={sys.flags.utf8_mode} tty={sys.stdin.isatty()}"
        )
    except Exception:
        pass


def _decode_encodings() -> List[str]:
    """conhost DBCS 逐字节重组的解码候选：控制台 CP 优先，再 utf-8 / gbk"""
    encs: List[str] = []
    try:
        import ctypes

        cp = ctypes.windll.kernel32.GetConsoleCP()
    except Exception:
        cp = 0
    if cp == 65001:
        encs.append("utf-8")
    elif cp and cp != 65001:
        encs.append(f"cp{cp}")
    for fallback in ("utf-8", "gbk"):
        if fallback not in encs:
            encs.append(fallback)
    return encs


def _expected_trails(lead: int) -> int:
    """console CP 决定的多字节尾字节数：GBK 等双字节编码固定 1；
    UTF-8（inCP=65001）按首字节位宽 1-3。"""
    if lead < 0x80:
        return 0
    try:
        import ctypes

        cp = ctypes.windll.kernel32.GetConsoleCP()
    except Exception:
        cp = 0
    if cp == 65001:
        if lead >= 0xF0:
            return 3
        if lead >= 0xE0:
            return 2
        if lead >= 0xC0:
            return 1
        return 0
    return 1


# Windows 扫描码 → kind（与 music player 的 _WIN_SCAN_MAP 同族）
_WIN_SCAN = {
    72: "up",
    80: "down",
    75: "left",
    77: "right",
    83: "delete",
    71: "home",
    79: "end",
    73: "scroll_up",
    81: "scroll_down",
    82: "hotkey_insert",
    59: "hotkey_f1",
    60: "hotkey_f2",
    61: "hotkey_f3",
    62: "hotkey_f4",
    63: "hotkey_f5",
    64: "hotkey_f6",
    65: "hotkey_f7",
    66: "hotkey_f8",
    67: "hotkey_f9",
    68: "hotkey_f10",
    87: "hotkey_f11",
    88: "hotkey_f12",
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


def _kind_event(name: str) -> Event:
    if name.startswith("hotkey_"):
        return ("hotkey", name.split("_", 1)[1])
    return (name, "")


def _scan_event(code: int) -> Event:
    return _kind_event(_WIN_SCAN[code]) if code in _WIN_SCAN else TICK


def _kbhit() -> bool:
    return bool(msvcrt.kbhit()) if _WINDOWS else False


def _peek_enter_mod() -> Optional[str]:
    """窥视控制台队列中下一个按键是否为 Enter 及修饰键。

    返回 ``enter`` / ``shift+enter`` / ``ctrl+enter``；非 Enter 或失败返回 None。
    仅 Windows 有效；用于在 getch 消费 ``\\r``/``\\n`` 前区分 Shift+Enter。
    """
    if not _WINDOWS:
        return None
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
            _fields_ = [
                ("EventType", ctypes.c_ushort),
                ("Event", _INPUT_UNION),
            ]

        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.restype = ctypes.c_void_p
        handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
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


class KeyReader:
    """控制台输入读取器。

    状态字段五组：
    - _buf:            待解析字符队列（_fill_win 采集 → _parse_win_buffer 消费）
    - _pend/_pend_at:  DBCS 孤立首字节缓冲及时间戳（超时 latin-1 兜底）
    - _half_at:        半包首次形成时间戳（超时决断防假死）
    - _pasting/_paste_chunks/_paste_at: bracketed-paste 收流态
    - _burst/_burst_text: 粘贴爆发收流及已冲洗待交付的 paste 文本
    """

    def __init__(self):
        self._fd = None
        self._old_settings = None
        self._buf: List[str] = []
        self._pend: bytes = b""  # DBCS 孤立首字节缓冲
        self._pend_at: Optional[float] = None
        self._half_at: Optional[float] = None
        # bracketed-paste 状态机：paste_start/end 之间字节整体合成一个 paste 事件
        self._pasting = False
        self._paste_chunks: List[str] = []
        self._paste_at = 0.0
        # 粘贴爆发收流（大段粘贴整批快速收流，与 IME 滴灌互斥）
        self._burst: Optional[bytearray] = None
        self._burst_text: Optional[str] = None
        # IME 握手放行计时：孤立 \x00 丢弃时刻 → 下一个 raw 单元的间隔，
        # 用于量化 IME/conhost 扣押删除序列的时长（验证日志直接可读）
        self._lone_prefix_at: Optional[float] = None
        # 预分类事件（如 Shift+Enter → newline），优先于字节解析交付
        self._pending: List[Event] = []
        _log("KeyReader init")
        _log_console_info()

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
        return None if ev == TICK else ev

    def read_event(self, timeout: float = 0.02) -> Event:
        if _WINDOWS:
            ev = self._read_windows(timeout)
        else:
            ev = self._read_unix(timeout)
        if ev != TICK:
            _log(f"event {ev[0]} {ev[1]!r}")
        return ev

    def flush_input(self) -> None:
        """丢弃缓冲与队列中未处理按键（进入设置等界面时用）。

        清队列必须用 getch（与 _fill_win 同 API，教训 1）。"""
        self._buf.clear()
        self._pend = b""
        self._pend_at = None
        self._half_at = None
        self._pasting = False
        self._paste_chunks = []
        self._paste_at = 0.0
        self._burst = None
        self._burst_text = None
        self._lone_prefix_at = None
        self._pending.clear()
        if _WINDOWS:
            try:
                while msvcrt.kbhit():
                    msvcrt.getch()
            except Exception:
                pass

    def _decode_pend(self) -> Optional[str]:
        """对重组缓冲做严格全量解码；未凑齐/全部失败返回 None"""
        return self._decode_bytes(self._pend)

    def _decode_bytes(self, data: bytes) -> Optional[str]:
        """严格全量解码（console CP 优先，utf-8/gbk 兜底）；失败返回 None"""
        if not data:
            return None
        for enc in _decode_encodings():
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return None

    # ----- TMP 形滴灌采集原语 -----
    @staticmethod
    def _getch_char() -> str:
        raw = msvcrt.getch()
        if isinstance(raw, bytes):
            return chr(raw[0]) if raw else ""
        return raw

    @staticmethod
    def _getch_guarded(grace: float = 0.005) -> Optional[str]:
        """kbhit 守卫读一字节（TMP _read_mb_char_windows 同款）：
        无键时等 grace 秒再查一次，仍无返回 None——绝不无界阻塞（教训 2）。"""
        if _kbhit():
            return KeyReader._getch_char()
        if grace > 0:
            time.sleep(grace)
            if _kbhit():
                return KeyReader._getch_char()
        return None

    @staticmethod
    def _pending_console_events() -> int:
        """控制台输入队列积压事件数（粘贴爆发检测用）；失败返回 0 走滴灌"""
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            k32.GetStdHandle.restype = ctypes.c_void_p
            handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            count = ctypes.c_uint32()
            if k32.GetNumberOfConsoleInputEvents(handle, ctypes.byref(count)):
                return count.value
        except Exception:
            pass
        return 0

    def _pend_timeout_check(self) -> None:
        """DBCS 重组缓冲超时/超限出口：不可解码时按 latin-1 落地，不丢字不滞留"""
        if not self._pend:
            self._pend_at = None
            return
        now = time.time()
        overdue = self._pend_at is None or (now - self._pend_at) > PEND_TIMEOUT
        if not overdue and len(self._pend) <= PEND_MAX:
            return
        decoded = self._decode_pend()
        if decoded is not None:
            _log(f"dbcs decoded {decoded!r}")
            self._buf.append(decoded)
        else:
            self._buf.append("".join(chr(b) for b in self._pend))
            _log(f"dbcs timeout fallback {[hex(b) for b in self._pend]}")
        self._pend = b""
        self._pend_at = None

    # ----- bracketed-paste 收流（粘贴走字节层，绝不触发按键语义） -----
    @staticmethod
    def _trailing_partial(s: str, marker: str) -> int:
        """s 尾部与 marker 前缀的最长匹配长度（可能是尚未到齐的 end 标记）"""
        for keep in range(min(len(s), len(marker) - 1), 0, -1):
            if marker.startswith(s[-keep:]):
                return keep
        return 0

    def _decode_str_maybe_dbcs(self, s: str) -> str:
        """粘贴内容若含 0x80-0xFF 伪字节，按 console CP 尝试解码还原"""
        if not any(0x80 <= ord(c) <= 0xFF for c in s):
            return s
        try:
            data = s.encode("latin-1")
        except UnicodeEncodeError:
            return s
        for enc in _decode_encodings():
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return s

    def _flush_paste_event(self) -> Event:
        raw = "".join(self._paste_chunks)
        self._paste_chunks = []
        self._pasting = False
        self._paste_at = 0.0
        text = self._decode_str_maybe_dbcs(raw)
        _log(f"paste flushed {len(text)} chars")
        return ("paste", text)

    # ----- Windows：TMP 形滴灌采集 + DBCS 重组 + 粘贴收流 -----
    def _fill_win(self, wait: float = 0.0) -> None:
        """TMP 形滴灌采集：每次调用至多从控制台队列消费一个逻辑单元。

        - 单字节键：kbhit → getch 入 _buf；
        - 0x00/0xE0 前缀：立即守卫查一次，有配对则成对入 _buf，无则零等待
          丢弃（IME 握手字节，教训 2）；
        - 0x80-0xFF（IME 上屏中文按 console CP 拆散的形态）：TMP 同款守卫
          读法立即读齐尾字节（kbhit→getch，无则 5ms grace）后统一解码入
          _buf（教训 3）；凑不齐入 _pend 超时兜底，不丢字；
        - 粘贴爆发（队列积压事件数 ≥ PASTE_BURST_EVENTS，且非 bracketed
          收流态）：整批抽干入 _burst——粘贴不是 IME 上屏，无扣押风险，
          且大段文本不能按 50ms/字符滴灌。

        绝不一帧抽干常规键流（教训 6）：realreader 二分实证，上屏批次被
        瞬间抽干时 conhost/TSF 会扣押紧随的退格到下次上屏才放行。
        wait>0 时先同步等待至多 wait 秒（CSI 半包续读场景）；主路径 wait=0。
        """
        if not _WINDOWS:
            return
        try:
            if wait > 0:
                end = time.time() + wait
                while time.time() < end and not msvcrt.kbhit():
                    time.sleep(0.002)
            self._pend_timeout_check()
            if self._burst is not None:
                self._drain_burst()
                return
            if not _kbhit():
                return
            # Enter 修饰键：Shift+Enter 预分类为 newline（产品约定 Enter=提交）
            enter_mod = _peek_enter_mod()
            if enter_mod == "shift+enter" and not self._pasting:
                self._getch_char()  # 消费 \r/\n
                self._pending.append(("newline", ""))
                _log("peek shift+enter -> newline")
                return
            if not self._pasting and self._pending_console_events() >= PASTE_BURST_EVENTS:
                self._burst = bytearray()
                self._drain_burst()
                return
            ch = self._getch_char()
            if not ch:
                return
            _log(f"raw unit {ch!r} U+{ord(ch):04X}")
            if self._lone_prefix_at is not None:
                _log(f"post-handshake unit after {time.time() - self._lone_prefix_at:.3f}s")
                self._lone_prefix_at = None
            if self._pasting:
                # bracketed-paste 收流：一切字节只进粘贴缓冲，不产生按键语义（教训 4）
                self._paste_chunks.append(ch)
                self._paste_at = time.time()
                return
            if ch in ("\x00", "\xe0"):
                # 扩展键前缀：立即守卫查一次（不等待），有配对成对入 _buf；
                # 无配对零等待丢弃——等待会让 IME 挂起（教训 2）
                nxt = self._getch_guarded(grace=0.0)
                if nxt is None:
                    self._lone_prefix_at = time.time()
                    _log(f"lone prefix {ch!r} dropped (ime-handshake)")
                    return
                self._buf.append(ch)
                self._buf.append(nxt)
                return
            lead = ord(ch)
            if 0x80 <= lead <= 0xFF:
                # DBCS/UTF-8：TMP 同款立即读齐尾字节后统一解码（教训 3）
                data = bytearray([lead])
                for _ in range(_expected_trails(lead)):
                    nxt = self._getch_guarded(grace=0.005)
                    if nxt is None:
                        break
                    data.append(ord(nxt))
                decoded = self._decode_bytes(bytes(data))
                if decoded is not None:
                    _log(f"dbcs decoded {decoded!r} from {bytes(data).hex(' ')}")
                    self._buf.append(decoded)
                else:
                    self._pend = bytes(data)
                    self._pend_at = time.time()
                    _log(f"dbcs pending {[hex(b) for b in data]}")
                return
            self._buf.append(ch)
        except Exception:
            pass

    def _drain_burst(self) -> None:
        """爆发收流：把队列中可得的字节整批抽入 _burst（上限 BURST_DRAIN_LIMIT）；
        队列抽干（或到上限）后合成一个 paste 事件——内容是数据不是按键（教训 4）。"""
        if self._burst is None:
            return
        try:
            while _kbhit() and len(self._burst) < BURST_DRAIN_LIMIT:
                self._burst += self._getch_char().encode("latin-1", "replace")
        except Exception:
            pass
        if not _kbhit() or len(self._burst) >= BURST_DRAIN_LIMIT:
            raw = bytes(self._burst)
            self._burst = None
            text = self._decode_str_maybe_dbcs(raw.decode("latin-1"))
            _log(f"burst paste flushed {len(text)} chars")
            self._burst_text = text

    def _start_burst(self, first: bytes) -> None:
        """流式粘贴收流入口：批次中部的 \\r/\\n/\\t 已确定是内容而非按键"""
        self._burst = bytearray(first)
        _log(f"burst start from {first!r}")
        self._drain_burst()

    def _read_windows(self, timeout: float = 0.02) -> Event:
        """主读取循环：返回一个事件，无键返回 TICK。

        timeout<=0 真非阻塞（帧循环供拍，教训 5）；timeout>0 无键时
        sleep(timeout)——这是主循环的心跳节拍。采集为 TMP 形滴灌：
        每帧至多从队列消费一个逻辑单元（教训 6）。
        """
        try:
            now = time.time()
            # 预分类事件（Shift+Enter 等）优先交付
            if self._pending:
                return self._pending.pop(0)
            # 已冲洗的爆发粘贴：优先交付
            if self._burst_text is not None:
                text = self._burst_text
                self._burst_text = None
                return ("paste", text)
            # 粘贴 idle 超时：end 标记丢失（终端 bug）时强制 flush
            if self._pasting and self._paste_chunks and (now - self._paste_at) > PASTE_IDLE_TIMEOUT:
                return self._flush_paste_event()
            self._pend_timeout_check()
            # 已有半包：先续读再解析，避免方向键/CSI 被丢
            if self._buf:
                ev = self._parse_win_buffer()
                if ev != TICK:
                    return ev
                if self._buf:
                    self._fill_win(max(0.01, timeout))
                    if self._burst_text is not None:
                        text = self._burst_text
                        self._burst_text = None
                        return ("paste", text)
                    if self._buf:
                        return self._parse_win_buffer()
                return TICK

            if not _kbhit():
                if timeout <= 0:
                    return TICK
                time.sleep(timeout)
                if not _kbhit():
                    return TICK
            self._fill_win(0.0)
            if self._burst_text is not None:
                text = self._burst_text
                self._burst_text = None
                return ("paste", text)
            if not self._buf:
                return TICK
            ev = self._parse_win_buffer()
            if ev != TICK:
                return ev
            if self._burst_text is not None:
                text = self._burst_text
                self._burst_text = None
                return ("paste", text)
            return TICK
        except Exception:
            self._buf.clear()
            return TICK

    def _parse_win_buffer(self) -> Event:
        if not self._buf:
            return TICK

        # bracketed-paste 收流态：_buf 内容整体进粘贴缓冲，只找 end 标记
        if self._pasting:
            s = "".join(self._buf)
            # Ctrl+C 逃生舱优先于粘贴收流：截断粘贴、flush 已收内容
            i03 = s.find("\x03")
            if i03 >= 0:
                self._paste_chunks.append(s[:i03])
                self._buf.clear()
                self._buf.append("\x03")
                _log("paste interrupted by Ctrl+C")
                return self._flush_paste_event()
            end_idx = s.find(PASTE_END)
            if end_idx >= 0:
                self._paste_chunks.append(s[:end_idx])
                self._buf.clear()
                return self._flush_paste_event()
            # 尾部可能是半截 end 标记：留在 _buf 等待，其余收流
            keep = self._trailing_partial(s, PASTE_END)
            take = len(s) - keep
            if take > 0:
                self._paste_chunks.append(s[:take])
                del self._buf[:take]
                self._paste_at = time.time()
            return TICK

        ch = self._buf[0]

        # 扩展键前缀 0x00 / 0xE0
        # 两个来源：① 真扩展键（方向键等）：前缀+扫描码同批到达；
        # ② IME 握手字节：删除上屏字符时注入孤立 \x00（无配对字节）。
        # 处理原则：立即查一次队列，有配对则按扫描码解析；无配对则零等待
        # 丢弃——任何等待都会让 IME 挂起到自身超时（实测 1.3s+，教训 2）。
        if ch in ("\x00", "\xe0"):
            if len(self._buf) < 2:
                self._fill_win(0.0)  # 立即查一次，不等待
            if len(self._buf) < 2:
                self._buf.pop(0)
                self._lone_prefix_at = time.time()
                _log(f"lone prefix {ch!r} dropped (ime-handshake)")
                return TICK
            self._half_at = None
            ch2 = self._buf[1]
            code2 = ord(ch2)
            if ch == "\xe0" and code2 != 15 and code2 not in _WIN_SCAN:
                # 0xE0 配对的不是扫描码：按普通字符（à）放行，避免吞字
                self._buf.pop(0)
                return ("char", ch)
            del self._buf[:2]
            if ch2 == "\x0f" or code2 == 15:
                return ("mode_switch", "")
            return _scan_event(code2)

        if ch == "\x1b":
            return self._parse_csi_win()

        if ch == "\r":
            self._buf.pop(0)
            # 最小改动：仅已在粘贴/爆发收流时，回车视为粘贴内容（教训 4）。
            # 非粘贴态的 Enter 一律提交，避免控制台残留字节导致真实回车被吞。
            if self._pasting or self._burst is not None:
                self._start_burst(b"\r")
                return TICK
            return ("submit", "")
        if ch == "\n":
            self._buf.pop(0)
            if self._pasting or self._burst is not None:
                self._start_burst(b"\n")
                return TICK
            return ("submit_ctrl", "")
        if ch == "\t":
            self._buf.pop(0)
            if _kbhit():
                self._start_burst(b"\t")
                return TICK
            return ("tab", "")
        if ch in ("\x08", "\x7f"):
            self._buf.pop(0)
            return ("backspace", "")
        if ch == "\x03":
            self._buf.pop(0)
            return ("interrupt", "")
        if ch == "\x15":
            self._buf.pop(0)
            return ("clear", "")
        ctrl = _CTRL.get(ch)
        if ctrl:
            self._buf.pop(0)
            return ("hotkey", ctrl)

        # 可打印字符（含中文）：合并 _buf 中已收流的连续可打印单元。
        # 不再向队列追加读取——队列消费必须保持滴灌节拍（教训 6）
        if ch and all(ord(c) >= 32 for c in ch):
            parts = [ch]
            self._buf.pop(0)
            while self._buf:
                n = self._buf[0]
                if not n or n in ("\x00", "\xe0", "\x1b", "\r", "\n", "\t") or not all(
                    ord(c) >= 32 for c in n
                ):
                    break
                parts.append(self._buf.pop(0))
            return ("char", "".join(parts))

        self._buf.pop(0)
        return TICK

    def _half_deadline_passed(self) -> bool:
        """半包超时判断：首次滞留记时间戳，超 HALF_TIMEOUT 返回 True（决断出口）"""
        if self._half_at is None:
            self._half_at = time.time()
            return False
        if time.time() - self._half_at > HALF_TIMEOUT:
            self._half_at = None
            return True
        return False

    def _parse_csi_win(self) -> Event:
        """解析 ESC 后续序列（CSI/SS3/单独 Esc）。

        半包策略：可能继续到达的 CSI 前缀保留缓冲返回 TICK，下一帧续读；
        仅「确认单独 Esc」或「ESC+无法形成序列」返回 escape。
        超时出口：半包滞留超 HALF_TIMEOUT 强制按 Esc 决断（教训 2）。
        """
        if len(self._buf) < 2:
            self._fill_win(0.04)
        if len(self._buf) < 2:
            self._buf.pop(0)
            self._half_at = None
            return ("escape", "")

        nxt = self._buf[1]

        # SS3: ESC O X（应用光标键）
        if nxt == "O":
            if len(self._buf) < 3:
                self._fill_win(0.04)
            if len(self._buf) < 3:
                if self._half_deadline_passed():
                    self._buf.clear()
                    _log("half timeout ESC O -> escape")
                    return ("escape", "")
                return TICK
            self._half_at = None
            code = self._buf[2]
            del self._buf[:3]
            name = _CSI_FINAL.get(code)
            return _kind_event(name) if name else TICK

        if nxt != "[":
            del self._buf[:2]
            self._half_at = None
            return ("escape", "")

        # CSI: ESC [ params final
        if len(self._buf) < 3:
            self._fill_win(0.04)
        if len(self._buf) < 3:
            if self._half_deadline_passed():
                self._buf.clear()
                _log("half timeout ESC [ -> escape")
                return ("escape", "")
            return TICK
        self._half_at = None

        idx = 2
        params = ""
        while idx < len(self._buf):
            c = self._buf[idx]
            if c.isdigit() or c == ";":
                params += c
                idx += 1
                continue
            break

        if idx >= len(self._buf):
            self._fill_win(0.04)
            idx = 2
            params = ""
            while idx < len(self._buf):
                c = self._buf[idx]
                if c.isdigit() or c == ";":
                    params += c
                    idx += 1
                    continue
                break
            if idx >= len(self._buf):
                # 参数滞留超时出口：强制按 Esc 决断
                if self._half_deadline_passed():
                    self._buf.clear()
                    _log("half timeout CSI params -> escape")
                    return ("escape", "")
                return TICK

        final = self._buf[idx]
        # 非法终止符：若像新的 ESC，只丢掉 ESC [ 前缀，让后续字节重新解析
        if final not in _CSI_FINAL and final != "~" and not final.isalpha():
            if final == "\x1b":
                del self._buf[:2]
                return TICK
            del self._buf[: idx + 1]
            return TICK

        del self._buf[: idx + 1]

        if final == "~":
            num = params.split(";")[0] if params else ""
            name = _CSI_TILDE.get(num)
            if not name:
                return TICK
            if name == "paste_start":
                self._pasting = True
                self._paste_at = time.time()
                _log("paste start")
                return TICK
            if name == "paste_end":
                _log("paste end (stale)")
                return TICK
            ev = _kind_event(name)
            parts = [p for p in params.split(";") if p != ""] if params else []
            if len(parts) >= 2 and ev[0] == "hotkey":
                mod = _MOD.get(parts[-1], "")
                if mod:
                    return ("hotkey", mod + ev[1])
            return ev

        if final == "Z":
            return ("mode_switch", "")

        # CSI u（kitty/部分终端）：ESC [ 13 ; mod u  → Enter 变体
        if final == "u":
            parts = [p for p in params.split(";") if p != ""] if params else []
            if parts and parts[0] == "13":
                mod = _MOD.get(parts[-1], "") if len(parts) >= 2 else ""
                if mod == "shift+":
                    return ("newline", "")
                if mod == "ctrl+":
                    return ("submit_ctrl", "")
                return ("submit", "")
            return TICK

        if final in _CSI_FINAL:
            name = _CSI_FINAL[final]
            if name == "mode_switch":
                return ("mode_switch", "")
            parts = [p for p in params.split(";") if p != ""] if params else []
            mod = ""
            if len(parts) >= 2:
                mod = _MOD.get(parts[-1], "")
            if name in ("up", "down", "left", "right", "home", "end"):
                if not mod:
                    return (name, "")
                return ("hotkey", mod + name)
            return _kind_event(name)

        return TICK

    # ----- Unix 路径（保留原有实现） -----
    def _read_unix(self, timeout: float) -> Event:
        import select

        if not _UNIX or not sys.stdin.isatty():
            return TICK
        try:
            r, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout))
            if not r:
                return TICK
            ch = sys.stdin.read(1)
            if ch == "\n":
                return ("submit_ctrl", "")
            if ch == "\r":
                return ("submit", "")
            if ch == "\t":
                return ("tab", "")
            if ch in ("\x7f", "\x08"):
                return ("backspace", "")
            if ch == "\x03":
                return ("interrupt", "")
            if ch == "\x15":
                return ("clear", "")
            if ch == "\x1b":
                r2, _, _ = select.select([sys.stdin], [], [], 0.02)
                if not r2:
                    return ("escape", "")
                nxt = sys.stdin.read(1)
                if nxt == "Z":
                    return ("mode_switch", "")
                if nxt == "O":
                    r3, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if not r3:
                        return ("escape", "")
                    code = sys.stdin.read(1)
                    name = _CSI_FINAL.get(code)
                    return _kind_event(name) if name else TICK
                if nxt == "[":
                    params = ""
                    for _ in range(16):
                        r3, _, _ = select.select([sys.stdin], [], [], 0.02)
                        if not r3:
                            return TICK
                        code = sys.stdin.read(1)
                        if code.isdigit() or code == ";":
                            params += code
                            continue
                        if code == "~":
                            num = params.split(";")[0] if params else ""
                            name = _CSI_TILDE.get(num)
                            return _kind_event(name) if name else TICK
                        if code == "Z":
                            return ("mode_switch", "")
                        if code == "u":
                            parts = [p for p in params.split(";") if p != ""]
                            if parts and parts[0] == "13":
                                mod_u = _MOD.get(parts[-1], "") if len(parts) >= 2 else ""
                                if mod_u == "shift+":
                                    return ("newline", "")
                                if mod_u == "ctrl+":
                                    return ("submit_ctrl", "")
                                return ("submit", "")
                            return TICK
                        name = _CSI_FINAL.get(code)
                        if not name:
                            return TICK
                        parts = [p for p in params.split(";") if p != ""]
                        mod = _MOD.get(parts[-1], "") if len(parts) >= 2 else ""
                        if name in ("up", "down", "left", "right", "home", "end") and not mod:
                            return (name, "")
                        return _kind_event(name) if not mod else ("hotkey", mod + name)
                    return TICK
                return ("escape", "")
            if ch in _CTRL:
                return ("hotkey", _CTRL[ch])
            if ch and all(ord(c) >= 32 for c in ch):
                if ord(ch) >= 0x80:
                    extra = 3 if ord(ch) >= 0xF0 else 2 if ord(ch) >= 0xE0 else 1
                    raw = ch
                    for _ in range(extra):
                        r4, _, _ = select.select([sys.stdin], [], [], 0.02)
                        if not r4:
                            break
                        raw += sys.stdin.read(1)
                    return ("char", raw)
                return ("char", ch)
            return TICK
        except Exception:
            return TICK


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
    """批处理读取（滴灌形，教训 6）：每帧只从控制台队列消费一个逻辑单元，
    队列中积压的键由后续帧逐个交付——瞬间抽干上屏批次会让 conhost/TSF
    扣押紧随的退格（realreader 二分实证；TMP 形为证）。_buf/爆发收流/
    _pending 中已消费的内容同帧交付（那不是队列消费，无扣押风险）。
    timeout<=0 真非阻塞（帧循环供拍）。粘贴收流态不视为队列空闲。"""
    reader = get_reader()
    events: List[Event] = []
    ev = reader.read_event(timeout)
    while ev != TICK:
        events.append(ev)
        if reader._pending:
            ev = reader.read_event(0.0)
            continue
        if reader._burst_text is not None:
            # 爆发粘贴已冲洗待交付：同帧交付完（不碰队列）
            ev = reader.read_event(0.0)
            continue
        if not reader._buf:
            break  # 队列消费权留给下一帧（滴灌节拍）
        ev = reader.read_event(0.0)
    return events


def read_key(timeout: float = 0.0) -> Optional[Event]:
    ev = read_key_event(timeout)
    return None if ev == TICK else ev


def flush_input() -> None:
    get_reader().flush_input()


def direction_of(kind: str, value: str) -> Optional[str]:
    """从事件中取出方向名：支持 kind=up… 与 hotkey=ctrl+up / up。"""
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
        "paste",
    )
