# -*- coding: utf-8 -*-
"""子代理系统：主代理经 task 工具派发独立上下文的子代理，经 query_subagent 查询其工作过程。

一回合内派发：runtime（llm/config/io/app/session）由 main._agent_turn_impl 绑定、回合末解绑；
子代理消息列表为局部变量，不进主 session、不参与压缩，全程轨迹按记录文件落盘：
data/subagents/{project_id}/{session_id}/{id}.json，id 为会话内顺序编号（"1"/"2"/…）。

权限（permission）：manual < auto < full。默认继承主代理（手动模式主代理→auto），
LLM 传值只允许收紧不允许放宽（钳制到继承默认）。子代理内每次工具调用照走
policy.evaluate + 确认桥，不构成权限旁路。

角色（role）：仅决定系统提示词与模型（data/agents/<role>.json，内置 universal 兜底），
不携带工具白名单；工具范围统一剔除 task/query_subagent 与会话状态工具（防污染主会话
计划/记忆），递归天然不存在。
"""
from __future__ import annotations

import copy
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import policy
from core import prompt_loader
from core import register
from core.llm import CANCELLED
from core.log import get_logger
from core.tools import has_recent_progress

log = get_logger("subagent")

_ROOT = Path(__file__).resolve().parent.parent

# 子代理不可用的工具：递归防线（task/query_subagent）+ 会话状态工具（内部用
# memory.get_session() 全局单例，子代理调用会污染主会话计划/记忆）
_SUBAGENT_EXCLUDED_TOOLS = {
    "task",
    "query_subagent",
    "write_plan",
    "update_plan",
    "generate_steps",
    "update_step_status",
    "memory_add_fact",
    "memory_add_project_note",
    "rag_add",
}

# 宽窄序：值越大越宽松；钳制规则=派发值不得比继承默认更宽
_PERMISSION_ORDER = {"manual": 0, "auto": 1, "full": 2}

_STATUS_LABELS = {
    "done": "完成",
    "interrupted": "已中断",
    "max_rounds": "达轮次上限",
    "error": "出错",
}

_UNIVERSAL_PROMPT = (
    "你是 BAWCode 的子代理，独立完成主代理委托的一个边界清晰的任务，无法与用户交互。\n"
    "- 直接执行并输出结果，不要反问\n"
    "- 结论简洁、结构化，附必要依据（文件路径:行号）\n"
    "- 检索无果时明确说明查过什么、没查到什么，不臆测"
)

# 角色表：name -> {description, model, prompt, prompt_file}；load_specs 重建
_SPECS: Dict[str, dict] = {}

# 回合内运行时：main._agent_turn_impl 绑定、_agent_turn finally 解绑
_RUNTIME: Dict[str, Any] = {}


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
    }


def bind_runtime(llm, config, io, app=None, session=None) -> None:
    _RUNTIME.update(llm=llm, config=config, io=io, app=app, session=session)


def unbind_runtime() -> None:
    _RUNTIME.clear()


def _runtime() -> Optional[dict]:
    return _RUNTIME or None


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
    """主代理模式 → 子代理继承权限：full→full，manual/auto→auto（自动放行普通请求）"""
    return "full" if str(mode or "").strip().lower() == "full" else "auto"


def effective_permission(requested: str, inherited: str) -> str:
    """派发值钳制：只允许比继承默认更窄（manual<auto<full），放宽一律降回继承默认"""
    req = str(requested or "").strip().lower()
    if req not in _PERMISSION_ORDER:
        return inherited
    if req == inherited:
        return req
    wider = _PERMISSION_ORDER[req] > _PERMISSION_ORDER[inherited]
    if wider:
        log.info("子代理权限钳制: 请求 %s → 继承 %s", req, inherited)
    return inherited if wider else req


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
    safe = str(record_id).strip() or "0"
    return directory / f"{safe}.json"


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
# 直播槽（app.subagent_stream：单写者=agent 线程，签名失效驱动帧循环重绘）


