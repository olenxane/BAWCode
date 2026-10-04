# -*- coding: utf-8 -*-
"""剪贴板粘贴适配：剪贴板 IO、取文件/取图判定与占位登记（Ctrl+V 热键与终端字符流粘贴共用）

三条规则（判定与登记封装在 PasteBuffer，与 UI 输入框缓冲解耦，可独立实例化测试）：
  1. 文本 ≤ _PASTE_INLINE_LIMIT 直贴；超出返回占位 token（[粘贴:N字符]，同长碰撞
     加 ·2/·3 保唯一），完整内容存注册表，提交时经 expand 展开回全文
  2. 剪贴板纯图片经 save_clipboard_image 落盘 data/temp/clipboard-*.png，占位符=
     落盘路径（图片任何时刻不显示）；粘贴内容是图片路径则直取原图、不复制
  3. 其他文件：可解码且 ≤1MB 读全文走占位机制；二进制（\\x00 判据）/超限/解码
     失败退回仅插路径文本
"""
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.log import get_logger
from core.memory import IMAGE_SUFFIXES
from core.tools import _decode_best_effort

log = get_logger("pasteboard")

_PASTE_INLINE_LIMIT = 320            # 超过则输入框只放占位符，发送时展开全文
_PASTE_FILE_MAX_BYTES = 1024 * 1024  # 取文件通路的读取上限（二进制/超限退回仅插路径）
_CLIPBOARD_BRIDGE = None             # 测试注入点


def set_clipboard_bridge(fn) -> None:
    """剪贴板读取注入点（测试用）：返回 {"kind": "paths"|"text"|"image"|"none", ...}"""
    global _CLIPBOARD_BRIDGE
    _CLIPBOARD_BRIDGE = fn


def read_clipboard() -> dict:
    """读剪贴板 payload：优先测试桥；非 Windows 无剪贴板读取返回 none，Windows 走 ctypes"""
    if _CLIPBOARD_BRIDGE is not None:
        try:
            return _CLIPBOARD_BRIDGE() or {"kind": "none"}
        except Exception:
            log.error("剪贴板桥异常", exc_info=True)
            return {"kind": "none"}
    if sys.platform != "win32":
        return {"kind": "none"}
    return _win_clipboard_payload()


def _win_clipboard_payload() -> dict:
    """Windows 剪贴板读取（ctypes，无新依赖）：HDROP 文件列表 > 文本 > 图片探测。
    图片字节经 PowerShell 落盘（剪贴板关闭后调用，GetImage 自行开剪贴板）。"""
    import ctypes

    user32 = ctypes.windll.user32
    if not user32.OpenClipboard(0):
        return {"kind": "none"}
    has_hd = bool(user32.IsClipboardFormatAvailable(15))   # CF_HDROP
    has_txt = bool(user32.IsClipboardFormatAvailable(13))  # CF_UNICODETEXT
    has_img = bool(user32.IsClipboardFormatAvailable(8) or user32.IsClipboardFormatAvailable(2))
    paths, text = [], None
    try:
        if has_hd:
            shell32 = ctypes.windll.shell32
            h = user32.GetClipboardData(15)
            if h:
                count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
                buf = ctypes.create_unicode_buffer(520)
                for i in range(max(0, count)):
                    if shell32.DragQueryFileW(h, i, buf, 520):
                        paths.append(buf.value)
        elif has_txt:
            k32 = ctypes.windll.kernel32
            h = user32.GetClipboardData(13)
            if h:
                ptr = k32.GlobalLock(h)
                if ptr:
                    try:
                        text = ctypes.wstring_at(ptr)
                    finally:
                        k32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()
    if paths:
        return {"kind": "paths", "paths": paths}
    if text:
        return {"kind": "text", "text": text}
    if has_img:
        return {"kind": "image"}
    return {"kind": "none"}


def paste_temp_dir() -> Path:
    """剪贴板图片临时目录：data/temp（按需创建）"""
    d = Path(__file__).resolve().parent.parent / "data" / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_clipboard_image() -> Optional[Path]:
    """剪贴板图片落盘 PNG（System.Windows.Forms 取图；-STA 必须，剪贴板属 STA 单元）"""
    target = paste_temp_dir() / ("clipboard-%s-%s.png" % (time.strftime("%Y%m%d-%H%M%S"), os.urandom(2).hex()))
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        "$img = [System.Windows.Forms.Clipboard]::GetImage(); "
        "if ($img -ne $null) { $img.Save('%s', [System.Drawing.Imaging.ImageFormat]::Png) }"
        % str(target).replace("'", "''")
    )
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, timeout=20, creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("剪贴板图片落盘失败: %r", exc)
        return None
    return target if target.is_file() else None


