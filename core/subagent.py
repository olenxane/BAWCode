# -*- coding: utf-8 -*-
"""子代理系统：主代理经 task 工具异步派发独立上下文的子代理，配套 wait/cancel/query 工具。

异步派发：task 立即返回任务编号，子代理在本进程内以后台线程持续执行（跨主回合、
不受主回合 Esc 影响；应用退出时统一取消）。主代理可继续工作、再派多个任务，
用 wait_subagent(id, timeout) 等待（超时返回进度）或等完成后被通知唤醒：
完成通知经 Runner 进入当前回合的注入队列，在下一个安全轮次边界作为 user 消息交给模型。

每个任务持有独立运行上下文（自己的 LLM 实例与取消信号、派发时捕获的 session/config、
线程本地工具运行时与文件快照序列），与主回合的上下文互不干扰。子代理消息列表为局部
变量，不进主 session、不参与压缩，全程轨迹按记录文件落盘：
data/subagents/{project_id}/{session_id}/{id}.json，id 为会话内顺序编号（"1"/"2"/…）。

权限（permission）：子代理权限跟随派发时的主代理访问模式，派发参数不改变权限。
子代理没有确认通道：需要用户确认的调用一律拒绝，模型可经 request_permission 申请提权；
提权由主代理当前权限裁决——可自主放行的直接放行，自动模式交 LLM 裁决，
其余（confirm/edit）交由用户确认（对话框锁串行）。被当前模式整体拦住时还可用
request_mode_switch 申请切换访问模式：走同一用户面板裁决，批准后新模式立即生效、
子代理权限随之提升（桥为任务线程级，主回合解绑后台任务仍可用）。子代理内每次工具调用照走
policy.evaluate，不构成权限旁路。

角色（role）：仅决定系统提示词与模型（data/agents/<role>.json，内置 universal 兜底），
不携带工具白名单；工具范围统一剔除 task/wait/cancel/query 与会话状态工具（防污染主会话
计划/记忆），递归天然不存在。
"""
from __future__ import annotations

import copy
import json
import os
import threading
from contextlib import contextmanager
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import policy
from core import memory as memory_mod
from core import prompt_loader
from core import register
from core.llm import CANCELLED
from core.log import get_logger
from core.tools import SpinGuard

log = get_logger("subagent")

_ROOT = Path(__file__).resolve().parent.parent

# 子代理不可用的工具：task/query_subagent 递归防线，加经 memory.get_session() 全局单例操作的
# 会话状态工具，子代理调用会污染主会话计划与记忆
_SUBAGENT_EXCLUDED_TOOLS = {
    "task",
    "query_subagent",
    # 等待/取消是主代理对后台任务的编排动作，子代理不该阻塞在别的任务上
    "wait_subagent",
    "cancel_subagent",
    "write_plan",
    "update_plan",
    "generate_steps",
    "update_step_status",
    # RAG 项目级开关写插件状态文件，属主会话语义；检索/重建对子代理开放
    "set_rag",
}

# 模式切换工具：主代理侧走回合内全局桥，子代理侧走任务线程桥（_task_scope 绑定）
_MODE_SWITCH_TOOL = "request_mode_switch"

# 子代理可识别的模式；实际权限由主代理当前模式唯一决定
_SUBAGENT_MODES = set(policy.MODES)

# 记录编号分配锁：同轮并行派发时 _next_id 的 max+1 会互相抢号
_RECORDS_LOCK = threading.Lock()
_TASKS_LOCK = threading.RLock()
_TASKS: Dict[str, dict] = {}
_TASK_GATES: Dict[str, threading.Semaphore] = {}

# 并发派发默认上限：同一会话内多个任务共用并发限制
_DEFAULT_MAX_CONCURRENT = 3

# 提权工具：子代理专属（不进共享注册表，主代理侧不可见），由 _execute_one 拦截处理
_ELEVATION_TOOL = "request_permission"
_ELEVATION_DEF = {
    "type": "function",
    "function": {
        "name": _ELEVATION_TOOL,
        "description": (
            "Request elevated permission from the main agent for a tool call that was "
            "just denied for permission. The main agent's current access mode decides: "
            "granted directly when it may auto-allow, adjudicated by the LLM in auto "
            "mode, otherwise handed to the user. If granted, the call is executed and "
            "its result returned; if denied, the call must not be retried"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "Name of the denied tool to run"},
                "arguments": {"type": "object", "description": "Arguments for that tool call"},
                "reason": {"type": "string", "description": "Short justification for the elevated call"},
            },
            "required": ["tool", "arguments", "reason"],
        },
    },
}

_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "cancelling": "取消中",
    "done": "完成",
    "interrupted": "已中断",
    "max_rounds": "达轮次上限",
    "error": "出错",
}

_UNIVERSAL_PROMPT = (
    "你是 BAWCode 的子代理，独立完成主代理委托的一个边界清晰的任务，无法与用户交互。\n"
    "- 直接执行并输出结果，不要反问\n"
    "- 结论简洁、结构化，附必要依据（文件路径:行号）\n"
    "- 检索无果时明确说明查过什么、没查到什么，不臆测\n"
    "- 命令按当前执行器（^{shell_hint}^）的语法写：顺序执行、变量与引号、多行的写法随执行器不同"
    "（cmd.exe 用 '&&' 且必须单行、不支持 heredoc；PowerShell 5.1 用 ';'；bash 支持多行与 heredoc），"
    "语法不符会被前置拦下并提示改法；需要别的执行器时可在调用里传 shell 参数；"
    "返回“无输出”就是真的没有输出，不要据此臆断工具不回显\n"
    "- 工具权限跟随主代理当前访问模式；调用因权限被拒绝时，如确有必要可调用 "
    "request_permission 申请放行本次调用（写明理由），裁决结果可能立即放行、交由用户或 LLM 决定；"
    "若被当前模式整体拦住，可用 request_mode_switch 申请切换到权限足够的模式（用户裁决，批准后立即生效）"
)

# 角色表：name -> {description, model, prompt, prompt_file}；load_specs 重建
_SPECS: Dict[str, dict] = {}

# 线程本地运行时：主回合与各异步子代理线程隔离，回合解绑不影响后台任务
_RUNTIME_LOCAL = threading.local()