def _live(rt: dict) -> Optional[dict]:
    app = rt.get("app")
    if app is None:
        return None
    live = getattr(app, "subagent_stream", None)
    if live is None:
        live = {"id": "", "role": "", "phase": "", "text": "", "tools": []}
        app.subagent_stream = live
    return live


def _live_text_delta(rt: dict, piece: str) -> None:
    live = _live(rt)
    if live is not None and piece:
        live["text"] = (live.get("text") or "")[-1200:] + piece


def _live_phase(rt: dict, text: str) -> None:
    live = _live(rt)
    if live is not None:
        live["phase"] = text


def _live_tool(rt: dict, line: str) -> None:
    live = _live(rt)
    if live is not None:
        tools = live.setdefault("tools", [])
        tools.append(line)
        del tools[:-8]  # 只保留最近 8 行工具轨迹


def _live_clear(rt: dict) -> None:
    app = rt.get("app")
    if app is not None:
        app.subagent_stream = None


# ---------------------------------------------------------------------------
# 执行


def _sub_tool_defs() -> List[dict]:
    """共享注册表的视图过滤：剔除递归/会话状态工具，不改动注册表本体"""
    return [d for d in register.get_tool_defs() if d["function"]["name"] not in _SUBAGENT_EXCLUDED_TOOLS]


def _normalize_tool_msg(call: dict, content: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": str(call.get("id") or ""),
        "tool_name": call.get("name"),
        "content": str(content or ""),
        "type": "tool",
    }


def _execute_one(rt: dict, call: dict, permission: str) -> dict:
    """子代理内单个工具调用：description 先提取（确认面板/执行不可见），照走主策略与确认桥"""
    io, llm = rt["io"], rt["llm"]
    name = str(call.get("name") or "")
    args = call.get("arguments")
    if not isinstance(args, dict):
        args = {}
    call = {**call, "id": str(call.get("id") or f"call_{uuid.uuid4().hex[:12]}"), "arguments": args}
    if "description" in args:
        call["description"] = str(args.pop("description") or "")

    if name in _SUBAGENT_EXCLUDED_TOOLS:
        return _normalize_tool_msg(call, "该工具不对此子代理开放")
    if not register.has_tool(name):
        return _normalize_tool_msg(call, f"工具不存在: {name}")

    action, reason = policy.evaluate(name, args, permission)
    if action == policy.ALLOW:
        item = llm.execute_approved_tool(call)
        return item if isinstance(item, dict) and "content" in item else _normalize_tool_msg(call, item)
    if action == policy.CONFIRM:
        if io.tool_confirm is None:
            return _normalize_tool_msg(call, policy.default_reject_message("无确认通道"))
        item = io.tool_confirm(call)
        if isinstance(item, dict) and "content" in item:
            return item
        return _normalize_tool_msg(call, policy.default_reject_message(""))
    return _normalize_tool_msg(call, policy.default_reject_message(reason))


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
            if name in ("write", "edit_file", "multi_edit"):
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
              max_rounds: int, extensions_limit: int, directory: Path) -> tuple:
    """子代理执行循环。返回 (status, final_text)；防空转与主循环同判据（has_recent_progress）"""
    llm, io = rt["llm"], rt["io"]
    extensions = 0
    rounds = 0
    while True:
        if rounds >= max_rounds:
            if extensions < extensions_limit and has_recent_progress(messages):
                extensions += 1
                _live_phase(rt, f"轮次续期 {extensions}/{extensions_limit}")
                log.info("子代理 #%s 轮次续期 %d/%d", record.get("id"), extensions, extensions_limit)
            else:
                return ("max_rounds", "")
        rounds += 1
        record["rounds"] = rounds
        if io.cancelled() or llm.cancelled():
            return ("interrupted", "")
        _live_phase(rt, f"推理 · 第{rounds}轮")
        response = llm.chat(
            messages,
            tools=tool_defs,
            model=str(record.get("model") or "") or None,
            on_delta=lambda kind, piece, _rt=rt: _live_text_delta(_rt, piece) if kind == "content" else None,
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
        messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": str(c.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                        "name": c.get("name"),
                        "arguments": c.get("arguments") if isinstance(c.get("arguments"), dict) else {},
                        "type": "function",
                    }
                    for c in tool_calls
                ],
            }
        )
        _live_phase(rt, f"工具调用中 · 第{rounds}轮")
        for call in tool_calls:
            item = _execute_one(rt, call, str(record.get("permission") or "auto"))
            messages.append(item)
            record["tools_used"] = _int(record.get("tools_used"), 0) + 1
            failed = "❌" if _failure_hint(item.get("content")) else "✅"
            _live_tool(rt, f"⚙ {call.get('name')} {failed}")
            if io.cancelled() or llm.cancelled():
                return ("interrupted", "")
        # 每轮落盘：崩溃/中断时记录可恢复
        _save_record(directory, record)
    # 不可达出口：轮次终止统一在循环内返回