def looks_like_path(s: str) -> bool:
    """单行文本是否为现存文件路径（资源管理器「复制文件地址」的引号一并容忍）"""
    s = s.strip().strip('"')
    if not s or len(s) > 500 or s.endswith(("\\", "/")):
        return False
    try:
        return Path(s).exists()
    except OSError:
        return False


def read_paste_file(path: Path) -> Optional[str]:
    """取文件通路的内容读取：≤1MB、二进制拒绝（\\x00 判据同 read 工具）、best-effort 解码"""
    try:
        if path.stat().st_size > _PASTE_FILE_MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:512]:
        return None
    text, _enc, _ok = _decode_best_effort(raw)
    return text


class PasteBuffer:
    """粘贴占位状态对象：注册表（token→全文）+ 已登记图片路径。

    accept_* 返回应插入输入框缓冲的文本（原文/路径/token），expand 在提交时
    展开为全文并清空登记；展开得到的图片路径由调用方入队 pending_images，
    供回合开始时编码为多模态数组。
    """

    def __init__(self) -> None:
        self.registry: Dict[str, str] = {}  # 占位 token → 完整内容（占位符仅存在于输入框）
        self.image_paths: List[str] = []    # 已登记图片路径（占位符=路径本身，不复制）

    def reset(self) -> None:
        """清占位登记（提交消费后/清空输入框时防孤儿 token）"""
        self.registry.clear()
        self.image_paths = []

    def _token(self, content: str) -> str:
        """注册占位 token（[粘贴:N字符]，同长碰撞加 ·2/·3 保唯一）"""
        token = f"[粘贴:{len(content)}字符]"
        seq = 2
        while token in self.registry:
            token = f"[粘贴:{len(content)}字符·{seq}]"
            seq += 1
        self.registry[token] = content
        return token

    def _inline(self, text: str) -> str:
        """≤320 字返回原文直贴；超出只放占位符（完整内容在发送时展开）"""
        if len(text) <= _PASTE_INLINE_LIMIT:
            return text
        return self._token(text)

    def accept_text(self, text: str) -> str:
        """paste 事件载荷（终端字符流/括号粘贴）：单行像路径走取文件通路，否则按
        文本规则。返回应插入缓冲的文本（空串=无内容可插）"""
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return ""
        if "\n" not in text and looks_like_path(text):
            return self.accept_path(text)
        return self._inline(text)

    def accept_path(self, raw: str) -> str:
        """取文件通路：图片路径直取原图（不复制临时目录，占位符=路径）；
        其他文件读文本内容并入占位机制；二进制/超限/解码失败退回仅插路径"""
        s = raw.strip().strip('"')
        if not s:
            return raw
        path = Path(s)
        if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
            sp = str(path)
            if sp not in self.image_paths:
                self.image_paths.append(sp)
            return sp
        content = read_paste_file(path)
        if content is None:
            return s  # 取不到内容：路径文本本身入框，agent 自用 read 取
        return self._inline(content)

    def accept_image(self, path) -> str:
        """已落盘图片登记（剪贴板纯图片通路），返回插入文本（落盘路径）"""
        sp = str(path)
        self.image_paths.append(sp)
        return sp

    def accept_clipboard(self) -> Tuple[List[str], str]:
        """应用侧剪贴板粘贴（Ctrl+V 热键）：文件列表/文本/纯图片三分支。
        返回 (逐项插入文本列表, 状态行提示)；提示为空串表示无需更新状态行"""
        data = read_clipboard()
        kind = (data or {}).get("kind")
        if kind == "paths":
            return [self.accept_path(p) for p in data["paths"]], ""
        if kind == "text":
            return [self.accept_text(str(data.get("text") or ""))], ""
        if kind == "image":
            saved = save_clipboard_image()
            if saved is not None:
                return [self.accept_image(saved)], ""
            return [], "剪贴板图片保存失败（详见日志）"
        return [], "剪贴板无可用内容"

    def expand(self, line: str) -> Tuple[str, List[str]]:
        """提交前展开：占位 token→注册全文；登记图片路径按出现序收集（路径文本保留在
        正文里，图片块随消息另行编码）。返回 (展开文本, 图片路径列表)，内部清登记"""
        text = line
        for token in sorted(self.registry, key=len, reverse=True):
            if token in text:
                text = text.replace(token, self.registry[token])
        images = []
        for p in self.image_paths:
            if p in text and p not in images:
                images.append(p)
        self.reset()
        return text, images
