#待机巡检：模型登记一个到点触发的任务（一次性或周期），时间满足时经后台通知通道唤起回合
#单例——同时只允许一个巡检，重复注册被拒绝；内存态不持久化（实例重启即丢、不补触发）；
#任务绑定创建时的会话，会话切换后自销。一条线程+Event.wait：可即时取消，周期档顺延
import threading
import time
import uuid
from typing import Optional

from core import memory as memory_mod
from core.log import get_logger

log = get_logger("scheduler")

# 触发文本前缀：loop.drain_injections 按 text 前缀打 type，通知进上下文即一眼可辨来源
TRIGGER_PREFIX = "[定时任务触发]"

_task: Optional[dict] = None  # 单例巡检槽
_lock = threading.Lock()

_notifier = None              # Callable[[str, Optional[str]], None]：复用 runner.notify


def set_notifier(fn) -> None:
    """注入完成通知通道：与后台命令共用 runner.notify（busy 并入当前回合、idle 起新回合）"""
    global _notifier
    _notifier = fn


def _session_key() -> tuple:
    """任务归属会话：项目+会话 id 元组；无会话时退 (project, session) 空串仍可比"""
    session = memory_mod.get_session()
    return (
        str(getattr(session, "project_id", "") or ""),
        str(getattr(session, "session_id", "") or ""),
    )


def _describe(task: dict) -> str:
    when = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task["next_fire"]))
        if task["kind"] == "once"
        else f"每 {task['every_minutes']}分钟（下次 {time.strftime('%H:%M:%S', time.localtime(task['next_fire']))}）"
    )
    return f"id={task['id']} · {when} · {task['text']}"


def create(text: str, run_at: str = "", delay_minutes: float = 0, every_minutes: float = 0) -> str:
    """登记唯一巡检；已有任务时拒绝（先 cancel）。返回任务描述或参数错误文本"""
    text = str(text or "").strip()
    if not text:
        return "参数错误: text 不能为空"
    now = time.time()
    if every_minutes and every_minutes > 0:
        kind, every, next_fire = "every", float(every_minutes), now + float(every_minutes) * 60
    elif run_at:
        try:
            fire_at = time.mktime(time.strptime(run_at.strip(), "%Y-%m-%d %H:%M"))
        except (ValueError, OverflowError):
            return "参数错误: run_at 需为 'YYYY-MM-DD HH:MM' 本地时间"
        if fire_at <= now:
            return "参数错误: run_at 已是过去时间"
        kind, every, next_fire = "once", 0.0, fire_at
    elif delay_minutes and delay_minutes > 0:
        kind, every, next_fire = "once", 0.0, now + float(delay_minutes) * 60
    else:
        return "参数错误: 需给 delay_minutes / run_at / every_minutes 之一"

    task = {
        "id": f"t-{uuid.uuid4().hex[:6]}",
        "kind": kind,
        "every_minutes": every,
        "next_fire": next_fire,
        "text": text,
        "binding": _session_key(),
        "fired": 0,
        "cancel": threading.Event(),
    }
    with _lock:
        global _task
        if _task is not None:
            return (
                f"已存在巡检任务，同时只允许一个；如需重设请先 schedule action=cancel。\n现有: {_describe(_task)}"
            )
        _task = task
    threading.Thread(target=_run, args=(task,), daemon=True, name=f"bawcode-sched-{task['id']}").start()
    log.info("巡检任务登记: %s", _describe(task))
    return (
        f"已登记巡检（内存态单例，实例重启即丢；仅限当前会话）\n{_describe(task)}\n"
        "到点后系统会自动唤起回合执行；用 schedule action=list|cancel 查看或取消。"
    )


def list_tasks() -> str:
    """列出本会话名下巡检；无则明说"""
    binding = _session_key()
    with _lock:
        task = _task if _task is not None and _task["binding"] == binding else None
    if task is None:
        return "当前会话没有巡检任务。"
    extra = f" · 已触发 {task['fired']} 次" if task["kind"] == "every" else ""
    return f"当前会话巡检：{_describe(task)}{extra}"


def cancel(task_id: str = "") -> str:
    """取消当前巡检；id 可省（单例），给了则校验一致性"""
    global _task
    with _lock:
        task = _task
        if task is None or task["binding"] != _session_key():
            return "当前会话没有可取消的巡检任务。"
        if task_id and task_id.strip() and task["id"] != task_id.strip():
            return f"参数错误: 当前巡检 id 是 {task['id']}，不是 {task_id}"
        task["cancel"].set()
        _task = None
    log.info("巡检任务取消: id=%s", task["id"])
    return f"已取消巡检 id={task['id']}。"


def _run(task: dict) -> None:
    """巡检线程：Event.wait 睡到触发点；会话换了或取消即自销，周期档到点后顺延"""
    try:
        while not task["cancel"].is_set():
            delay = task["next_fire"] - time.time()
            if delay > 0 and task["cancel"].wait(delay):
                return  # 被取消
            if _session_key() != task["binding"]:
                log.info("巡检因会话切换失效: id=%s", task["id"])
                return
            _fire(task)
            if task["kind"] != "every":
                return
            task["next_fire"] = time.time() + task["every_minutes"] * 60
    finally:
        with _lock:
            global _task
            if _task is task:
                _task = None


def _fire(task: dict) -> None:
    task["fired"] += 1
    note = (
        f"{TRIGGER_PREFIX} id={task['id']} · 第 {task['fired']} 次"
        + (f"（每 {task['every_minutes']} 分钟）" if task["kind"] == "every" else "")
        + f"\n任务内容: {task['text']}\n请按任务内容执行；后台任务完成后用 schedule action=cancel 取消巡检。"
    )
    fn = _notifier
    if fn is None:
        log.warn("巡检触发但无通知通道，已丢弃: id=%s", task["id"])
        return
    try:
        fn(note, task["binding"][1] or None)
    except TypeError:
        # 兼容历史的一参通知回调
        try:
            fn(note)
        except Exception as exc:
            log.error("巡检通知投递失败 id=%s: %r", task["id"], exc)
    except Exception as exc:
        log.error("巡检通知投递失败 id=%s: %r", task["id"], exc)