def _failure_hint(content: Any) -> bool:
    from core.tools import tool_failure_hint

    try:
        return bool(tool_failure_hint(str(content or "")))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# task / query_subagent 工具


def _tool_task(
    role: str = "",
    task: str = "",
    permission: str = "",
    context: str = "",
    resume_id: str = "",
    context_id: str = "",
) -> str:
    rt = _runtime()
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
        role = str(source.get("role") or "universal")
    else:
        role = str(role or "").strip() or "universal"

    spec = _SPECS.get(role)
    if spec is None:
        return f"角色不存在: {role}。可用角色:\n{roles_listing()}"

    directory = _records_dir(config, session)
    permission_eff = effective_permission(permission, inherited_permission(_current_mode(rt)))
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    if resume_key:
        messages = copy.deepcopy(source.get("messages") or [])
        if not messages:
            return f"子代理 #{source.get('id')} 记录为空，无法恢复"
        messages.append({"role": "user", "content": task_text})
        origin_task = str(source.get("task") or "")
        if resume_id:
            # 恢复：沿用原编号与记录（含 resumed 计数）
            record_id = str(source.get("id"))
            resumed = _int(source.get("resumed"), 0) + 1
            created_at = str(source.get("created_at") or now)
        else:
            # context_id 派生：新编号新记录，上下文（含源系统提示词）原样承接
            record_id = _next_id(directory)
            resumed = 0
            created_at = now
        record = {
            "id": record_id,
            "role": role,
            "task": task_text,
            "origin_task": origin_task,
            "permission": permission_eff,
            "model": source.get("model") or spec.get("model") or "",
            "status": "running",
            "rounds": 0,
            "tools_used": 0,
            "artifacts": [],
            "messages": messages,
            "resumed": resumed,
            "created_at": created_at,
        }
    else:
        record_id = _next_id(directory)
        user_text = task_text if not context else f"{task_text}\n\n[主代理补充背景]\n{context}"
        messages = [
            {"role": "system", "content": _role_prompt(spec, task_text, str(context or ""), config)},
            {"role": "user", "content": user_text},
        ]
        record = {
            "id": record_id,
            "role": role,
            "task": task_text,
            "origin_task": "",
            "permission": permission_eff,
            "model": str(spec.get("model") or ""),
            "status": "running",
            "rounds": 0,
            "tools_used": 0,
            "artifacts": [],
            "messages": messages,
            "resumed": 0,
            "created_at": now,
        }
    record_path = _save_record(directory, record)
    log.info(
        "派发子代理 #%s role=%s permission=%s model=%s resume=%s",
        record_id, role, permission_eff, record.get("model") or "-", bool(resume_key),
    )

    if app is not None:
        app.subagent_stream = {"id": record_id, "role": role, "phase": "运行中", "text": "", "tools": []}

    wf_cfg = (getattr(config, "data", None) or {}).get("workflow") or {}
    extensions_limit = max(0, _int(wf_cfg.get("max_rounds_extensions"), 5))
    try:
        status, final_text = _run_loop(
            rt, record, messages, _sub_tool_defs(), cfg["max_rounds"], extensions_limit, directory
        )
    except Exception as exc:
        log.error("子代理 #%s 执行异常: %r", record_id, exc)
        status, final_text = "error", f"内部异常: {exc}"
    finally:
        record["status"] = status
        record["artifacts"] = extract_artifacts(messages)
        record_path = _save_record(directory, record) or record_path
        _live_clear(rt)
        if session is not None:
            label = _STATUS_LABELS.get(status, status)
            session.add_message(
                "assistant",
                f"子代理 #{record_id} · {role} · {label}（{record.get('rounds', 0)}轮/"
                f"{record.get('tools_used', 0)}次工具）",
                type="subagent",
                subagent_id=record_id,
                subagent_role=role,  # 不能叫 role：与 add_message 的位置参数 role 撞名
                status=status,
                trace=messages,
            )
    summary = _build_summary(record, final_text, cfg, record_path)
    record["summary"] = summary
    _save_record(directory, record)
    log.info("子代理 #%s 收尾: %s（%d字摘要）", record_id, status, len(summary))
    return summary


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
    n = _int(rounds, 0) or cfg["query_default_rounds"]
    blocks = _split_rounds(record.get("messages") or [])
    picked = blocks[-n:] if n > 0 else blocks
    status = _STATUS_LABELS.get(str(record.get("status")), record.get("status"))
    head = (
        f"[子代理 #{record.get('id')} · {record.get('role')} · {status}"
        f" · 共{record.get('rounds', 0)}轮 · 显示最后{len(picked)}轮]"
        f"\n任务: {str(record.get('task') or '')[:200]}"
    )
    lines = [head]
    for block in picked:
        lines.append("---")
        lines.extend(_render_round_block(block) or ["  （空轮）"])
    lines.append("---")
    lines.append(f"完整记录: {_record_path(_records_dir(config, session), key)}（可用 read 工具分批读取）")
    return "\n".join(lines)


