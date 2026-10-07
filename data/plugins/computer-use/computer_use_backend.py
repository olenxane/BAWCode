# computer-use 插件 GUI 后端：pyautogui/pygetwindow/pyperclip 薄封装。
# 独立成模块便于测试 monkeypatch 与未来替换后端（如 UIA）；函数名带前缀防 sys.path 撞名。
import time

import pyautogui
import pygetwindow as gw
import pyperclip

pyautogui.FAILSAFE = True  # 鼠标推到屏幕角落立即抛 FailSafeException 急停
FailSafeException = pyautogui.FailSafeException


def cu_screen_size() -> tuple:
    """屏幕原生尺寸 (w, h)；与 screenshot() 同一坐标系（DPI 虚拟化对两者一致生效）"""
    s = pyautogui.size()
    return int(s.width), int(s.height)


def cu_screenshot():
    """全屏截图，返回 PIL Image（屏幕原生分辨率）"""
    return pyautogui.screenshot()


def cu_click(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    pyautogui.click(int(x), int(y), clicks=int(clicks), button=button)


_WHEEL_DELTA = 120  # Windows 一格滚轮的标准 delta


def cu_scroll(dy: int, x=None, y=None) -> None:
    """滚动 dy 格（dy>0 向上），可选定位 (x, y)。
    pyautogui 的 Windows 实现把格数直接当原始 wheel delta 传给 mouse_event
    （一格=120），此处乘回 WHEEL_DELTA 保证「格」语义。"""
    pyautogui.scroll(int(dy) * _WHEEL_DELTA, x=x, y=y)


def cu_typewrite_ascii(text: str) -> None:
    """逐键键入纯 ASCII 文本（含 \\n 等控制字符映射为按键）"""
    pyautogui.typewrite(text, interval=0.01)


def cu_press(key: str) -> None:
    pyautogui.press(key)


def cu_hotkey(*keys: str) -> None:
    pyautogui.hotkey(*keys)


def cu_paste() -> None:
    """Ctrl+V 粘贴（配合 cu_set_clipboard 使用）"""
    pyautogui.hotkey("ctrl", "v")


def cu_clipboard_text() -> str:
    return pyperclip.paste() or ""


def cu_set_clipboard(text: str) -> None:
    pyperclip.copy(text)


def cu_window_titles() -> list:
    """可见窗口标题列表（去空）"""
    return [t.strip() for t in gw.getAllTitles() if t and t.strip()]


def cu_focus_window(title_sub: str) -> tuple:
    """按标题子串（忽略大小写）激活窗口。

    返回 (ok, 激活后前台窗口标题, 错误说明)。Windows 的 SetForegroundWindow
    有前台锁（仅前台/最近输入进程可调），先敲一下 ALT 解锁再 activate；仍失败
    时用「最小化→还原」兜底——SW_RESTORE 在 Windows 上必定置前台。复核前台
    窗口标题确认生效，不静默假成功。
    """
    import ctypes

    target = None
    needle = title_sub.strip().casefold()
    for w in gw.getAllWindows():
        t = (w.title or "").strip()
        if t and needle in t.casefold():
            target = w
            break
    if target is None:
        return False, "", f"未找到标题含「{title_sub}」的窗口"
    expected = (target.title or "").strip()

    def _active_title() -> str:
        active = gw.getActiveWindow()
        return (active.title or "").strip() if active else ""

    def _activate_once() -> None:
        pyautogui.keyDown("alt")
        time.sleep(0.05)
        pyautogui.keyUp("alt")
        target.activate()
        time.sleep(0.25)

    try:
        if target.isMinimized:
            target.restore()
            time.sleep(0.2)
        _activate_once()
        if _active_title() == expected:
            return True, expected, ""
        # 兜底：最小化再还原（SW_RESTORE 强制前台），绕开前台锁残余
        target.minimize()
        time.sleep(0.15)
        target.restore()
        time.sleep(0.3)
        ctypes.windll.user32.SetForegroundWindow(target._hWnd)
        time.sleep(0.2)
        active_title = _active_title()
        if active_title == expected:
            return True, expected, ""
        return False, active_title, f"前台窗口为「{active_title}」，激活未生效"
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}"
