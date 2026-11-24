

from bawcode_sdk import PluginContext
import threading
import time
from pathlib import Path
from typing import Optional

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from . import computer_use_backend as backend
except ImportError as _e:  # 缺 pyautogui 等依赖时不注册，宿主占位壳自然回位
    backend = None
    _IMPORT_ERROR = str(_e)

try:
    from core import proc as _proc  # 宿主取消令牌（线程本地，工具执行期已挂上）
except Exception:
    _proc = None

_TOOL_NAME = "computer_use"
_KEEP_SHOTS = 50          # 截图目录保留张数
_PASTE_SLEEP = 0.3        # 粘贴后等待插入生效
_WAIT_FOR_POLL = 0.25     # wait_for 轮询间隔

# 输入类动作持 GUI 独占锁（防并行 Agent 互搅鼠标/剪贴板）；观测类动作豁免
_MUTATING = {"click", "scroll", "type", "key", "window_focus", "drag", "hover"}
# 纯观测动作：注册表 safe_actions 放行，全模式免确认
_SAFE_ACTIONS = ["screenshot", "window_list", "locate", "wait", "wait_for", "screen_info"]

_DESC = (
    "GUI automation on Windows (all monitors, virtual-screen coordinates). "
    "Actions: screenshot (capture screen; optional region={x,y,w,h} or window={title} crop), "
    "click, double via clicks, drag (mouse drag), hover (move), scroll, type (text input), "
    "key (key/combo, hold_ms for long-press), locate (find template image on screen, returns "
    "clickable coordinates), wait_for (poll until template image appears or a window title "
    "exists), window_list (windows with geometry), window_focus (activate by title; "
    "ambiguous matches must be resolved via index), wait, screen_info (monitors/DPI/cursor "
    "diagnostics). "
    "Workflow: screenshot first, then act on what you see; after each state-changing action a "
    "fresh screenshot is attached automatically. Coordinate rule: x/y are PIXEL COORDINATES "
    "IN THE LATEST ATTACHED SCREENSHOT; the plugin rescales them to the real screen "
    "automatically — including negative coordinates on secondary monitors (no rescale before "
    "any screenshot). locate returns coordinates in the same latest-screenshot space, usable "
    "directly in click. "
    "type: default pastes text via the clipboard (reliable under any input method/IME; the "
    "full clipboard contents are restored afterwards). method=keys types ASCII keystrokes "
    "directly as a fallback for targets that block Ctrl+V (non-ASCII text is rejected). "
    "key: single key (enter/esc/f5), combo (ctrl+s), optional hold_ms. Focus the target "
    "window with window_focus before typing. Verify results on the newest screenshot — an "
    "accepted action does not guarantee the app acted. Do not touch mouse/keyboard while "
    "actions run; moving the cursor to any screen corner triggers FAILSAFE abort. If the "
    "tool reports the GUI is busy, another computer_use call is running — retry later. "
    "Do not screenshot or interact with windows showing passwords, keys, or other secrets."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "screenshot", "click", "scroll", "type", "key", "drag", "hover",
                "locate", "wait_for", "window_list", "window_focus", "wait", "screen_info",
            ],
            "description": "动作名",
        },
        "params": {
            "type": "object",
            "description": (
                "动作参数：screenshot={region?{x,y,w,h}|window?{title}}; "
                "click={x,y,button?,clicks?}; drag={x,y,x2,y2,duration?,button?}; hover={x,y,duration?}; "
                "scroll={dy,x?,y?}; type={text,method?:auto|paste|keys}; key={combo,hold_ms?}; "
                "locate={image,region?{x,y,w,h},confidence?0-1}; wait_for={image|window,timeout?,region?}; "
                "window_focus={title,index?,exact?}; wait={seconds}; window_list/screen_info 无参数"
            ),
        },
    },
    "required": ["action"],
}


class _NoPillow(RuntimeError):
    pass


class _Cancelled(RuntimeError):
    pass