# ---------------------------------------------------------------------------
# 配置与运行时


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def subagent_cfg(config) -> dict:
    data = (getattr(config, "data", None) or {}).get("subagent") or {}
    return {
        "enabled": bool(data.get("enabled", True)),
        "dir": str(data.get("dir") or "data/subagents"),
        "max_rounds": max(1, _int(data.get("max_rounds"), 20)),
        "result_char_cap": max(200, _int(data.get("result_char_cap"), 3000)),
        "query_default_rounds": max(1, _int(data.get("query_default_rounds"), 3)),
        "max_concurrent": max(1, _int(data.get("max_concurrent"), _DEFAULT_MAX_CONCURRENT)),
    }


def bind_runtime(llm, config, io, app=None, session=None, **extra) -> None:
    _RUNTIME_LOCAL.value = {"llm": llm, "config": config, "io": io, "app": app, "session": session, **extra}


def unbind_runtime() -> None:
    _RUNTIME_LOCAL.value = None


def _runtime() -> Optional[dict]:
    return getattr(_RUNTIME_LOCAL, "value", None)


def _runtime_snapshot() -> Optional[dict]:
    """为派发的异步任务捕获依赖引用；具体 LLM 与取消态在 worker 创建。"""
    rt = _runtime()
    return dict(rt) if rt else None


# ---------------------------------------------------------------------------
# 角色


def _agents_dir(config) -> Path:
    raw = str(((getattr(config, "data", None) or {}).get("subagent") or {}).get("agents_dir") or "data/agents")
    p = Path(raw)
    return p if p.is_absolute() else _ROOT / raw


def load_specs(config) -> int:
    """扫描 data/agents/*.json 重建角色表；universal 内置兜底（同名文件可覆盖其提示词）"""
    global _SPECS
    specs: Dict[str, dict] = {
        "universal": {
            "name": "universal",
            "description": "通用子代理：极简系统提示词，直接执行委托任务",
            "model": "",
            "prompt": _UNIVERSAL_PROMPT,
            "prompt_file": "",
        }
    }
    directory = _agents_dir(config)
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warn("角色文件解析失败，跳过 %s: %s", path, e)
                continue
            if not isinstance(data, dict):
                log.warn("角色文件根节点须为对象，跳过: %s", path)
                continue
            name = str(data.get("name") or path.stem).strip()
            if not name:
                log.warn("角色文件缺少 name，跳过: %s", path)
                continue
            specs[name] = {
                "name": name,
                "description": str(data.get("description") or ""),
                "model": str(data.get("model") or ""),
                "prompt": str(data.get("prompt") or ""),
                "prompt_file": str(data.get("prompt_file") or ""),
            }
    _SPECS = specs
    log.info("子代理角色已加载: %s", ", ".join(specs) or "（无）")
    return len(specs)


def roles_listing() -> str:
    lines = []
    for spec in _SPECS.values():
        desc = spec.get("description") or ""
        model = f" · 模型 {spec['model']}" if spec.get("model") else ""
        lines.append(f"  {spec['name']}: {desc}{model}")
    return "\n".join(lines) or "  （无）"


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(str(status or ""), str(status or ""))


def _role_prompt(spec: dict, task: str, context: str, config) -> str:
    variables = prompt_loader.build_variable_context(
        config=config, extra={"task": task, "context": context or "（无）"}
    )
    if spec.get("prompt_file"):
        segs = prompt_loader.load_prompt_bundle([spec["prompt_file"]], variables, config=config)
        if segs:
            return prompt_loader.join_segments(segs)
        log.warn("角色提示词文件缺失，回退内联/内置: %s", spec["prompt_file"])
    return prompt_loader.render_text(spec.get("prompt") or _UNIVERSAL_PROMPT, variables)


# ---------------------------------------------------------------------------
# 权限


def inherited_permission(mode: str) -> str:
    """子代理权限始终跟随主代理当前模式"""
    normalized = str(mode or "").strip().lower()
    return normalized if normalized in _SUBAGENT_MODES else policy.MODE_AUTO


def _current_mode(rt: dict) -> str:
    app = rt.get("app")
    mode = getattr(app, "mode", "") if app is not None else ""
    if not mode:
        mode = getattr(rt.get("config"), "mode", "") or ""
    return str(mode or policy.MODE_AUTO)


# ---------------------------------------------------------------------------
# 记录存储：data/subagents/{project_id}/{session_id}/{id}.json


def _records_dir(config, session) -> Path:
    base = Path(subagent_cfg(config)["dir"])
    base = base if base.is_absolute() else _ROOT / str(base)
    project_id = str(getattr(session, "project_id", "") or "_default")
    session_id = str(getattr(session, "session_id", "") or "_default")
    return base / project_id / session_id


def _record_path(directory: Path, record_id: str) -> Path:
    # id 由 _next_id 生成为纯数字；外部入参 query_subagent/resume_id 同样只接受纯数字，
    # 防止 ".."、盘符等内容注入路径读到目录外文件，0.json 不存在时等效查无此记录
    safe = str(record_id).strip()
    return directory / f"{safe if safe.isdigit() else '0'}.json"


def _next_id(directory: Path) -> str:
    nums = [int(p.stem) for p in directory.glob("*.json") if p.stem.isdigit()]
    return str(max(nums, default=0) + 1)


