# computer-use 插件 GUI 后端：pyautogui/pygetwindow/pyperclip/ctypes 薄封装。
# 独立成模块便于测试 monkeypatch 与未来替换后端（如 UIA）；函数名带前缀防 sys.path 撞名。
# 坐标系：Windows 虚拟屏（含全部显示器，原点可为负），与 SetCursorPos/GetWindowRect 同空间。
import ctypes
import time

import pyautogui
import pygetwindow as gw
import pyperclip

pyautogui.FAILSAFE = True  # 鼠标推到屏幕角落立即抛 FailSafeException 急停
FailSafeException = pyautogui.FailSafeException

_corner_tol = 4  # 急停判定：光标距虚拟屏任一角 ≤4px


def cu_virtual_rect() -> tuple:
    """虚拟屏矩形 (x0, y0, w, h)：多屏时原点可为负；失败退主屏 (0,0,size)"""
    try:
        u32 = ctypes.windll.user32
        # SM_XVIRTUALSCREEN=76 SM_YVIRTUALSCREEN=77 SM_CXVIRTUALSCREEN=78 SM_CYVIRTUALSCREEN=79
        x0, y0 = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)
        w, h = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
        if w > 0 and h > 0:
            return int(x0), int(y0), int(w), int(h)
    except Exception:
        pass
    s = pyautogui.size()
    return 0, 0, int(s.width), int(s.height)


def cu_screen_size() -> tuple:
    """虚拟屏尺寸 (w, h)；与 cu_screenshot/cu_click 同一坐标系"""
    _x, _y, w, h = cu_virtual_rect()
    return w, h


def cu_screenshot():
    """虚拟屏全图（含副屏、负原点），返回 PIL Image。
    必须 all_screens=True：默认只抓主屏再按 bbox 裁剪，副屏区域静默填黑。"""
    from PIL import ImageGrab

    x0, y0, w, h = cu_virtual_rect()
    return ImageGrab.grab(bbox=(x0, y0, x0 + w, y0 + h), all_screens=True)


def cu_check_abort() -> None:
    """光标抵近虚拟屏任一角 → 急停。
    pyautogui 自带 FAILSAFE 只认主屏 (0,0)，副屏角落不触发；此处按虚拟屏四角补判，
    供主循环在 sleep/轮询间隙周期性调用。"""
    x0, y0, w, h = cu_virtual_rect()
    try:
        pos = pyautogui.position()
    except Exception:
        return
    corners = ((x0, y0), (x0 + w - 1, y0), (x0, y0 + h - 1), (x0 + w - 1, y0 + h - 1))
    if any(abs(int(pos.x) - cx) <= _corner_tol and abs(int(pos.y) - cy) <= _corner_tol for cx, cy in corners):
        raise FailSafeException()


def cu_monitors() -> list:
    """逐显示器信息 [{x,y,w,h,primary,dpi_x,dpi_y}]；枚举失败退单虚拟屏条目"""
    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    out = []
    try:
        u32 = ctypes.windll.user32
        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT), ctypes.c_void_p
        )

        def _cb(hmon, _hdc, lprect, _lparam):
            r = lprect.contents
            entry = {
                "x": int(r.left), "y": int(r.top),
                "w": int(r.right - r.left), "h": int(r.bottom - r.top),
                "primary": bool(r.left == 0 and r.top == 0),
            }
            try:
                dx, dy = ctypes.c_uint(), ctypes.c_uint()
                ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
                entry["dpi_x"], entry["dpi_y"] = int(dx.value), int(dy.value)
            except Exception:
                entry["dpi_x"] = entry["dpi_y"] = None
            out.append(entry)
            return 1

        u32.EnumDisplayMonitors(None, None, MonitorEnumProc(_cb), 0)
    except Exception:
        pass
    if not out:
        x0, y0, w, h = cu_virtual_rect()
        out = [{"x": x0, "y": y0, "w": w, "h": h, "primary": True, "dpi_x": None, "dpi_y": None}]
    return out


def cu_screen_info() -> dict:
    """坐标链诊断：虚拟矩形/逐屏 DPI/进程 DPI 感知/光标位置"""
    info = {"virtual": cu_virtual_rect(), "monitors": cu_monitors()}
    try:
        pos = pyautogui.position()
        info["cursor"] = (int(pos.x), int(pos.y))
    except Exception:
        info["cursor"] = None
    try:
        v = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(v))
        info["dpi_awareness"] = {0: "unaware", 1: "system", 2: "per_monitor"}.get(v.value, str(v.value))
    except Exception:
        info["dpi_awareness"] = "unknown"
    return info