_ctx = None  # PluginContext（setup 注入）
_GUI_LOCK = threading.Lock()
# 坐标基（截图空间→虚拟屏）：(图宽, 图高, 虚拟x0, 虚拟y0, 区宽, 区高)
# 线程本地 + 全局兜底：截过图的线程锁用自己的基（并行 Agent 互不污染）；
# 未截过图的新线程（主 agent 跨忙周期换线程）继承最近已知基，保住"按最新回注截图点击"语义
_basis_local = threading.local()
# 全局兜底基带时效与全屏标记：仅全屏基可被无基线程继承且限时有效——
# 防 B 代理凭上一回合截图继承 A 代理（尤其窗口/裁剪）的基，坐标被错算进 A 的窗口矩形误点击
_basis_global = {"entry": None}
_BASIS_TTL = 10.0  # 全局兜底基最长继承年龄（秒）；超龄按原生坐标处理并提示先截图


def _get_basis():
    b = getattr(_basis_local, "b", None)
    if b is not None:
        return b
    e = _basis_global["entry"]
    if e is None or not e["full"] or time.monotonic() - e["t"] > _BASIS_TTL:
        return None
    return e["b"]


def _set_basis(b, full: bool) -> None:
    _basis_local.b = b
    _basis_global["entry"] = {"b": b, "t": time.monotonic(), "full": bool(full)}


def _settings() -> dict:
    return _ctx.settings


def _err(msg: str) -> dict:
    return {"content": f"工具执行错误（computer_use）: {msg}"}


def _cancelled_msg() -> dict:
    return {"content": "computer_use 已取消（等待/执行期间被中断，此前已执行的部分可能已生效）"}


