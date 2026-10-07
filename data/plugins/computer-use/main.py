import time
from typing import Optional

from PIL import Image

try:
    import computer_use_backend as backend
except ImportError as _e:  # 缺 pyautogui 等依赖时不注册，宿主占位壳自然回位
    backend = None
    _IMPORT_ERROR = str(_e)

_TOOL_NAME = "computer_use"
_KEEP_SHOTS = 50          # 截图目录保留张数
_SETTLE_FLOOR = 0.05
_PASTE_SLEEP = 0.3        # 粘贴后等待插入生效

_AUTO_SHOT_ACTIONS = {"click", "scroll", "type", "key", "window_focus"}

_DESC = (
    "GUI automation on the primary monitor. Actions: screenshot (capture screen), "
    "click (mouse click), scroll, type (text input), key (key/combo press), "
    "window_list (list window titles), window_focus (activate window by title substring), "
    "wait. Workflow: screenshot first to observe, then act on what you see; after each "
    "state-changing action a fresh screenshot is attached automatically. Coordinate rule: "
    "x/y are PIXEL COORDINATES IN THE LATEST ATTACHED SCREENSHOT; the plugin rescales them "
    "to the real screen automatically (no rescale before any screenshot). Screenshots are "
    "proportionally scaled from the native screen size, so coordinates adapt to any display. "
    "type: text is pasted via the clipboard (reliable under any input method/IME; the original "
    "clipboard content is restored afterwards). "
    "key: single key (enter/esc/f5) or combo (ctrl+s). Focus the target window with "
    "window_focus before typing. Verify results on the newest screenshot — an accepted action "
    "does not guarantee the app acted. Do not touch mouse/keyboard while actions run. "
    "Moving the mouse to a screen corner triggers FAILSAFE abort."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["screenshot", "click", "scroll", "type", "key", "window_list", "window_focus", "wait"],
            "description": "动作名",
        },
        "params": {
            "type": "object",
            "description": (
                "动作参数：click={x,y 必需（最近截图的像素坐标）, button?(left/right/middle), "
                "double?(bool)}; scroll={dy 必需（>0 向上，一格为标准滚轮格）, x?, y?}; "
                "type={text 必需（经剪贴板粘贴，规避输入法干扰）}; "
                "key={combo 必需, 如 ctrl+s 或 enter}; window_focus={title 必需, 标题子串}; "
                "wait={seconds, 0.1-10}; screenshot/window_list 无参数"
            ),
        },
    },
    "required": ["action"],
}

_ctx = None  # PluginContext（setup 注入）
_last_shot = {"sent": None, "screen": None}  # 最近回注截图的坐标基（换算+钳制依据）


def _settings() -> dict:
    return _ctx.settings


def _err(msg: str) -> dict:
    return {"content": f"工具执行错误（computer_use）: {msg}"}