def cu_click(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    pyautogui.click(int(x), int(y), clicks=int(clicks), button=button)


def cu_move_to(x: int, y: int, duration: float = 0.0) -> None:
    pyautogui.moveTo(int(x), int(y), duration=max(0.0, float(duration)))


def cu_drag_to(x: int, y: int, duration: float = 0.3, button: str = "left") -> None:
    pyautogui.dragTo(int(x), int(y), duration=max(0.0, float(duration)), button=button)


_WHEEL_DELTA = 120  # Windows 一格滚轮的标准 delta


def cu_scroll(dy: int, x=None, y=None) -> None:
    """滚动 dy 格（dy>0 向上），可选定位 (x, y)。
    pyautogui 的 Windows 实现把格数直接当原始 wheel delta 传给 mouse_event
    （一格=120），此处乘回 WHEEL_DELTA 保证「格」语义。"""
    pyautogui.scroll(int(dy) * _WHEEL_DELTA, x=x, y=y)


def cu_typewrite_ascii(text: str) -> None:
    """逐键键入纯 ASCII 文本（含 \n 等控制字符映射为按键）"""
    pyautogui.typewrite(text, interval=0.01)


def cu_press(key: str) -> None:
    pyautogui.press(key)


def cu_hotkey(*keys: str) -> None:
    pyautogui.hotkey(*keys)


def cu_key_down(key: str) -> None:
    pyautogui.keyDown(key)


def cu_key_up(key: str) -> None:
    pyautogui.keyUp(key)


def cu_paste() -> None:
    """Ctrl+V 粘贴（配合 cu_set_clipboard 使用）"""
    pyautogui.hotkey("ctrl", "v")


def cu_clipboard_text() -> str:
    return pyperclip.paste() or ""


def cu_set_clipboard(text: str) -> None:
    pyperclip.copy(text)


# 剪贴板快照跳过的句柄型格式：内容是 GDI 句柄/元文件而非内存块，无法按字节克隆
_CLIP_SKIP_FORMATS = {
    2,   # CF_BITMAP
    3,   # CF_METAFILEPICT
    9,   # CF_PALETTE
    14,  # CF_ENHMETAFILE
    8,   # CF_DIB（设备相关位图，DIBV5=17 保留）
    128, # CF_OWNERDISPLAY：返回 HWND 非 HGLOBAL，不能按内存块克隆
    129, 130, 131, 142,  # CF_DSP* 私有显示格式
}

_u32, _k32 = ctypes.windll.user32, ctypes.windll.kernel32
# 64 位 HANDLE/指针必须显式 restype=c_void_p，默认 c_int 截断会在 GlobalLock 溢出
_u32.OpenClipboard.restype = ctypes.c_bool
_u32.OpenClipboard.argtypes = [ctypes.c_void_p]
_u32.GetClipboardData.restype = ctypes.c_void_p
_u32.GetClipboardData.argtypes = [ctypes.c_uint]
_u32.SetClipboardData.restype = ctypes.c_void_p
_u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_k32.GlobalSize.restype = ctypes.c_size_t
_k32.GlobalSize.argtypes = [ctypes.c_void_p]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalLock.argtypes = [ctypes.c_void_p]
_k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_k32.GlobalAlloc.restype = ctypes.c_void_p
_k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_k32.GlobalFree.argtypes = [ctypes.c_void_p]


def cu_clipboard_snapshot():
    """剪贴板全格式快照 → (items:[(fmt,data)], skipped:int)；剪贴板被占用返回 None。
    覆盖 CF_UNICODETEXT/CF_HDROP 等内存型格式；句柄型（位图/元文件）跳过计数。"""
    u32, k32 = _u32, _k32
    if not u32.OpenClipboard(0):
        return None
    items, skipped = [], 0
    try:
        fmt = u32.EnumClipboardFormats(0)
        while fmt:
            if fmt in _CLIP_SKIP_FORMATS:
                skipped += 1
            else:
                h = u32.GetClipboardData(fmt)
                if h:
                    size = k32.GlobalSize(h)
                    p = k32.GlobalLock(h)
                    if p:
                        try:
                            items.append((int(fmt), ctypes.string_at(p, int(size))))
                        finally:
                            k32.GlobalUnlock(h)
            fmt = u32.EnumClipboardFormats(fmt)
    finally:
        u32.CloseClipboard()
    return items, skipped


def cu_clipboard_restore(items) -> bool:
    """恢复 cu_clipboard_snapshot 拿到的快照；逐项 SetClipboardData，所有权移交系统"""
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    if not u32.OpenClipboard(0):
        return False
    ok = True
    try:
        u32.EmptyClipboard()
        for fmt, data in items or []:
            h = k32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
            if not h:
                ok = False
                continue
            p = k32.GlobalLock(h)
            if p:
                ctypes.memmove(p, data, len(data))
                k32.GlobalUnlock(h)
            if not u32.SetClipboardData(int(fmt), h):
                k32.GlobalFree(h)
                ok = False
    finally:
        u32.CloseClipboard()
    return ok


def cu_window_titles() -> list:
    """可见窗口标题列表（去空）"""
    return [t.strip() for t in gw.getAllTitles() if t and t.strip()]


def cu_window_list() -> list:
    """可见窗口明细 [{hwnd,title,x,y,w,h,minimized}]；矩形为虚拟屏坐标"""
    out = []
    for w in gw.getAllWindows():
        t = (w.title or "").strip()
        if not t:
            continue
        entry = {"hwnd": int(getattr(w, "_hWnd", 0) or 0), "title": t}
        try:
            entry.update(
                x=int(w.left), y=int(w.top), w=int(w.width), h=int(w.height),
                minimized=bool(w.isMinimized),
            )
        except Exception:
            entry.update(x=None, y=None, w=None, h=None, minimized=None)
        out.append(entry)
    return out


def cu_find_windows(title_sub: str, exact: bool = False) -> list:
    """按标题匹配窗口（子串忽略大小写或精确），返回 cu_window_list 同构条目"""
    needle = title_sub.strip().casefold()
    if not needle:
        return []
    out = []
    for e in cu_window_list():
        title = e["title"].casefold()
        if (title == needle) if exact else (needle in title):
            out.append(e)
    return out


def _activate_window(w) -> tuple:
    """对 pygetwindow 窗口对象执行前台激活（前台锁解锁 + 最小化兜底复核）"""
    expected = (w.title or "").strip()

    def _active_title() -> str:
        active = gw.getActiveWindow()
        return (active.title or "").strip() if active else ""

    def _activate_once() -> None:
        pyautogui.keyDown("alt")
        time.sleep(0.05)
        pyautogui.keyUp("alt")
        w.activate()
        time.sleep(0.25)

    if w.isMinimized:
        w.restore()
        time.sleep(0.2)
    _activate_once()
    if _active_title() == expected:
        return True, expected, ""
    # 兜底：最小化再还原（SW_RESTORE 强制前台），绕开前台锁残余
    w.minimize()
    time.sleep(0.15)
    w.restore()
    time.sleep(0.3)
    ctypes.windll.user32.SetForegroundWindow(w._hWnd)
    time.sleep(0.2)
    active_title = _active_title()
    if active_title == expected:
        return True, expected, ""
    return False, active_title, f"前台窗口为「{active_title}」，激活未生效"


def cu_focus_hwnd(hwnd: int) -> tuple:
    """按窗口句柄激活（消歧后由上层传入）。返回 (ok, 前台标题, 错误说明)"""
    target = None
    for w in gw.getAllWindows():
        if int(getattr(w, "_hWnd", 0) or 0) == int(hwnd):
            target = w
            break
    if target is None:
        return False, "", f"窗口句柄 {hwnd} 不存在或已关闭"
    try:
        return _activate_window(target)
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}"