def register_tools(config) -> bool:
    """注册 task / query_subagent；subagent.enabled=false 时不注册（模型侧完全不可见）"""
    cfg = subagent_cfg(config)
    if not cfg["enabled"]:
        log.info("子代理系统未启用，跳过工具注册")
        return False
    load_specs(config)
    register.register(
        name="task",
        description=(
            "派发子代理执行独立子任务：子代理拥有独立上下文与受限工具，只把最终结果"
            "返回本对话。适用于会产生大量中间输出、无需用户交互的调研/审查/独立执行；"
            "task 必须自足完整（子代理看不到主对话历史）。一次派发一个。"
        ),
        usage="task <role> <task> [permission] [context]",
        schema={
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": sorted(_SPECS),
                    "description": "子代理角色（决定系统提示词与模型），见 /agents",
                },
                "task": {
                    "type": "string",
                    "description": "子任务提示词：目标、范围、期望产出形式，自足完整",
                },
                "permission": {
                    "type": "string",
                    "enum": ["", "auto", "manual", "full"],
                    "description": (
                        "工具权限；留空=跟随主代理（手动模式主代理时为 auto）。"
                        "只允许收紧不允许放宽，放宽会被钳制回继承默认"
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "可选：主代理显式补充的背景文本（已知路径/结论），不自动注入会话上下文",
                },
                "resume_id": {
                    "type": "string",
                    "description": "恢复指定编号子代理：沿用其完整上下文继续（此时 role 被忽略）",
                },
                "context_id": {
                    "type": "string",
                    "description": (
                        "以指定编号子代理的上下文为初始上下文派发新编号子代理"
                        "（此时 role 仅作记录标签，系统提示词与模型沿用源）"
                    ),
                },
            },
            "required": ["task"],
        },
    )(_tool_task)
    register.register(
        name="query_subagent",
        description=(
            "查询本对话中某子代理的工作上下文：返回其最后 n 轮消息（输出与工具调用）。"
            "需要子代理过程细节而不仅是结果摘要时使用"
        ),
        usage="query_subagent <id> [rounds]",
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "子代理编号，如 \"1\""},
                "rounds": {
                    "type": "integer",
                    "description": "返回最后 n 轮，默认见配置（0=全部）",
                },
            },
            "required": ["id"],
        },
    )(_tool_query_subagent)
    log.info("子代理工具已注册（角色: %s）", ", ".join(sorted(_SPECS)))
    return True