def _num(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {name} 需要数字，得到 {value!r}")


def _screen_size() -> tuple:
    try:
        return backend.cu_screen_size()
    except Exception:
        return (0, 0)


def _to_screen(x: float, y: float) -> tuple:
    """截图空间坐标 → 屏幕坐标（按最近截图比例换算并钳制到屏幕内）。
    返回 (sx, sy, 说明文本)"""
    sent = _last_shot["sent"]
    screen = _screen_size()
    if sent and sent[0] > 0 and sent[1] > 0:
        sx, sy = x * screen[0] / sent[0], y * screen[1] / sent[1]
        based = f"截图坐标({x:g},{y:g})→屏幕"
    else:
        sx, sy = x, y
        based = f"屏幕原生坐标({x:g},{y:g})"
    if screen[0] > 0:
        sx = min(max(sx, 0), screen[0] - 1)
        sy = min(max(sy, 0), screen[1] - 1)
    sx, sy = int(round(sx)), int(round(sy))
    return sx, sy, f"{based}({sx},{sy})"


def _cleanup_shots(d) -> None:
    shots = sorted(d.glob("shot_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in shots[_KEEP_SHOTS:]:
        try:
            old.unlink()
        except OSError:
            pass


def _take_shot() -> tuple:
    """截图 → 等比缩放到长边上限内 → 落盘 → 记录坐标基。返回 (路径, 说明文本)"""
    img = backend.cu_screenshot()
    screen = _screen_size()
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
    path = d / f"shot_{time.strftime('%H%M%S')}_{int(time.time() * 1000) % 1000000}.png"
    img.save(path)
    _cleanup_shots(d)
    _last_shot["sent"] = tuple(img.size)
    _last_shot["screen"] = tuple(screen)
    note = (
        f"[已回注截图 {img.size[0]}x{img.size[1]}（屏幕原生 {screen[0]}x{screen[1]}）；"
        "后续 x/y 坐标按本截图像素给出，插件自动换算]"
    )
    return str(path), note


def _act_screenshot(p: dict) -> tuple:
    path, note = _take_shot()
    return f"已截取屏幕。{note}", [path]


def _act_click(p: dict) -> tuple:
    x, y = _num(p.get("x"), "x"), _num(p.get("y"), "y")
    button = str(p.get("button") or "left").strip().lower()
    if button not in ("left", "right", "middle"):
        raise ValueError(f"button 须为 left/right/middle，得到 {button!r}")
    double = bool(p.get("double"))
    sx, sy, conv = _to_screen(x, y)
    backend.cu_click(sx, sy, button=button, clicks=2 if double else 1)
    label = f"{'双击' if double else ''}{button}点击"
    return f"已{label}。{conv}", None


def _act_scroll(p: dict) -> tuple:
    dy = int(_num(p.get("dy"), "dy"))
    if dy == 0:
        raise ValueError("dy 不能为 0（>0 向上，<0 向下）")
    x, y = p.get("x"), p.get("y")
    conv = ""
    if x is not None and y is not None:
        sx, sy, conv = _to_screen(_num(x, "x"), _num(y, "y"))
        backend.cu_scroll(dy, x=sx, y=sy)
    else:
        backend.cu_scroll(dy)
    return f"已{'向上' if dy > 0 else '向下'}滚动 {abs(dy)} 格。{conv}".strip(), None


def _act_type(p: dict) -> tuple:
    """统一走剪贴板粘贴：合成键盘事件会被中文 IME 拦截组字（实测 'ab' 键入
    落成 '安柏'），粘贴是中文 Windows 环境下唯一可靠路径；完成后恢复原剪贴板。"""
    text = str(p.get("text") or "")
    if not text:
        raise ValueError("text 不能为空")
    restore = bool(_settings().get("clipboard_restore", True))
    old = backend.cu_clipboard_text() if restore else None
    backend.cu_set_clipboard(text)
    backend.cu_paste()
    time.sleep(_PASTE_SLEEP)
    if restore:
        backend.cu_set_clipboard(old)
    return f"已通过剪贴板粘贴 {len(text)} 字符（规避输入法干扰，原剪贴板内容{'已恢复' if restore else '未恢复'}）。", None


def _act_key(p: dict) -> tuple:
    combo = str(p.get("combo") or "").strip()
    if not combo:
        raise ValueError("combo 不能为空（如 ctrl+s 或 enter）")
    keys = [k.strip().lower() for k in combo.replace("＋", "+").split("+") if k.strip()]
    if not keys:
        raise ValueError(f"combo 无法解析: {combo!r}")
    if len(keys) == 1:
        backend.cu_press(keys[0])
    else:
        backend.cu_hotkey(*keys)
    return f"已按键 {'+'.join(keys)}。", None


def _act_window_list(p: dict) -> tuple:
    titles = backend.cu_window_titles()
    if not titles:
        return "当前无可见窗口标题。", None
    lines = [f"{i + 1}. {t[:80]}" for i, t in enumerate(titles[:30])]
    more = f"\n（共 {len(titles)} 个，仅列前 30）" if len(titles) > 30 else ""
    return "可见窗口:\n" + "\n".join(lines) + more, None


def _act_window_focus(p: dict) -> tuple:
    title = str(p.get("title") or "").strip()
    if not title:
        raise ValueError("title 不能为空（窗口标题子串）")
    ok, active, err = backend.cu_focus_window(title)
    if not ok:
        raise ValueError(f"窗口聚焦失败: {err}")
    return f"已激活窗口「{active}」。", None


def _act_wait(p: dict) -> tuple:
    secs = _num(p.get("seconds"), "seconds") if p.get("seconds") is not None else 1.0
    secs = min(max(secs, 0.1), 10.0)
    time.sleep(secs)
    return f"已等待 {secs:g}s。", None


_DISPATCH = {
    "screenshot": _act_screenshot,
    "click": _act_click,
    "scroll": _act_scroll,
    "type": _act_type,
    "key": _act_key,
    "window_list": _act_window_list,
    "window_focus": _act_window_focus,
    "wait": _act_wait,
}


def _computer_use(action: str = "", params: Optional[dict] = None, **_) -> dict:
    """computer_use 工具执行体：动作分发 + 自动截图回注（协议见工具 description）"""
    action = str(action or "").strip().lower()
    p = params if isinstance(params, dict) else {}
    if action not in _DISPATCH:
        return _err(f"未知动作「{action}」。支持: {'/'.join(_DISPATCH)}")
    try:
        text, images = _DISPATCH[action](p)
    except backend.FailSafeException:
        return _err("触发 FAILSAFE 急停（鼠标被移至屏幕角落），动作中止")
    except (ValueError, TypeError) as exc:
        return _err(f"参数错误: {exc}")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")
    if images is None and action in _AUTO_SHOT_ACTIONS:
        try:
            settle = int(_settings().get("settle_ms") or 350)
        except (TypeError, ValueError):
            settle = 350
        if bool(_settings().get("auto_screenshot", True)):
            time.sleep(max(settle, 50) / 1000)
            try:
                path, note = _take_shot()
                text = f"{text} {note}"
                images = [path]
            except Exception as exc:
                text = f"{text} [自动截图失败: {type(exc).__name__}: {exc}]"
    if images:
        return {"content": text, "images": images}
    return {"content": text}


def setup(ctx) -> None:
    global _ctx
    if backend is None:
        ctx.log.warn("computer-use 依赖缺失（%s），未注册工具", _IMPORT_ERROR)
        return
    _ctx = ctx
    ctx.register_tool(name=_TOOL_NAME, description=_DESC, usage="computer_use(action, params)", schema=_SCHEMA)(
        _computer_use
    )
    ctx.log.info("computer_use 工具已注册）；动作: %s", "/".join(_DISPATCH))