def _save_record(directory: Path, record: dict) -> Optional[Path]:
    record["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = _record_path(directory, str(record.get("id")))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.error("子代理记录写入失败 %s: %s", path, e)
        return None
    return path


def create_record(directory: Path, build, record_id: Optional[str] = None) -> tuple:
    """并发安全地编号、组装和首次落盘；返回 (record, path)。"""
    with _RECORDS_LOCK:
        rid = str(record_id) if record_id is not None else _next_id(directory)
        record = build(rid)
        path = _save_record(directory, record)
    return record, path


def _task_key(project_id: str, session_id: str, task_id: str) -> str:
    return f"{project_id or '_default'}:{session_id or '_default'}:{task_id}"


def running_tasks_for_session(session) -> List[dict]:
    """本会话仍在排队/运行/取消中的任务 id 列表（回合末保留其直播槽用）"""
    project_id = str(getattr(session, "project_id", "") or "")
    session_id = str(getattr(session, "session_id", "") or "")
    with _TASKS_LOCK:
        return [
            {"id": str(task.get("id")), "status": task.get("status"), "phase": task.get("phase")}
            for task in _TASKS.values()
            if task.get("project_id") == project_id
            and task.get("session_id") == session_id
            and task.get("status") in ("queued", "running", "cancelling")
        ]


def pending_task_count() -> int:
    """进程内仍在排队/运行的后台子代理数（无头收尾用）"""
    with _TASKS_LOCK:
        return sum(1 for t in _TASKS.values() if t.get("status") in ("queued", "running", "cancelling"))


def wait_tasks(timeout: float = 1.0) -> None:
    """等待后台子代理收敛（全部结束或超时；无头模式跑完通知回合前的收尾）"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _TASKS_LOCK:
            pending = [t for t in _TASKS.values() if t.get("status") in ("queued", "running", "cancelling")]
        if not pending:
            return
        for task in pending:
            task["done"].wait(min(0.2, max(0.0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            return


def retain_running_slots(app, session) -> None:
    """回合末清理直播槽：仍在跑的后台子代理保留（跨回合存活），其余移除"""
    running_ids = {item["id"] for item in running_tasks_for_session(session)}
    slots = getattr(app, "subagent_stream", None)
    slots = slots if isinstance(slots, dict) else {}
    app.subagent_stream = {key: slot for key, slot in slots.items() if key in running_ids}


def task_status(config, session, task_id: str) -> Optional[dict]:
    key = _task_key(str(getattr(session, "project_id", "") or ""), str(getattr(session, "session_id", "") or ""), str(task_id))
    with _TASKS_LOCK:
        task = _TASKS.get(key)
        if task:
            return {k: task.get(k) for k in ("id", "status", "phase", "rounds", "tools_used", "last_tool", "updated_at", "summary", "error")}
    record = load_record(config, session, str(task_id))
    return record if record else None


def cancel_task(config, session, task_id: str) -> bool:
    status = task_status(config, session, task_id)
    if not status or status.get("status") not in ("queued", "running", "cancelling"):
        return False
    key = _task_key(str(getattr(session, "project_id", "") or ""), str(getattr(session, "session_id", "") or ""), str(task_id))
    with _TASKS_LOCK:
        task = _TASKS.get(key)
        if not task:
            return False
        task["cancel"].set()
        task_llm = (task.get("runtime") or {}).get("llm")
        if task_llm is not None:
            task_llm.cancel()
        task["status"] = "cancelling"
        task["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return True


def stop_all_tasks(timeout: float = 3.0) -> None:
    with _TASKS_LOCK:
        tasks = list(_TASKS.values())
        for task in tasks:
            task["cancel"].set()
            task_llm = (task.get("runtime") or {}).get("llm")
            if task_llm is not None:
                task_llm.cancel()
    deadline = time.monotonic() + max(0.0, timeout)
    for task in tasks:
        thread = task.get("thread")
        if thread and thread.is_alive():
            thread.join(max(0.0, deadline - time.monotonic()))


def _task_mode_bridge(task: dict):
    """任务线程的模式切换申请桥：补上任务自身取消信号（面板等待随之可中断）；
    无桥时返回 None，工具层回落全局桥"""
    bridge = task.get("mode_bridge")
    if not callable(bridge):
        return None
    cancel = task["cancel"].is_set
    return lambda payload: bridge(payload, cancel)


@contextmanager
def _task_scope(task: dict):
    """绑定后台子代理的会话、文件快照与模式切换申请桥上下文。"""
    from contextlib import ExitStack
    from core import tools as tools_mod
    with ExitStack() as stack:
        stack.enter_context(memory_mod.bind_session(task["session"]))
        stack.enter_context(_bind_task_runtime(task))
        # 模式切换申请桥：任务线程级，主回合结束解绑后子代理仍能经用户面板申请切模式
        stack.enter_context(tools_mod.bind_thread_mode_bridge(_task_mode_bridge(task)))
        import core.snapshot as snapshot_mod
        stack.enter_context(snapshot_mod.bind_context(task["config"], task["project_id"], task["session_id"], task["id"]))
        yield


class _TaskIO:
    """后台任务的最小 IO 面：取消信号来自任务自身，确认交主线程面板（经确认桥）。

    llm 由 worker 启动时注入（确认桥批准后的工具执行走任务的独立 LLM）。"""

    def __init__(self, task: dict):
        self.task = task
        self.llm = None

    def cancelled(self):
        return self.task["cancel"].is_set()

    def tool_confirm(self, call):
        bridge = self.task.get("confirm_bridge")
        if bridge is None or self.llm is None or self.cancelled():
            return {"content": policy.default_reject_message("无确认通道或任务已取消")}
        return bridge(self.llm, call, self.cancelled)


def _bind_task_runtime(task: dict):
    class _Scope:
        def __enter__(self):
            self.previous = _runtime()
            _RUNTIME_LOCAL.value = task["runtime"]
            return task["runtime"]

        def __exit__(self, *_):
            _RUNTIME_LOCAL.value = self.previous
    return _Scope()


def _update_task(task: dict, **updates) -> None:
    with _TASKS_LOCK:
        task.update(updates)
        task["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def _emit_completion(task: dict) -> None:
    runner = task.get("runner")
    notify = getattr(runner, "notify_subagent_completion", None)
    if callable(notify):
        try:
            notify(
                task["id"],
                task.get("summary") or f"子代理 #{task['id']} 状态：{task.get('status')}",
                origin_turn_id=task.get("origin_turn_id"),
            )
        except TypeError:
            # 兼容只实现旧签名的 runner 测试替身/外部集成。
            try:
                notify(task["id"], task.get("summary") or f"子代理 #{task['id']} 状态：{task.get('status')}")
            except Exception as exc:
                log.error("子代理完成通知失败 #%s: %r", task["id"], exc)
        except Exception as exc:
            log.error("子代理完成通知失败 #%s: %r", task["id"], exc)


def _background_task_worker(task: dict, record: dict, cfg: dict, directory: Path, record_path: Optional[Path]) -> None:
    task_id = str(task["id"])
    messages = record["messages"]
    status, final_text = "error", "后台任务未启动"
    try:
        if task["cancel"].is_set():
            status, final_text = "interrupted", "任务在启动前被取消"
        else:
            with task["gate"]:
                if task["cancel"].is_set():
                    status, final_text = "interrupted", "任务在排队期间被取消"
                else:
                    from core.llm import LLM
                    factory = task.get("llm_factory")
                    task_llm = factory() if callable(factory) else LLM(task["config"])
                    task["runtime"]["llm"] = task_llm
                    task["runtime"]["io"].llm = task_llm
                    _update_task(task, status="running", phase="启动子代理")
                    record["status"] = "running"
                    _save_record(directory, record)
                    live = _live(task["runtime"], task_id)
                    if live is not None:
                        live.update({"id": task_id, "role": record["role"], "phase": "运行中", "text": "", "tools": []})
                    with _task_scope(task):
                        wf_cfg = (getattr(task["config"], "data", None) or {}).get("workflow") or {}
                        spin = SpinGuard(_int(wf_cfg.get("spin_kill_count"), 12))
                        status, final_text = _run_loop(
                            task["runtime"], record, messages, _sub_tool_defs(), cfg["max_rounds"], spin, directory
                        )
    except Exception as exc:
        log.error("后台子代理 #%s 执行异常: %r", task_id, exc)
        status, final_text = "error", f"内部异常: {exc}"
    if task["cancel"].is_set() and status not in ("error",):
        status, final_text = "interrupted", "任务被取消"
    record["status"] = status
    record["artifacts"] = extract_artifacts(messages)
    record["record_path"] = str(record_path or "")
    record_path = _save_record(directory, record) or record_path
    # 会话树节点（UI）
    session = task.get("session")
    if session is not None:
        try:
            session.add_message(
                "assistant",
                f"子代理 #{task_id} · {record.get('role')} · {_STATUS_LABELS.get(status, status)}"
                f"（{record.get('rounds', 0)}轮/{record.get('tools_used', 0)}次工具）",
                type="subagent",
                subagent_id=task_id,
                subagent_role=record.get("role"),
                status=status,
                trace=messages,
            )
        except Exception as exc:
            log.warn("子代理 #%s 会话节点写入失败: %r", task_id, exc)
    summary = _build_summary(record, final_text, cfg, record_path)
    record["summary"] = summary
    _save_record(directory, record)
    _live_clear(task["runtime"], task_id)
    _update_task(
        task,
        status=status,
        phase="完成" if status == "done" else _STATUS_LABELS.get(status, status),
        rounds=record.get("rounds", 0),
        tools_used=record.get("tools_used", 0),
        summary=summary,
        error=final_text if status == "error" else "",
        record=record,
    )
    task["done"].set()
    _emit_completion(task)


def submit_task(rt: dict, record: dict, cfg: dict, directory: Path, record_path: Optional[Path]) -> dict:
    """注册进程内后台任务并立即启动 worker。"""
    task_id = str(record["id"])
    project_id = str(getattr(rt.get("session"), "project_id", "") or "")
    session_id = str(getattr(rt.get("session"), "session_id", "") or "")
    runner = rt.get("runner")
    llm_factory = rt.get("task_llm_factory")
    cancel = threading.Event()
    with _TASKS_LOCK:
        gate = _TASK_GATES.setdefault(
            f"{project_id}:{session_id}",
            threading.Semaphore(max(1, int(cfg.get("max_concurrent") or _DEFAULT_MAX_CONCURRENT))),
        )
    task = {
        "id": task_id,
        "project_id": project_id,
        "session_id": session_id,
        "config": rt["config"],
        "session": rt.get("session"),
        "runner": runner,
        "origin_turn_id": rt.get("origin_turn_id"),
        "app": rt.get("app"),
        "confirm_bridge": rt.get("confirm_bridge"),
        # 模式切换申请桥（payload, cancelled）-> {"approved", "detail"}；无交互环境为 None
        "mode_bridge": rt.get("mode_bridge"),
        "cancel": cancel,
        "gate": gate,
        "done": threading.Event(),
        "status": "queued",
        "phase": "排队中",
        "rounds": 0,
        "tools_used": 0,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": "",
        "record": record,
    }
    task_runtime = {**rt, "llm": None, "io": _TaskIO(task), "task_id": task_id, "task": task}
    task["runtime"] = task_runtime
    task["llm_factory"] = llm_factory
    key = _task_key(project_id, session_id, task_id)
    with _TASKS_LOCK:
        _TASKS[key] = task
    thread = threading.Thread(
        target=_background_task_worker,
        args=(task, record, cfg, directory, record_path),
        daemon=True,
        name=f"bawcode-subagent-task-{task_id}",
    )
    task["thread"] = thread
    thread.start()
    return task


def load_record(config, session, record_id: str) -> Optional[dict]:
    path = _record_path(_records_dir(config, session), str(record_id))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warn("子代理记录读取失败 %s: %s", path, e)
        return None
    return data if isinstance(data, dict) and isinstance(data.get("messages"), list) else None


def list_records(config, session) -> List[dict]:
    directory = _records_dir(config, session)
    if not directory.is_dir():
        return []
    items = []
    for path in sorted(directory.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            items.append(data)
    return items


# ---------------------------------------------------------------------------
# 直播槽 app.subagent_stream：{id: slot}，每个子代理一个槽；主线程只读快照，agent 线程/子代理线程写入


def _live(rt: dict, record_id: str) -> Optional[dict]:
    """取（或建）本子代理的直播槽；id 为空时退化为单个匿名槽"""
    app = rt.get("app")
    if app is None:
        return None
    slots = getattr(app, "subagent_stream", None)
    if not isinstance(slots, dict):
        slots = {}
        app.subagent_stream = slots
    key = str(record_id or "")
    slot = slots.get(key)
    if not isinstance(slot, dict):
        slot = {"id": key, "role": "", "phase": "", "text": "", "tools": []}
        slots[key] = slot
    return slot


def _live_text_delta(rt: dict, record_id: str, piece: str) -> None:
    live = _live(rt, record_id)
    if live is not None and piece:
        live["text"] = (live.get("text") or "")[-1200:] + piece


def _live_phase(rt: dict, record_id: str, text: str) -> None:
    live = _live(rt, record_id)
    if live is not None:
        live["phase"] = text
    task = rt.get("task")
    if task is not None:
            _update_task(task, phase=text, rounds=(task.get("record") or {}).get("rounds", 0), tools_used=(task.get("record") or {}).get("tools_used", 0))



def _live_tool(rt: dict, record_id: str, line: str) -> None:
    live = _live(rt, record_id)
    if live is not None:
        tools = live.setdefault("tools", [])
        tools.append(line)
        del tools[:-8]  # 只保留最近 8 行工具轨迹
    task = rt.get("task")
    if task is not None:
        _update_task(task, last_tool=line)


def _live_clear(rt: dict, record_id: str) -> None:
    app = rt.get("app")
    slots = getattr(app, "subagent_stream", None) if app is not None else None
    if isinstance(slots, dict):
        slots.pop(str(record_id or ""), None)


# ---------------------------------------------------------------------------
# 执行


def _sub_tool_defs() -> List[dict]:
    """共享注册表的视图过滤：剔除递归/会话状态工具，不改动注册表本体；
    追加子代理专属提权工具（不在注册表内，主代理侧不可见）"""
    defs = [d for d in register.get_tool_defs() if d["function"]["name"] not in _SUBAGENT_EXCLUDED_TOOLS]
    defs.append(copy.deepcopy(_ELEVATION_DEF))
    return defs


def _normalize_tool_msg(call: dict, content: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": str(call.get("id") or ""),
        "tool_name": call.get("name"),
        "content": str(content or ""),
        "type": "tool",
    }


def _with_images(item: dict) -> dict:
    """执行层携带的 images（本地路径）转为数组形态 content，供子代理消息循环透传图片"""
    imgs = item.pop("images", None)
    if not imgs:
        return item
    parts, notes = memory_mod.build_image_parts(imgs)
    text = memory_mod.content_text(item.get("content"))
    if notes:
        text = (text.rstrip() + "\n" if text.strip() else "") + "[图片说明] " + "；".join(notes)
    item["content"] = [{"type": "text", "text": text}] + parts
    return item


def _denied(call: dict, reason: str) -> dict:
    """权限不足的拒绝回执：子代理没有确认通道，提示可申请放行本次调用或切换模式"""
    return _normalize_tool_msg(
        call,
        f"权限不足被拒绝（{reason}）：子代理权限跟随主代理当前模式。"
        f"如确有必要，可调用 {_ELEVATION_TOOL} 说明理由申请放行本次调用（可能由用户或 LLM 裁决），"
        f"或用 {_MODE_SWITCH_TOOL} 申请切换到权限足够的模式（用户裁决）。",
    )


def _elevation_result(call: dict, item: dict) -> dict:
    """提权结果归到 request_permission 调用名下（工具结果与 assistant.tool_calls 配对一致）"""
    item = _with_images(item)
    item["tool_call_id"] = str(call.get("id") or "")
    item["tool_name"] = call.get("name")
    return item


def _elevation_execute(llm, call: dict, target: dict) -> dict:
    item = llm.execute_approved_tool(target)
    if isinstance(item, dict) and "content" in item:
        return _elevation_result(call, item)
    return _normalize_tool_msg(call, str(item))


def _handle_elevation(rt: dict, call: dict) -> dict:
    """提权请求：主代理可自主放行的直接执行；自动模式交 LLM 裁决；其余交用户决定"""
    io, llm = rt["io"], rt["llm"]
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    tool = str(args.get("tool") or "")
    target_args = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}
    reason = str(args.get("reason") or "").strip()
    if tool in _SUBAGENT_EXCLUDED_TOOLS or tool == _ELEVATION_TOOL:
        return _normalize_tool_msg(call, f"该工具不对此子代理开放: {tool}")
    if not register.has_tool(tool):
        return _normalize_tool_msg(call, f"工具不存在: {tool}")
    target = {"id": f"call_{uuid.uuid4().hex[:12]}", "name": tool, "arguments": target_args, "type": "function"}
    mode = _current_mode(rt)
    log.info("子代理提权请求: %s mode=%s 理由=%s", tool, mode, reason or "（无）")
    action, _ = policy.evaluate(tool, target_args, mode)
    if action == policy.ALLOW:
        return _elevation_execute(llm, call, target)
    if mode == policy.MODE_AUTO:
        # 自动模式：不确定的命令（含危险类）由 LLM 裁决
        if llm.approve_tool_call(target):
            return _elevation_execute(llm, call, target)
        return _normalize_tool_msg(call, policy.approval_reject_message(f"提权请求被拒绝: {tool}（LLM 裁决）"))
    # confirm/edit：交用户裁决，批准时确认桥已完成执行；理由经 description 传面板（执行前由执行层剥离）
    if getattr(io, "tool_confirm", None) is None:
        return _normalize_tool_msg(call, f"提权请求被拒绝: {tool}（无确认通道）")
    note = f"子代理提权请求：{reason}" if reason else "子代理提权请求"
    confirm_target = {**target, "arguments": {**target_args, "description": note}}
    item = io.tool_confirm(confirm_target)
    if isinstance(item, dict) and "content" in item:
        return _elevation_result(call, item)
    return _normalize_tool_msg(call, f"提权请求被拒绝: {tool}（用户拒绝）")


def _execute_one(rt: dict, call: dict, permission: str) -> dict:
    """子代理内单个工具调用：照走主策略；需确认的调用一律拒绝并提示提权。
    request_mode_switch 属纯交互工具（各模式放行），经任务线程桥弹用户面板申请切模式"""
    llm = rt["llm"]
    name = str(call.get("name") or "")
    args = call.get("arguments")
    if not isinstance(args, dict):
        args = {}
    call = {**call, "id": str(call.get("id") or f"call_{uuid.uuid4().hex[:12]}"), "arguments": args}
    if "description" in args:
        call["description"] = str(args.pop("description") or "")

    if name == _ELEVATION_TOOL:
        return _handle_elevation(rt, call)
    if name in _SUBAGENT_EXCLUDED_TOOLS:
        return _normalize_tool_msg(call, "该工具不对此子代理开放")
    if not register.has_tool(name):
        return _normalize_tool_msg(call, f"工具不存在: {name}")

    action, reason = policy.evaluate(name, args, permission)
    if action == policy.ALLOW:
        item = llm.execute_approved_tool(call)
        return _with_images(item) if isinstance(item, dict) and "content" in item else _normalize_tool_msg(call, item)
    if action == policy.LLM_APPROVE:
        if llm.approve_tool_call(call):
            item = llm.execute_approved_tool(call)
            return _with_images(item) if isinstance(item, dict) and "content" in item else _normalize_tool_msg(call, item)
        return _normalize_tool_msg(
            call,
            policy.approval_reject_message(
                f"权限不足: {name}。如该调用为任务所必需，可调用 {_MODE_SWITCH_TOOL} "
                "说明理由申请切换访问模式（用户裁决）。"
            ),
        )
    return _denied(call, reason)


def extract_artifacts(messages: List[dict]) -> List[str]:
    """产物提取：从工具调用历史提取修改的文件/执行的命令/加载的技能（去重保序）"""
    arts: List[str] = []
    seen = set()
    for m in messages or []:
        for c in m.get("tool_calls") or []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "")
            args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
            label = ""
            if name in ("write", "edit_file"):
                label = f"修改 {args.get('file_path', '')}"
            elif name == "execute_command":
                cmd = str(args.get("command") or "").strip()
                label = f"命令 {cmd[:80]}" if cmd else ""
            elif name == "run_program":
                label = f"运行 {args.get('program', '')}"
            elif name == "load_skill":
                label = f"技能 {args.get('skill', '')}"
            if label and label not in seen:
                seen.add(label)
                arts.append(label)
    return arts


def _last_assistant_text(messages: List[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "assistant" and str(m.get("content") or "").strip():
            return str(m["content"])
    return ""


def _build_summary(record: dict, final_text: str, cfg: dict, record_path: Optional[Path]) -> str:
    status = str(record.get("status") or "done")
    label = _STATUS_LABELS.get(status, status)
    body = (final_text or "").strip() or _last_assistant_text(record.get("messages") or [])
    cap = cfg["result_char_cap"]
    if len(body) > cap:
        body = body[:cap] + f"\n…（超 {cap} 字截断，完整内容可 query_subagent 查看）"
    parts = [
        f"[子代理 #{record.get('id')} · {record.get('role')} · {label}"
        f" · {record.get('rounds', 0)}轮 · {record.get('tools_used', 0)}次工具]"
    ]
    if status == "error" and final_text:
        parts.append(f"出错原因: {final_text}")
    else:
        parts.append(body or "（无文本输出）")
    arts = record.get("artifacts") or []
    if arts:
        parts.append("产物:\n" + "\n".join(f"- {a}" for a in arts))
    pointer = f"完整轨迹已保存: {record_path}" if record_path else "完整轨迹保存失败（记录未落盘）"
    parts.append(f"过程详情可用 query_subagent(id=\"{record.get('id')}\") 查询；{pointer}")
    return "\n".join(parts)


def _run_loop(rt: dict, record: dict, messages: List[dict], tool_defs: List[dict],
              max_rounds: int, spin: SpinGuard, directory: Path) -> tuple:
    """子代理执行循环。返回 (status, final_text)；防空转与主循环同判据（SpinGuard：
    重复/近似重复输出才开始计数，整轮全新结果清零，计满 spin_kill_count 终止）；
    续期总量受 config.workflow.max_total_rounds 兜底保险丝约束（默认 100）"""
    llm, io = rt["llm"], rt["io"]
    rid = str(record.get("id") or "")
    wf_cfg = (getattr(rt.get("config"), "data", None) or {}).get("workflow") or {}
    try:
        fuse_cfg = int(wf_cfg.get("max_total_rounds") or 0)
    except (TypeError, ValueError):
        fuse_cfg = 0
    total_cap = fuse_cfg if fuse_cfg > 0 else max(100, max(1, max_rounds))
    rounds = 0
    while True:
        if rounds >= max_rounds:
            if rounds >= total_cap:
                log.info("子代理 #%s 回合计达兜底上限 %d 轮，终止", record.get("id"), total_cap)
                return ("max_rounds", "")
            if spin.spun_out():
                log.info("子代理 #%s 空转计数满 %d 次，终止", record.get("id"), spin.count)
                return ("max_rounds", "")
            spin_text = f"空转计数 {spin.count}/{spin.kill_count}"
            _live_phase(rt, rid, f"轮次续期 · {spin_text}")
            log.info("子代理 #%s 轮次续期，%s", record.get("id"), spin_text)
        rounds += 1
        record["rounds"] = rounds
        if io.cancelled() or llm.cancelled():
            return ("interrupted", "")
        _live_phase(rt, rid, f"推理 · 第{rounds}轮")

        def on_delta(kind, piece):
            if kind == "reset":
                live = _live(rt, rid)
                if live is not None:
                    live["text"] = ""
            elif kind == "content":
                _live_text_delta(rt, rid, piece)

        response = llm.chat(
            messages,
            tools=tool_defs,
            model=str(record.get("model") or "") or None,
            on_delta=on_delta,
        )
        if response.get("error") == CANCELLED or io.cancelled():
            return ("interrupted", "")
        if response.get("error"):
            return ("error", str(response["error"]))
        content = str(response.get("content") or "")
        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            if content:
                messages.append({"role": "assistant", "content": content})
            return ("done", content)
        norm_calls = [
            {
                "id": str(c.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                "name": c.get("name"),
                "arguments": c.get("arguments") if isinstance(c.get("arguments"), dict) else {},
                "type": "function",
            }
            for c in tool_calls
        ]
        messages.append({"role": "assistant", "content": content, "tool_calls": norm_calls})
        _live_phase(rt, rid, f"工具调用中 · 第{rounds}轮")
        round_results = []
        processed = []
        try:
            for call in norm_calls:
                permission = inherited_permission(_current_mode(rt))
                record["permission"] = permission
                item = _execute_one(rt, call, permission)
                messages.append(item)
                round_results.append(item)
                processed.append(call)
                record["tools_used"] = _int(record.get("tools_used"), 0) + 1
                failed = "×" if _failure_hint(item.get("content")) else "✓"
                _live_tool(rt, rid, f"⚙ {call.get('name')} {failed}")
                if io.cancelled() or llm.cancelled():
                    return ("interrupted", "")
        finally:
            # 中断/异常时补齐未执行调用的 tool 结果，保持与 assistant.tool_calls 配对，
            # 否则落盘的消息列表在 resume 后会被 API 以配对不全拒绝
            for call in norm_calls:
                if call not in processed:
                    messages.append(_normalize_tool_msg(call, "工具执行被中断，未获得结果"))
        # 空转计数：本轮结果喂入计数器，重复才计，整轮全新清零
        spin.feed(round_results)
        # 每轮落盘：崩溃/中断时记录可恢复
        _save_record(directory, record)
    # 不可达出口：轮次终止统一在循环内返回


def _failure_hint(content: Any) -> bool:
    from core.tools import tool_failure_hint

    return bool(tool_failure_hint(str(content or "")))


# ---------------------------------------------------------------------------
# task / wait / cancel / query 工具


def _tool_task(
    role: str = "",
    task: str = "",
    permission: str = "",  # 旧参数：兼容历史调用，权限只跟随主代理模式，已忽略
    context: str = "",
    resume_id: str = "",
    context_id: str = "",
) -> str:
    rt = _runtime_snapshot()
    if rt is None:
        return "子代理运行时未绑定（只能在回合内派发）"
    llm, config, io, app, session = rt["llm"], rt["config"], rt["io"], rt.get("app"), rt.get("session")
    cfg = subagent_cfg(config)
    task_text = str(task or "").strip()
    if not task_text:
        return "task 不能为空：应为目标、范围、期望产出自足完整的子任务提示词"
    if resume_id and context_id:
        return "resume_id 与 context_id 互斥：恢复运行用 resume_id，派生新子代理用 context_id"

    resume_key = str(resume_id or context_id or "").strip()
    source = None
    if resume_key:
        source = load_record(config, session, resume_key)
        if source is None:
            return f"子代理记录不存在: {resume_key}（可用 /agents 查看本对话的子代理编号）"
        if not (source.get("messages") or []):
            return f"子代理 #{source.get('id')} 记录为空，无法恢复"
        role = str(source.get("role") or "universal")
    else:
        role = str(role or "").strip() or "universal"

    spec = _SPECS.get(role)
    if spec is None:
        return f"角色不存在: {role}。可用角色:\n{roles_listing()}"

    directory = _records_dir(config, session)
    permission_eff = inherited_permission(_current_mode(rt))
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def _build(record_id: str) -> dict:
        """组装记录；编号由 create_record 并发分配，或恢复时沿用源编号"""
        if resume_key:
            messages = copy.deepcopy(source.get("messages") or [])
            messages.append({"role": "user", "content": task_text})
            return {
                "id": record_id,
                "role": role,
                "task": task_text,
                "origin_task": str(source.get("task") or ""),
                "permission": permission_eff,
                "permission_requested": "",
                "model": source.get("model") or spec.get("model") or "",
                "status": "running",
                "rounds": 0,
                "tools_used": 0,
                "artifacts": [],
                "messages": messages,
                "resumed": (_int(source.get("resumed"), 0) + 1) if resume_id else 0,
                "created_at": str(source.get("created_at") or now) if resume_id else now,
            }
        user_text = task_text if not context else f"{task_text}\n\n[主代理补充背景]\n{context}"
        return {
            "id": record_id,
            "role": role,
            "task": task_text,
            "origin_task": "",
            "permission": permission_eff,
            "permission_requested": "",
            "model": str(spec.get("model") or ""),
            "status": "running",
            "rounds": 0,
            "tools_used": 0,
            "artifacts": [],
            "messages": [
                {"role": "system", "content": _role_prompt(spec, task_text, str(context or ""), config)},
                {"role": "user", "content": user_text},
            ],
            "resumed": 0,
            "created_at": now,
        }

    requested_id = str(source.get("id")) if resume_id and source else None
    record, record_path = create_record(directory, _build, record_id=requested_id)
    record_id = str(record.get("id"))
    if source and not resume_id:
        record["origin_task"] = str(source.get("id"))
        record["source_task"] = str(source.get("id"))
        record_path = _save_record(directory, record) or record_path
    task = submit_task(rt, record, cfg, directory, record_path)
    log.info(
        "异步派发子代理 #%s role=%s permission=%s model=%s resume=%s",
        record_id, role, permission_eff, record.get("model") or "-", bool(resume_key),
    )
    return (
        f"[子代理已异步派发] id={record_id} role={role} status=queued\n"
        f"它在后台继续执行：你可以继续别的工作，或 wait_subagent(id=\"{record_id}\", timeout=秒) 等待"
        f"（超时返回进度）；完成后会自动注入完成通知。过程细节用 query_subagent(id=\"{record_id}\")，"
        f"需要中止用 cancel_subagent(id=\"{record_id}\")。"
    )


def _render_round_block(block: List[dict]) -> List[str]:
    lines = []
    for m in block:
        role = m.get("role")
        text = str(m.get("content") or "").strip()
        if role == "assistant":
            if text:
                lines.append(f"  [输出] {text[:800]}{'…' if len(text) > 800 else ''}")
            for c in m.get("tool_calls") or []:
                args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
                try:
                    args_text = json.dumps(args, ensure_ascii=False)
                except (TypeError, ValueError):
                    args_text = str(args)
                args_text = " ".join(args_text.split())[:200]
                lines.append(f"  [调用] {c.get('name')}({args_text})")
        elif role == "tool":
            body = " ".join(text.split())
            lines.append(f"  [{m.get('tool_name') or 'tool'}] {body[:500]}{'…' if len(body) > 500 else ''}")
        elif role == "user":
            lines.append(f"  [指令] {text[:800]}{'…' if len(text) > 800 else ''}")
    return lines


def _split_rounds(messages: List[dict]) -> List[List[dict]]:
    """按轮分组：assistant(含 tool_calls) 开启一轮并归并其后续 tool 结果；末次纯文本输出为终轮"""
    blocks: List[List[dict]] = []
    current: List[dict] = []
    for m in messages or []:
        role = m.get("role")
        if role == "assistant":
            if current:
                blocks.append(current)
            current = [m]
        elif role == "tool" and current:
            current.append(m)
        elif role == "user":
            if current:
                blocks.append(current)
            current = [m]
    if current:
        blocks.append(current)
    return blocks


def _tool_query_subagent(id: str, rounds: int = 0) -> str:
    rt = _runtime()
    if rt is None:
        return "子代理运行时未绑定（只能在回合内查询）"
    config, session = rt["config"], rt.get("session")
    cfg = subagent_cfg(config)
    key = str(id or "").strip()
    if not key:
        return "id 不能为空（形如 \"1\"；可用 /agents 查看本对话的子代理编号）"
    record = load_record(config, session, key)
    if record is None:
        return f"子代理记录不存在: {key}（可用 /agents 查看本对话的子代理编号）"
    task = task_status(config, session, key)
    if task and task.get("status") in ("queued", "running", "cancelling"):
        record = {**record, **{k: v for k, v in task.items() if v is not None}}
        status = record.get("status")
        progress = (
            f"\n当前进度: {record.get('phase') or status} · 第{record.get('rounds', 0)}轮 · "
            f"{record.get('tools_used', 0)}次工具 · 最近工具 {record.get('last_tool') or '-'} "
            f"· 更新 {record.get('updated_at') or '-'}"
        )
    else:
        progress = ""
    n = _int(rounds, 0) or cfg["query_default_rounds"]
    blocks = _split_rounds(record.get("messages") or [])
    picked = blocks[-n:] if n > 0 else blocks
    status = _STATUS_LABELS.get(str(record.get("status")), record.get("status"))
    head = (
        f"[子代理 #{record.get('id')} · {record.get('role')} · {status}"
        f" · 共{record.get('rounds', 0)}轮 · 显示最后{len(picked)}轮]"
        f"\n任务: {str(record.get('task') or '')[:200]}{progress}"
    )
    lines = [head]
    for block in picked:
        lines.append("---")
        lines.extend(_render_round_block(block) or ["  （空轮）"])
    lines.append("---")
    lines.append(f"完整记录: {_record_path(_records_dir(config, session), key)}（可用 read 工具分批读取）")
    return "\n".join(lines)


def _tool_wait_subagent(id: str, timeout: Optional[float] = None) -> str:
    rt = _runtime_snapshot()
    if rt is None:
        return "子代理运行时未绑定"
    task_id = str(id or "").strip()
    if not task_id:
        return "id 不能为空"
    status = task_status(rt["config"], rt.get("session"), task_id)
    if not status:
        return f"子代理记录不存在: {task_id}"
    key = _task_key(str(getattr(rt.get("session"), "project_id", "") or ""), str(getattr(rt.get("session"), "session_id", "") or ""), task_id)
    with _TASKS_LOCK:
        task = _TASKS.get(key)
    if task and status.get("status") not in ("done", "error", "interrupted", "max_rounds"):
        try:
            wait_seconds = None if timeout is None or float(timeout) < 0 else max(0.0, float(timeout))
        except (TypeError, ValueError):
            return "timeout 必须是秒数"
        io = rt.get("io")
        deadline = None if wait_seconds is None else time.monotonic() + wait_seconds
        # 轮询等待：主回合被取消（Esc）时立即返回，子代理在后台继续跑
        while True:
            if task["done"].wait(0.1):
                status = task_status(rt["config"], rt.get("session"), task_id) or status
                return str(status.get("summary") or f"子代理 #{task_id} 已完成，状态={status.get('status')}。")
            if io is not None and io.cancelled():
                return (
                    f"等待被中断（本回合已取消），子代理 #{task_id} 仍在后台运行；"
                    f"稍后可再 wait_subagent 或 query_subagent 跟踪。"
                )
            if deadline is not None and time.monotonic() >= deadline:
                status = task_status(rt["config"], rt.get("session"), task_id) or status
                return _format_task_progress(status, timed_out=True)
    if status.get("status") in ("done", "error", "interrupted", "max_rounds"):
        return str(status.get("summary") or f"子代理 #{task_id} 已结束，状态={status.get('status')}。")
    return _format_task_progress(status, timed_out=True)


def _format_task_progress(status: dict, timed_out: bool = False) -> str:
    prefix = "等待超时，任务仍在运行。" if timed_out else "任务状态。"
    return (
        f"{prefix} id={status.get('id')} status={status.get('status')} "
        f"phase={status.get('phase') or '?'} rounds={status.get('rounds', 0)} "
        f"tools={status.get('tools_used', 0)} last_tool={status.get('last_tool') or '-'} "
        f"updated={status.get('updated_at') or '?'}。"
        f"可稍后再次 wait_subagent 或 query_subagent。"
    )


def _tool_cancel_subagent(id: str) -> str:
    rt = _runtime_snapshot()
    if rt is None:
        return "子代理运行时未绑定"
    task_id = str(id or "").strip()
    if not task_id:
        return "id 不能为空"
    if not cancel_task(rt["config"], rt.get("session"), task_id):
        return f"无法取消子代理 #{task_id}（不存在或已结束）"
    return f"已请求取消子代理 #{task_id}；任务会在当前工具调用结束后收尾。"


def register_tools(config) -> bool:
    """注册 task / query_subagent；subagent.enabled=false 时不注册，模型侧完全不可见"""
    cfg = subagent_cfg(config)
    if not cfg["enabled"]:
        log.info("子代理系统未启用，跳过工具注册")
        return False
    load_specs(config)
    register.register(
        name="task",
        description=(
            "Asynchronously dispatch an independent subtask. Returns a task id immediately; "
            "the subagent continues in the background while you work or dispatch other tasks. "
            "Use wait_subagent(id, timeout) to wait for a result or query_subagent(id) to "
            "inspect progress. Completion will also be injected into the conversation as a "
            "notification. Independent tasks should be emitted together in one response. "
            "The task prompt must be self-contained because the subagent cannot see the "
            "main conversation history"
        ),
        usage="task <role> <task> [context]",
        schema={
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": sorted(_SPECS),
                    "description": "Subagent role, decides its system prompt and model; see /agents",
                },
                "task": {
                    "type": "string",
                    "description": "Subtask prompt stating goal, scope and expected output format; must be self-contained",
                },
                "context": {
                    "type": "string",
                    "description": "Optional background text provided explicitly by the main agent (known paths/conclusions); session context is not auto-injected",
                },
                "resume_id": {
                    "type": "string",
                    "description": "Resume the subagent with this id, continuing with its full context (role is ignored)",
                },
                "context_id": {
                    "type": "string",
                    "description": (
                        "Dispatch a new subagent seeded with the context of the given "
                        "subagent id (role is only a label; system prompt and model come from the source)"
                    ),
                },
            },
            "required": ["task"],
        },
    )(_tool_task)
    register.register(
        name="wait_subagent",
        description=(
            "Wait for an asynchronously running subagent. Returns the final summary if it "
            "finishes, otherwise returns the current status and progress when timeout expires. "
            "Use timeout=0 for a non-blocking status/result check; omitted timeout waits until done"
        ),
        usage="wait_subagent <id> [timeout]",
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Subagent id"},
                "timeout": {"type": "number", "description": "Seconds to wait; 0 checks immediately; omitted waits until completion"},
            },
            "required": ["id"],
        },
    )(_tool_wait_subagent)
    register.register(
        name="cancel_subagent",
        description="Cancel a queued or running asynchronous subagent by id",
        usage="cancel_subagent <id>",
        schema={
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Subagent id to cancel"}},
            "required": ["id"],
        },
    )(_tool_cancel_subagent)
    register.register(
        name="query_subagent",
        description=(
            "Query a subagent's working context in this conversation: returns its "
            "last n rounds of messages (outputs and tool calls). Use when you need "
            "process details rather than just the result summary"
        ),
        usage="query_subagent <id> [rounds]",
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Subagent id, e.g. \"1\""},
                "rounds": {
                    "type": "integer",
                    "description": "Return the last n rounds, default from config (0 = all)",
                },
            },
            "required": ["id"],
        },
    )(_tool_query_subagent)
    log.info("子代理工具已注册（角色: %s）", ", ".join(sorted(_SPECS)))
    return True