def cu_focus_window(title_sub: str) -> tuple:
    """按标题子串（忽略大小写）激活首个匹配窗口。
    返回 (ok, 激活后前台窗口标题, 错误说明)；歧义消解在上层（cu_find_windows+index）。"""
    matches = cu_find_windows(title_sub)
    if not matches:
        return False, "", f"未找到标题含「{title_sub}」的窗口"
    return cu_focus_hwnd(matches[0]["hwnd"])


_cv2_state = {"checked": False, "ok": False}


def cu_have_cv2() -> bool:
    """opencv 可用性（locate 的 confidence 模糊匹配依赖它），惰性探测缓存"""
    if not _cv2_state["checked"]:
        try:
            import cv2  # noqa: F401

            _cv2_state["ok"] = True
        except Exception:
            _cv2_state["ok"] = False
        _cv2_state["checked"] = True
    return _cv2_state["ok"]


def cu_locate_all(needle_path, region=None, confidence=None) -> list:
    """模板图定位：在虚拟屏截图中找 needle，返回 [{x,y,w,h,cx,cy}]（虚拟坐标，cx/cy 中心）。
    region=(x,y,w,h) 虚拟坐标限定搜索域；confidence(0-1) 需 opencv，缺失时按精确匹配。"""
    import pyscreeze
    from PIL import Image

    x0, y0, _vw, _vh = cu_virtual_rect()
    hay = cu_screenshot()
    if region:
        rx, ry, rw, rh = (int(v) for v in region)
        # hay 像素 (0,0) 对应虚拟坐标 (x0,y0)：裁剪偏移须先减原点
        hay = hay.crop((rx - x0, ry - y0, rx - x0 + rw, ry - y0 + rh))
        base_x, base_y = rx, ry  # 命中框映射回虚拟坐标的基准
    else:
        base_x, base_y = x0, y0
    needle = Image.open(str(needle_path))
    kwargs = {"confidence": float(confidence)} if (confidence is not None and cu_have_cv2()) else {}
    out = []
    for box in pyscreeze.locateAll(needle, hay, grayscale=True, **kwargs):
        bx, by = int(box.left) + base_x, int(box.top) + base_y
        bw, bh = int(box.width), int(box.height)
        out.append({"x": bx, "y": by, "w": bw, "h": bh, "cx": bx + bw // 2, "cy": by + bh // 2})
    return out