def _num(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {name} 需要数字，得到 {value!r}")


def _check_cancel() -> None:
    ev = _proc.cancel_event() if _proc is not None else None
    if ev is not None and ev.is_set():
        raise _Cancelled()


def _interruptible_sleep(seconds: float) -> None:
    """50ms 切片睡眠：宿主取消令牌 / 虚拟屏四角急停即时生效"""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        _check_cancel()
        backend.cu_check_abort()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def _shot_to_screen(x: float, y: float) -> tuple:
    """截图空间坐标 → 虚拟屏坐标（按最近截图坐标基换算并钳制到虚拟屏内）"""
    local = getattr(_basis_local, "b", None)
    inherited = local is None
    b = local if local is not None else _get_basis()
    vx0, vy0, vw, vh = backend.cu_virtual_rect()
    if b and b[0] > 0 and b[1] > 0:
        sx, sy = b[2] + x * b[4] / b[0], b[3] + y * b[5] / b[1]
        tag = "（基为最近截图继承）" if inherited else ""
        label = f"截图坐标({x:g},{y:g})→屏幕{tag}"
    else:
        sx, sy = x, y
        label = f"屏幕原生坐标({x:g},{y:g})（无有效坐标基，请先 screenshot 再给坐标）"
    sx = min(max(int(round(sx)), vx0), vx0 + vw - 1)
    sy = min(max(int(round(sy)), vy0), vy0 + vh - 1)
    return sx, sy, f"{label}({sx},{sy})"


def _screen_to_shot(vx: float, vy: float) -> tuple:
    """虚拟屏坐标 → 截图空间（locate 命中回给模型可直接喂 click）"""
    b = _get_basis()
    if not b or b[0] <= 0 or b[1] <= 0:
        return int(round(vx)), int(round(vy))
    return int(round((vx - b[2]) * b[0] / b[4])), int(round((vy - b[3]) * b[1] / b[5]))


def _region_param(p: dict):
    """解析 region 参数（dict 或四元列表），返回截图空间 (x,y,w,h) 或 None"""
    r = p.get("region")
    if r is None:
        return None
    if isinstance(r, dict):
        return _num(r.get("x"), "region.x"), _num(r.get("y"), "region.y"), \
            _num(r.get("w"), "region.w"), _num(r.get("h"), "region.h")
    if isinstance(r, (list, tuple)) and len(r) == 4:
        return tuple(_num(v, "region") for v in r)
    raise ValueError(f"region 须为 {{x,y,w,h}} 或 [x,y,w,h]，得到 {r!r}")


def _rect_to_screen(x: float, y: float, w: float, h: float) -> tuple:
    """截图空间矩形 → 虚拟屏矩形（钳制到虚拟屏内，w/h ≥1）"""
    x1, y1, _ = _shot_to_screen(x, y)
    x2, y2, _ = _shot_to_screen(x + w, y + h)
    vx0, vy0, vw, vh = backend.cu_virtual_rect()
    rx, ry = min(x1, x2), min(y1, y2)
    rw, rh = abs(x2 - x1), abs(y2 - y1)
    rx = min(max(rx, vx0), vx0 + vw - 1)
    ry = min(max(ry, vy0), vy0 + vh - 1)
    rw = max(1, min(rw, vx0 + vw - rx))
    rh = max(1, min(rh, vy0 + vh - ry))
    return rx, ry, rw, rh


def _find_window(p: dict, name: str = "title") -> dict:
    """按 title 子串/exact 找窗口，返回 {hwnd,title,x,y,w,h,minimized}。
    无匹配/多匹配（未给 index）一律报错并列出候选，绝不静默取第一个。"""
    title = str(p.get(name) or "").strip()
    if not title:
        raise ValueError(f"{name} 不能为空（窗口标题子串）")
    exact = bool(p.get("exact"))
    matches = backend.cu_find_windows(title, exact=exact)
    if not matches:
        raise ValueError(f"未找到{'标题为' if exact else '标题含'}「{title}」的窗口")
    idx = p.get("index")
    if len(matches) == 1 and idx is None:
        return matches[0]
    if idx is None:
        cand = "; ".join(
            f"{i}: {m['title'][:40]} ({m.get('x')},{m.get('y')},{m.get('w')}x{m.get('h')})"
            for i, m in enumerate(matches[:10])
        )
        raise ValueError(f"「{title}」匹配到 {len(matches)} 个窗口，请给 index 消歧：{cand}")
    i = int(_num(idx, "index"))
    if not (0 <= i < len(matches)):
        raise ValueError(f"index={i} 越界（共 {len(matches)} 个候选）")
    return matches[i]


def _cleanup_shots(d) -> None:
    shots = sorted(d.glob("shot_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in shots[_KEEP_SHOTS:]:
        try:
            old.unlink()
        except OSError:
            pass


def _take_shot(vrect=None) -> tuple:
    """虚拟屏截图（可裁剪到虚拟矩形）→ 缩放 → 落盘 → 原子换坐标基。返回 (路径, 说明)"""
    if Image is None:
        raise _NoPillow("截图需要 Pillow，请执行 pip install Pillow 后重载插件")
    img = backend.cu_screenshot()
    vx0, vy0, vw, vh = backend.cu_virtual_rect()
    if vrect is not None:
        rx, ry, rw, rh = vrect
        # 裁剪域钳到虚拟屏内（窗口可能部分在屏外）
        rx = min(max(rx, vx0), vx0 + vw - 1)
        ry = min(max(ry, vy0), vy0 + vh - 1)
        rw = max(1, min(rw, vx0 + vw - rx))
        rh = max(1, min(rh, vy0 + vh - ry))
        img = img.crop((rx - vx0, ry - vy0, rx - vx0 + rw, ry - vy0 + rh))
        basis_vrect = (rx, ry, rw, rh)
        full = (rx, ry, rw, rh) == (vx0, vy0, vw, vh)
    else:
        basis_vrect = (vx0, vy0, vw, vh)
        full = True
    try:
        max_edge = int(_settings().get("screenshot_max_edge") or 1280)
    except (TypeError, ValueError):
        max_edge = 1280
    max_edge = max(320, min(max_edge, 4096))
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > max_edge:
        r = max_edge / long_edge
        img = img.resize((max(1, round(w * r)), max(1, round(h * r))), Image.LANCZOS)
    d = _ctx.storage_dir() / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"shot_{time.strftime('%H%M%S')}_{int(time.time() * 1000) % 1000000}_{threading.get_ident() & 0xFFFF:04x}.png"
    img.save(path)
    _cleanup_shots(d)
    _set_basis((img.size[0], img.size[1]) + basis_vrect, full)
    note = (
        f"[已回注截图 {img.size[0]}x{img.size[1]}"
        f"（屏幕区域 {basis_vrect[2]}x{basis_vrect[3]} @({basis_vrect[0]},{basis_vrect[1]})）；"
        "后续 x/y 坐标按本截图像素给出，插件自动换算]"
    )
    return str(path), note


def _act_screenshot(p: dict) -> tuple:
    win = str(p.get("window") or "").strip()
    if win:
        m = _find_window({"title": win, "exact": p.get("exact"), "index": p.get("index")})
        if m.get("minimized"):
            raise ValueError(f"窗口「{m['title']}」已最小化，无法截取内容")
        if m.get("w") is None or m.get("w") <= 0:
            raise ValueError(f"窗口「{m['title']}」矩形不可用")
        path, note = _take_shot(vrect=(m["x"], m["y"], m["w"], m["h"]))
        return f"已截取窗口「{m['title']}」区域。{note}", [path]
    region = _region_param(p)
    if region is not None:
        rx, ry, rw, rh = _rect_to_screen(*region)
        if rw <= 1 or rh <= 1:
            raise ValueError(f"region {region} 钳制到虚拟屏后退化（实际 {rw}x{rh}），请检查坐标")
        path, note = _take_shot(vrect=(rx, ry, rw, rh))
        return f"已截取区域 ({rx},{ry},{rw}x{rh})。{note}", [path]
    path, note = _take_shot()
    return f"已截取屏幕。{note}", [path]


def _locate_confidence(p: dict):
    """locate 置信度解析：参数 > 插件设置；>1 视为百分数；缺失/0 → 精确匹配"""
    conf = p.get("confidence")
    if conf is None:
        conf = _settings().get("locate_confidence")
    conf = None if conf in (None, "", 0) else float(conf)
    if conf is not None and conf > 1:
        conf = conf / 100.0
    return None if conf is None else min(max(conf, 0.0), 1.0)


def _act_locate(p: dict) -> tuple:
    """模板图定位：命中中心点转回截图空间坐标，可直接喂 click"""
    image = str(p.get("image") or "").strip()
    if not image:
        raise ValueError("image 不能为空（模板图路径）")
    if not Path(image).is_file():
        raise ValueError(f"模板图不存在: {image}")
    conf = _locate_confidence(p)
    region = _region_param(p)
    vrect = _rect_to_screen(*region) if region is not None else None
    matches = backend.cu_locate_all(image, region=vrect, confidence=conf)
    no_cv = conf is not None and not backend.cu_have_cv2()
    if not matches:
        note = "（confidence 要求 opencv，未安装已按精确匹配）" if no_cv else ""
        return f"未找到模板图「{image}」{note}。", None
    lines = []
    for i, m in enumerate(matches[:20]):
        cx, cy = _screen_to_shot(m["cx"], m["cy"])
        lines.append(f"{i}: 中心({cx},{cy}) 框({m['w']}x{m['h']})")
    more = f"\n（共 {len(matches)} 处，仅列前 20）" if len(matches) > 20 else ""
    note = "（confidence 要求 opencv，未安装已按精确匹配）" if no_cv else ""
    return (
        f"找到 {len(matches)} 处「{image}」{note}，坐标为截图空间可直接用于 click：\n"
        + "\n".join(lines) + more
    ), None


def _act_wait_for(p: dict) -> tuple:
    """轮询等待：模板图出现 / 窗口标题出现，超时返回最后一次观测"""
    image = str(p.get("image") or "").strip()
    win = str(p.get("window") or "").strip()
    if not image and not win:
        raise ValueError("wait_for 需 image（模板图路径）或 window（标题子串）之一")
    if image and not Path(image).is_file():
        raise ValueError(f"模板图不存在: {image}")
    try:
        cap = float(_settings().get("wait_for_max_seconds") or 60)
    except (TypeError, ValueError):
        cap = 60.0
    timeout = min(max(_num(p.get("timeout"), "timeout") if p.get("timeout") is not None else 10.0, 0.2), max(cap, 0.2))
    deadline = time.monotonic() + timeout
    region = _region_param(p)
    vrect = _rect_to_screen(*region) if region is not None else None
    err_count, last_err = 0, ""
    while True:
        _check_cancel()
        backend.cu_check_abort()
        if image:
            try:
                matches = backend.cu_locate_all(image, region=vrect, confidence=_locate_confidence(p))
                err_count = 0
            except Exception as exc:
                matches = []
                err_count += 1
                last_err = f"{type(exc).__name__}: {exc}"
            if matches:
                cx, cy = _screen_to_shot(matches[0]["cx"], matches[0]["cy"])
                return f"模板图「{image}」已出现（共 {len(matches)} 处），首处中心({cx},{cy})。", None
        else:
            found = backend.cu_find_windows(win)
            if found:
                lines = "; ".join(f"{i}: {m['title'][:40]}" for i, m in enumerate(found[:10]))
                return f"窗口「{win}」已出现（{len(found)} 个匹配）：{lines}", None
        if time.monotonic() >= deadline:
            break
        _interruptible_sleep(_WAIT_FOR_POLL)
    errnote = f"（定位调用 {err_count} 次报错，最后：{last_err}）" if err_count else ""
    if image:
        try:
            path, note = _take_shot()
            return f"等待超时（{timeout:g}s）：模板图「{image}」未出现{errnote}，现场如下。{note}", [path]
        except Exception:
            pass
    return f"等待超时（{timeout:g}s）：{'模板图「' + image + '」' if image else '窗口「' + win + '」'}未出现{errnote}。", None


def _act_click(p: dict) -> tuple:
    x, y = _num(p.get("x"), "x"), _num(p.get("y"), "y")
    button = str(p.get("button") or "left").strip().lower()
    if button not in ("left", "right", "middle"):
        raise ValueError(f"button 须为 left/right/middle，得到 {button!r}")
    if p.get("clicks") is not None:
        clicks = min(max(int(_num(p.get("clicks"), "clicks")), 1), 3)
    else:
        clicks = 2 if bool(p.get("double")) else 1
    sx, sy, conv = _shot_to_screen(x, y)
    backend.cu_click(sx, sy, button=button, clicks=clicks)
    label = f"{clicks}击" if clicks > 1 else "单击"
    return f"已{button}{label}。{conv}", None


def _act_drag(p: dict) -> tuple:
    x, y = _num(p.get("x"), "x"), _num(p.get("y"), "y")
    x2, y2 = _num(p.get("x2"), "x2"), _num(p.get("y2"), "y2")
    button = str(p.get("button") or "left").strip().lower()
    if button not in ("left", "right", "middle"):
        raise ValueError(f"button 须为 left/right/middle，得到 {button!r}")
    duration = min(max(_num(p.get("duration"), "duration") if p.get("duration") is not None else 0.3, 0.0), 5.0)
    sx, sy, conv1 = _shot_to_screen(x, y)
    ex, ey, conv2 = _shot_to_screen(x2, y2)
    backend.cu_move_to(sx, sy)
    backend.cu_drag_to(ex, ey, duration=duration, button=button)
    return f"已拖拽 ({sx},{sy})→({ex},{ey})（{duration:g}s）。{conv1}；{conv2}", None


def _act_hover(p: dict) -> tuple:
    x, y = _num(p.get("x"), "x"), _num(p.get("y"), "y")
    duration = min(max(_num(p.get("duration"), "duration") if p.get("duration") is not None else 0.0, 0.0), 5.0)
    sx, sy, conv = _shot_to_screen(x, y)
    backend.cu_move_to(sx, sy, duration=duration)
    return f"已移动光标。{conv}", None


def _act_scroll(p: dict) -> tuple:
    dy = int(_num(p.get("dy"), "dy"))
    if dy == 0:
        raise ValueError("dy 不能为 0（>0 向上，<0 向下）")
    x, y = p.get("x"), p.get("y")
    conv = ""
    if x is not None and y is not None:
        sx, sy, conv = _shot_to_screen(_num(x, "x"), _num(y, "y"))
        backend.cu_scroll(dy, x=sx, y=sy)
    elif x is not None or y is not None:
        raise ValueError("scroll 定位需 x,y 同时提供")
    else:
        backend.cu_scroll(dy)
    return f"已{'向上' if dy > 0 else '向下'}滚动 {abs(dy)} 格。{conv}".strip(), None


def _act_type(p: dict) -> tuple:
    """默认走剪贴板粘贴：合成键盘事件会被中文 IME 拦截组字（实测 'ab' 键入
    落成 '安柏'），粘贴是中文 Windows 环境下唯一可靠路径；完成后恢复剪贴板。
    method=keys 为禁用粘贴的目标程序兜底（仅 ASCII）。"""
    text = str(p.get("text") or "")
    if not text:
        raise ValueError("text 不能为空")
    method = str(p.get("method") or "auto").strip().lower()
    if method in ("keys", "typewrite"):
        if not text.isascii():
            raise ValueError("method=keys 仅支持 ASCII 文本；含中文/非 ASCII 请用默认粘贴")
        # 分块键入：块间检查取消/急停，长文本不至于霸占锁与键盘到打完为止
        for i in range(0, len(text), 200):
            _check_cancel()
            backend.cu_check_abort()
            backend.cu_typewrite_ascii(text[i:i + 200])
            if i + 200 < len(text):
                _interruptible_sleep(0.05)
        return f"已逐键键入 {len(text)} 字符。", None
    if method not in ("auto", "paste"):
        raise ValueError(f"method 须为 auto/paste/keys，得到 {method!r}")
    restore = bool(_settings().get("clipboard_restore", True))
    snapshot = None
    old_text = None
    if restore:
        snapshot = backend.cu_clipboard_snapshot()
        if snapshot is None:
            old_text = backend.cu_clipboard_text()  # 快照失败（剪贴板被占用）退文本兜底
    restore_ok = True
    try:
        backend.cu_set_clipboard(text)
        backend.cu_paste()
        _interruptible_sleep(_PASTE_SLEEP)
    finally:
        if restore:
            if snapshot is not None:
                restore_ok = bool(backend.cu_clipboard_restore(snapshot[0]))
            else:
                backend.cu_set_clipboard(old_text or "")
    skipped = f"，{snapshot[1]} 个不可克隆格式未恢复" if (snapshot and snapshot[1]) else ""
    if restore and not restore_ok:
        skipped = f"{skipped}；注意：恢复失败（剪贴板被占用），原内容可能已丢失"
    return f"已通过剪贴板粘贴 {len(text)} 字符（规避输入法干扰，原剪贴板内容已恢复{skipped}）。", None


def _act_key(p: dict) -> tuple:
    combo = str(p.get("combo") or "").strip()
    if not combo:
        raise ValueError("combo 不能为空（如 ctrl+s 或 enter）")
    keys = [k.strip().lower() for k in combo.replace("＋", "+").split("+") if k.strip()]
    if not keys:
        raise ValueError(f"combo 无法解析: {combo!r}")
    hold_ms = p.get("hold_ms")
    if len(keys) > 1 and hold_ms is not None:
        raise ValueError("hold_ms 仅支持单键长按，组合键请勿携带")
    if len(keys) == 1:
        if hold_ms is not None:
            ms = min(max(_num(hold_ms, "hold_ms"), 50), 10000) / 1000.0
            backend.cu_key_down(keys[0])
            try:
                _interruptible_sleep(ms)
            finally:
                backend.cu_key_up(keys[0])
            return f"已长按 {'+'.join(keys)} {ms:g}s。", None
        backend.cu_press(keys[0])
    else:
        backend.cu_hotkey(*keys)
    return f"已按键 {'+'.join(keys)}。", None


def _act_window_list(p: dict) -> tuple:
    wins = backend.cu_window_list()
    if not wins:
        return "当前无可见窗口。", None
    lines = []
    for i, w in enumerate(wins[:30]):
        rect = "?"
        if w.get("w") is not None:
            rect = f"({w['x']},{w['y']}) {w['w']}x{w['h']}"
        flag = " 最小化" if w.get("minimized") else ""
        lines.append(f"{i}. {w['title'][:60]} | {rect}{flag}")
    more = f"\n（共 {len(wins)} 个，仅列前 30）" if len(wins) > 30 else ""
    return "可见窗口（坐标为虚拟屏）:\n" + "\n".join(lines) + more, None


def _act_window_focus(p: dict) -> tuple:
    m = _find_window(p)
    ok, active, err = backend.cu_focus_hwnd(m["hwnd"])
    if not ok:
        raise ValueError(f"窗口聚焦失败: {err}")
    return f"已激活窗口「{active}」。", None


def _act_wait(p: dict) -> tuple:
    secs = _num(p.get("seconds"), "seconds") if p.get("seconds") is not None else 1.0
    secs = min(max(secs, 0.1), 10.0)
    _interruptible_sleep(secs)
    return f"已等待 {secs:g}s。", None


def _act_screen_info(p: dict) -> tuple:
    info = backend.cu_screen_info()
    vx0, vy0, vw, vh = info["virtual"]
    lines = [
        f"虚拟屏: ({vx0},{vy0}) {vw}x{vh}",
        f"进程 DPI 感知: {info['dpi_awareness']}",
        "显示器:",
    ]
    for i, m in enumerate(info["monitors"], 1):
        tag = " [主屏]" if m.get("primary") else ""
        dpi = f" dpi={m['dpi_x']}x{m['dpi_y']}" if m.get("dpi_x") else ""
        lines.append(f" {i}. ({m['x']},{m['y']}) {m['w']}x{m['h']}{tag}{dpi}")
    cur = info.get("cursor")
    if cur:
        lines.append(f"光标: ({cur[0]},{cur[1]})")
    lines.append(f"opencv: {'可用（locate 支持 confidence）' if backend.cu_have_cv2() else '未安装（locate 仅精确匹配）'}")
    return "\n".join(lines), None


_DISPATCH = {
    "screenshot": _act_screenshot,
    "locate": _act_locate,
    "wait_for": _act_wait_for,
    "click": _act_click,
    "drag": _act_drag,
    "hover": _act_hover,
    "scroll": _act_scroll,
    "type": _act_type,
    "key": _act_key,
    "window_list": _act_window_list,
    "window_focus": _act_window_focus,
    "wait": _act_wait,
    "screen_info": _act_screen_info,
}


def _computer_use(action: str = "", params: Optional[dict] = None, **_) -> dict:
    """computer_use 工具执行体：GUI 独占锁 + 动作分发 + 自动截图回注"""
    action = str(action or "").strip().lower()
    p = params if isinstance(params, dict) else {}
    if action not in _DISPATCH:
        return _err(f"未知动作「{action}」。支持: {'/'.join(_DISPATCH)}")
    locked = False
    if action in _MUTATING:
        locked = _GUI_LOCK.acquire(blocking=False)
        if not locked:
            return _err("GUI 正被另一 computer_use 调用占用，请稍后重试")
    try:
        try:
            _check_cancel()
            backend.cu_check_abort()
            text, images = _DISPATCH[action](p)
        except _Cancelled:
            return _cancelled_msg()
        except backend.FailSafeException:
            return _err("触发 FAILSAFE 急停（鼠标被移至屏幕角落），动作中止")
        except _NoPillow as exc:
            return _err(str(exc))
        except (ValueError, TypeError) as exc:
            return _err(f"参数错误: {exc}")
        except Exception as exc:
            return _err(f"{type(exc).__name__}: {exc}")
        if images is None and action in _MUTATING:
            try:
                settle = int(_settings().get("settle_ms") or 350)
            except (TypeError, ValueError):
                settle = 350
            if Image is None:
                text = f"{text} [未自动截图：缺少 Pillow]"
            elif bool(_settings().get("auto_screenshot", True)):
                try:
                    _interruptible_sleep(max(settle, 50) / 1000)
                    path, note = _take_shot()
                    text = f"{text} {note}"
                    images = [path]
                except _Cancelled:
                    return _cancelled_msg()
                except backend.FailSafeException:
                    return _err(f"{text} [自动截图前触发 FAILSAFE 急停]")
                except Exception as exc:
                    text = f"{text} [自动截图失败: {type(exc).__name__}: {exc}]"
        if images:
            return {"content": text, "images": images}
        return {"content": text}
    finally:
        if locked:
            _GUI_LOCK.release()


def setup(ctx: PluginContext) -> None:
    global _ctx
    if backend is None:
        ctx.log.warn("computer-use 依赖缺失（%s），未注册工具", _IMPORT_ERROR)
        return
    _ctx = ctx
    ctx.register_tool(
        name=_TOOL_NAME, description=_DESC, usage="computer_use(action, params)", schema=_SCHEMA,
        safe_actions=_SAFE_ACTIONS,
    )(_computer_use)
    ctx.log.info("computer_use 工具已注册；动作: %s；免确认: %s", "/".join(_DISPATCH), "/".join(_SAFE_ACTIONS))
