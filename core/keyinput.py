"""键盘输入统一出口：ui 通过 read_events / read_key_event 读键。

=============================================================================
架构总览（三层流水线，风格对齐 terminal-music-player 的 mp/keyinput.py）
=============================================================================

  物理按键 / IME 上屏
    │  控制台输入队列（KEY_EVENT / 粘贴标记 / IME 握手字节）
    ▼
  ① 采集层  _fill_win (line ~340)
    │  msvcrt.getch() 字节读取
    │  - 0x80-0xFF 字节 → _pend 重组缓冲（conhost 把 IME 中文按 console CP
    │    拆成逐字节投递；批次末统一按 console CP（GBK 优先）解码还原）
    │  - 其他字节直接入 _buf
    ▼
  ② 解析层  _parse_win_buffer (line ~433)
    │  - 0xE0/0x00 前缀 + 扫描码 → 方向键/功能键（TMP 主路径）
    │    ★ 孤立前缀（IME 删除时的握手字节 NUL）零等待丢弃——
    │      等待会让 IME 挂起到自身超时（实测 1.3s+）才放行真实按键
    │  - ESC [ ... → CSI（方向键/功能键/粘贴标记/Shift+Tab）
    │  - \r → submit；\x08/\x7f → backspace；可打印串合并为一次 char
    ▼
  ③ 事件层  read_events (line ~775)
    │  一次抽干输入队列返回 Event 列表，供主循环批量消费、统一绘制
    ▼
  Event = (kind, value)；kind ∈ char/submit/backspace/up/down/.../paste/tick

=============================================================================
历史教训
=============================================================================
1. 读键 API 必须 getch/getwch 二选一并全文件一致（含 flush_input）——
   混用会导致控制台队列 ANSI/宽字符记录错位；
2. 对"等另一半序列"的任何等待都必须有超时或零等待出口：IME 删除上屏字符
   时会注入孤立 \x00 握手字节，等待会挂起输入（曾实测 1.3s+）；
3. 中文上屏可能被 conhost 按 console CP 拆成逐字节（GBK 2 字节/UTF-8 3 字节），
   必须重组后统一解码——中途解码会让 GBK 在 UTF-8 多字节中途抢跑解出乱码；
4. bracketed-paste（ESC[200~/201~）内容是数据不是按键，绝不能触发 submit。
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

# 半包/缓冲超时出口（对齐 qwen-code：任何"等另一半序列"的逻辑都必须有超时，
# 否则丢半个序列 = 输入假死）
HALF_TIMEOUT = 0.2      # CSI / 0xE0 前缀半包保留上限，超时强制决断
PEND_MAX = 8            # DBCS 重组缓冲字节上限
PEND_TIMEOUT = 0.1      # DBCS 重组超时，超时按 latin-1 落地不丢字
PASTE_IDLE_TIMEOUT = 1.0  # bracketed-paste end 丢失时强制 flush 的空闲超时
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"

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

# Windows 扫描码 → kind
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
    15: "mode_switch",  # Shift+Tab（无需 ENABLE_VIRTUAL_TERMINAL_INPUT）
}

_CTRL = {
    "\x01": "ctrl+a",
    "\x02": "ctrl+b",
    "\x04": "ctrl+d",
    "\x05": "ctrl+e",
    "\x06": "ctrl+f",
    "\x07": "ctrl+g",
    "\x0b": "ctrl+k",
    "\x0c": "ctrl+l",
    "\x0e": "ctrl+n",
    "\x0f": "ctrl+o",
    "\x10": "ctrl+p",
    "\x12": "ctrl+r",
    "\x13": "ctrl+s",
    "\x14": "ctrl+t",
    "\x17": "ctrl+w",
    "\x18": "ctrl+x",
    "\x19": "ctrl+y",
    "\x1a": "ctrl+z",
}

_CSI_FINAL = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "Z": "mode_switch",
}

_CSI_TILDE = {
    "1": "home",
    "2": "hotkey_insert",
    "3": "delete",
    "4": "end",
    "5": "scroll_up",
    "6": "scroll_down",
    "11": "hotkey_f1",
    "12": "hotkey_f2",
    "13": "hotkey_f3",
    "14": "hotkey_f4",
    "15": "hotkey_f5",
    "17": "hotkey_f6",
    "18": "hotkey_f7",
    "19": "hotkey_f8",
    "20": "hotkey_f9",
    "21": "hotkey_f10",
    "23": "hotkey_f11",
    "24": "hotkey_f12",
    "200": "paste_start",
    "201": "paste_end",
}

_MOD = {"2": "shift+", "3": "alt+", "5": "ctrl+", "6": "ctrl+shift+", "7": "ctrl+alt+"}

_DIRECTION_KINDS = frozenset({"up", "down", "left", "right", "home", "end", "delete", "scroll_up", "scroll_down"})


def _kind_event(name: str) -> Event:
    if name.startswith("hotkey_"):
        return ("hotkey", name.split("_", 1)[1])
    return (name, "")


def _scan_event(code: int) -> Event:
    return _kind_event(_WIN_SCAN[code]) if code in _WIN_SCAN else TICK


def _kbhit() -> bool:
    return bool(msvcrt.kbhit()) if _WINDOWS else False


class KeyReader:
    """控制台输入读取器（Windows 主路径 msvcrt，Unix 备用 termios）。

    状态字段分四组：
    - _buf:            待解析字符队列（_fill_win 采集 → _parse_win_buffer 消费）
    - _pend/_pend_at:  DBCS 字节重组缓冲及首字节时间戳（中文上屏逐字节到达时
                       累积，批次末/超时统一按 console CP 解码）
    - _half_at:        CSI 半包首次形成时间戳（超时决断防假死）
    - _pasting/_paste_chunks/_paste_at:
                       bracketed-paste 收流态、内容缓冲、最近收流时间
                       （end 标记丢失时按 PASTE_IDLE_TIMEOUT 强制 flush）
    """

    def __init__(self):
        self._fd = None
        self._old_settings = None
        self._buf: List[str] = []
        self._pend: bytes = b""  # conhost DBCS 逐字节伪宽字符的重组缓冲
        self._pend_at: Optional[float] = None
        self._half_at: Optional[float] = None  # 半包首次形成时间（超时决断用）
        # bracketed-paste 状态机：paste_start/end 之间字节整体合成一个 paste 事件
        self._pasting = False
        self._paste_chunks: List[str] = []
        self._paste_at = 0.0
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

        注意：清队列必须用 getch（与 _fill_win 同 API）。getch 读 ANSI 字节
        记录、getwch 读宽字符记录，混用会导致控制台输入队列错位（历史教训，
        见模块 docstring 教训 1）。"""
        self._buf.clear()
        self._pend = b""
        self._pend_at = None
        self._half_at = None
        self._pasting = False
        self._paste_chunks = []
        self._paste_at = 0.0
        if _WINDOWS:
            try:
                while msvcrt.kbhit():
                    msvcrt.getch()
            except Exception:
                pass

    def _decode_pend(self) -> Optional[str]:
        """对重组缓冲做严格全量解码；未凑齐/全部失败返回 None"""
        if not self._pend:
            return None
        for enc in _decode_encodings():
            try:
                return self._pend.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return None

    def _flush_pend_before(self, ch: str) -> None:
        """非伪字节单元入缓冲前冲刷 pending：可解则产出字符；
        孤立 0xE0 视为扩展键前缀交解析层配对；其余 latin-1 落地不丢字。"""
        if not self._pend:
            return
        decoded = self._decode_pend()
        if decoded is not None:
            self._buf.append(decoded)
            _log(f"dbcs decoded {decoded!r}")
        elif self._pend == b"\xe0":
            self._buf.append("\xe0")
            _log("dbcs pending=E0 -> ext-prefix passthrough")
        else:
            self._buf.append("".join(chr(b) for b in self._pend))
            _log(f"dbcs latin1 fallback {[hex(b) for b in self._pend]}")
        self._pend = b""
        _log(f"flushed before {ch!r}")

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

    # ----- bracketed-paste 状态机（粘贴走字节层，绝不触发按键语义） -----
    @staticmethod
    def _trailing_partial(s: str, marker: str) -> int:
        """s 尾部与 marker 前缀的最长匹配长度（可能是尚未到齐的 end 标记）"""
        for keep in range(min(len(s), len(marker) - 1), 0, -1):
            if marker.startswith(s[-keep:]):
                return keep
        return 0

    def _decode_str_maybe_dbcs(self, s: str) -> str:
        """粘贴内容若含 0x80-0xFF 伪宽字节（conhost DBCS 逐字符投递），按控制台
        代码页尝试解码还原；含真实宽字符（latin-1 无法编码）时原样保留。"""
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

    # ----- Windows：getch 字节采集（TMP 同款验证路径）+ DBCS 重组 + 粘贴收流 -----
    def _fill_win(self, wait: float = 0.0) -> None:
        """把控制台输入队列抽干到 _buf / _pend。

        - getch 字节读取：每字节 chr() 后进 _buf。0x80-0xFF 字节是 conhost
          按 console CP 拆散的中文（GBK 2 字节 / UTF-8 3 字节），进 _pend
          累积，批次末统一解码——中途解码会让 GBK 在 UTF-8 序列中途抢跑
          解出乱码（历史教训 3）。
        - wait>0 时先同步等待至多 wait 秒（半包续读场景）；
          主路径 wait=0 零等待。
        - 粘贴收流态下一切字符只进 _paste_chunks，不产生按键语义。
        """
        if not _WINDOWS:
            return
        try:
            if wait > 0:
                end = time.time() + wait
                while time.time() < end and not msvcrt.kbhit():
                    time.sleep(0.002)
            self._pend_timeout_check()
            while msvcrt.kbhit():
                # getch 字节 API（对齐 music player 的验证路径）：
                # - ConPTY/整字 IME 上屏 = 代码页字节流（GBK/UTF-8），
                #   经下方 0x80+ 重组层按 console CP 解码还原；
                # - IME 注入的 \x00/\x1e 握手字节由解析层零等待放行（丢弃），
                #   不等待即回应——等待会让 IME 挂起到自身超时才放行真实按键
                raw = msvcrt.getch()
                if isinstance(raw, bytes):
                    if not raw:
                        continue
                    ch = chr(raw[0])
                    _log(f"raw unit {ch!r} U+{ord(ch):04X}")
                else:
                    ch = raw
                    _log(f"raw unit {ch!r} U+{ord(ch[0]):04X}")
                if self._pasting:
                    # 粘贴收流：一切字符（含 \r\n\t）只进粘贴缓冲，不产生按键语义
                    self._paste_chunks.append(ch)
                    self._paste_at = time.time()
                    continue
                # conhost DBCS：中文 IME 文本按控制台代码页逐字节投递，
                # 0x80-0xFF 字节累积重组，批次结束统一解码。
                if len(ch) == 1 and 0x80 <= ord(ch) <= 0xFF:
                    if not self._pend:
                        self._pend_at = time.time()
                    self._pend += bytes([ord(ch)])
                    continue
                self._flush_pend_before(ch)
                self._buf.append(ch)
            # 批次结束：统一解码 pending（console CP 优先，utf-8/gbk 兜底）
            if self._pend:
                decoded = self._decode_pend()
                if decoded is not None:
                    _log(f"dbcs decoded {decoded!r} from {self._pend.hex(' ')}")
                    self._pend = b""
                    self._pend_at = None
                    self._buf.append(decoded)
                # 不可解：IME 字节未凑齐，保留到下一帧（_pend_timeout_check 兜底）
        except Exception:
            pass

    def _read_windows(self, timeout: float = 0.02) -> Event:
        """主读取循环：返回一个事件，无键返回 TICK。

        时序契约：无键时 sleep(timeout)（默认 20ms）——这是主循环的心跳节拍，
        ui.read_line 依赖它驱动空闲重画兜底（TMP 式恒定重画）。有键时一次
        批次抽干（_fill_win）后解析出一个事件。
        """
        try:
            now = time.time()
            # 粘贴 idle 超时：end 标记丢失（终端 bug）时强制 flush，防后续按键全被吞
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
                    if self._buf:
                        return self._parse_win_buffer()
                return TICK

            if not _kbhit():
                time.sleep(timeout if timeout > 0 else 0.02)
                if not _kbhit():
                    return TICK
            self._fill_win(0.0)
            if not self._buf:
                return TICK
            # conhost 粘贴启发式（无 bracketed-paste 标记的裸粘贴）：同一批次
            # 多字符且回车/换行出现在批次中部（或含 Tab）、无 ESC → 整批合成
            # paste 事件，防止粘贴文本中的 \r 被当作提交。回车在末尾视为
            # 正常「输入+提交」（IME 中文上屏后立即回车常同批到达，不可误判）。
            # 已知残留：conhost 下单行粘贴且末尾带回车仍会提交（该终端不发
            # 粘贴标记，无法区分）；bracketed-paste 终端不受影响。
            if not self._pasting and len(self._buf) >= 2:
                joined = "".join(self._buf)
                mid_break = any(c in joined[:-1] for c in ("\r", "\n"))
                if "\x1b" not in joined and (mid_break or "\t" in joined):
                    self._buf.clear()
                    _log(f"paste heuristic {len(joined)} chars")
                    return ("paste", self._decode_str_maybe_dbcs(joined))
            return self._parse_win_buffer()
        except Exception:
            self._buf.clear()
            return TICK

    def _parse_win_buffer(self) -> Event:
        if not self._buf:
            return TICK

        # bracketed-paste 收流态：_buf 内容整体进粘贴缓冲，只找 end 标记
        if self._pasting:
            s = "".join(self._buf)
            # Ctrl+C 逃生舱优先于粘贴收流：截断粘贴、保留 \x03 交下帧 interrupt
            i03 = s.find("\x03")
            if i03 >= 0:
                self._paste_chunks.append(s[:i03])
                self._buf.clear()
                self._buf.append("\x03")
                _log("paste interrupted by Ctrl+C")
                if any(self._paste_chunks) or self._pasting:
                    return self._flush_paste_event()
                return TICK
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

        # 扩展键前缀 0x00 / 0xE0 —— music player 主路径
        # 两个来源：① 真扩展键（方向键等）：前缀+扫描码同批到达；
        # ② IME 握手字节：删除上屏字符时注入孤立 \x00（无配对字节）。
        # 处理原则：立即查一次队列，有配对则按扫描码解析；无配对则零等待
        # 丢弃——任何等待都会让 IME 挂起到自身超时（实测 1.3s+）才放行
        # 真实按键，表现为删除严重延迟（八轮排查的终审根因之一）。
        if ch in ("\x00", "\xe0"):
            if len(self._buf) < 2:
                self._fill_win(0.0)  # 立即查一次，不等待
            if len(self._buf) < 2:
                self._buf.pop(0)
                _log(f"lone prefix {ch!r} dropped (ime-handshake/scan-lost)")
                return TICK
            self._half_at = None
            ch2 = self._buf[1]
            code2 = ord(ch2)
            if ch == "\xe0" and code2 != 15 and code2 not in _WIN_SCAN:
                # 0xE0 配对的不是扫描码：getch 下 à(U+00E0) 上屏即 0xE0 单字节，
                # 是普通字符——按字符放行，下一字节留在缓冲重新解析
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
            return ("submit", "")
        if ch == "\n":
            self._buf.pop(0)
            return ("submit_ctrl", "")
        if ch == "\t":
            self._buf.pop(0)
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
        if ch in _CTRL:
            self._buf.pop(0)
            return ("hotkey", _CTRL[ch])

        # 可打印字符（含中文）：合并连续可打印单元，但不吞扩展键/CSI 前缀
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
            self._fill_win(0.0)
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
        """解析 ESC / ESC [ … / ESC O …

        半包策略：若缓冲仍是可能继续到达的 CSI 前缀，则保留缓冲并返回 TICK，
        由下一帧续读；只有确认是「单独 Esc」或无法形成序列时才返回 escape。
        超时出口：半包滞留超 HALF_TIMEOUT 强制按 Esc 决断，防终端丢字节假死。
        """
        if len(self._buf) < 2:
            self._fill_win(0.04)
        if len(self._buf) < 2:
            # 确认单独 ESC
            self._buf.pop(0)
            self._half_at = None
            return ("escape", "")

        nxt = self._buf[1]

        # SS3：ESC O A（应用光标键）
        if nxt == "O":
            if len(self._buf) < 3:
                self._fill_win(0.04)
            if len(self._buf) < 3:
                # 半包保留，勿当 Esc 退出设置页；超时强制决断
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
            # ESC + 非 CSI 第二字节：消费掉，作 Esc
            del self._buf[:2]
            self._half_at = None
            return ("escape", "")

        # CSI：ESC [ params final
        if len(self._buf) < 3:
            self._fill_win(0.04)
        if len(self._buf) < 3:
            # ESC [ 半包 → 保留缓冲；超时强制决断为 Esc
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
            # 参数尚未见到 final 字节：续读；仍无则保留缓冲等下帧
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
                # 参数滞留超时出口：强制按 Esc 决断，防丢 final 字节假死
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
                # 进入粘贴收流态：后续字节由 _parse_win_buffer 的粘贴分支接管
                self._pasting = True
                self._paste_at = time.time()
                _log("paste start")
                return TICK
            if name == "paste_end":
                # 陈旧 paste-end（超时已 flush 过）是回声，丢弃
                _log("paste end (stale, ignored)")
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

        if final in _CSI_FINAL:
            name = _CSI_FINAL[final]
            if name == "mode_switch":
                return ("mode_switch", "")
            parts = [p for p in params.split(";") if p != ""] if params else []
            mod = ""
            if len(parts) >= 2:
                mod = _MOD.get(parts[-1], "")
            # 无修饰 / 应用光标键（params 空或 1）→ 返回基础方向 kind
            if name in ("up", "down", "left", "right", "home", "end"):
                if not mod:
                    return (name, "")
                return ("hotkey", mod + name)
            return _kind_event(name)

        return TICK

    # ----- Unix：对齐 music player —— 仅单独 Esc 才是 escape -----
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
                            # CSI 半包超时：不返回 escape，避免方向键拆包误退
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
                        name = _CSI_FINAL.get(code)
                        if not name:
                            return TICK
                        parts = [p for p in params.split(";") if p != ""] if params else []
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
    """批处理读取：一次抽干输入队列返回全部事件（无键返回空列表）。

    供主循环"批量处理键、统一绘制一次"——粘贴/快速输入时绘制次数
    从 N 降为 1；首个事件等待 timeout（即主循环心跳），其余不等待。
    粘贴收流态（_pasting）不视为队列空闲，继续等下一批数据。
    """
    reader = get_reader()
    events: List[Event] = []
    ev = reader.read_event(timeout)
    while ev != TICK:
        events.append(ev)
        if not reader._buf and not reader._pasting and not _kbhit():
            break
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
        "tick",
        "char",
        "submit",
        "submit_ctrl",
        "tab",
        "mode_switch",
        "backspace",
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "scroll_up",
        "scroll_down",
        "escape",
        "interrupt",
        "clear",
        "hotkey",
    )
