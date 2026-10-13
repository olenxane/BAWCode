# -*- coding: utf-8 -*-
"""提示词文件加载与 ^{var}^ 占位符统一替换"""
from __future__ import annotations

import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.log import get_logger

log = get_logger("prompt_loader")

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_DIR = _ROOT / "core" / "prompts"
PLACEHOLDER_RE = re.compile(r"\^\{([a-zA-Z_][a-zA-Z0-9_]*)\}\^")


class PromptSegment:
    __slots__ = ("name", "path", "content_raw", "content_rendered")

    def __init__(self, name: str, path: Path, content_raw: str, content_rendered: str):
        self.name = name
        self.path = path
        self.content_raw = content_raw
        self.content_rendered = content_rendered

    @property
    def files_label(self) -> str:
        return self.name


def prompt_dir(config=None) -> Path:
    if config is not None:
        rel = ((config.data or {}).get("prompt") or {}).get("dir") or "core/prompts"
        p = Path(rel)
        return p if p.is_absolute() else _ROOT / rel
    return DEFAULT_PROMPT_DIR


def _prompt_files(config, key: str, default: List[str]) -> List[str]:
    """config.prompt.<key> 覆盖缺省文件清单"""
    if config is not None:
        files = ((config.data or {}).get("prompt") or {}).get(key)
        if files:
            return list(files)
    return list(default)


def system_prompt_files(config=None) -> List[str]:
    return _prompt_files(config, "system_files", ["system_prompt.md"])


def plan_prompt_files(config=None) -> List[str]:
    return _prompt_files(config, "plan_files", ["plan.md"])


def compress_prompt_files(config=None) -> List[str]:
    return _prompt_files(config, "compress_files", ["compress.md"])


def memory_paths(config=None) -> Dict[str, Path]:
    root = _ROOT
    if config is not None:
        rel = ((config.data or {}).get("memory") or {}).get("longterm_dir") or "data/memory"
        base = Path(rel)
        base = base if base.is_absolute() else root / rel
    else:
        base = root / "data" / "memory"
    return {
        "agent_md": base / "Agent.md",
        "projects_dir": base / "Projects",
    }


def _shell_hint(config=None) -> str:
    """当前默认执行器说明：与实际执行口径一致，含实际解释器版本

    配置的执行器不可用时会回退到 cmd.exe（core/tools._resolve_shell），这里同步注明，
    避免模型按不可用执行器的语法写命令。"""
    from core import shells

    name = shells.default_name()
    if config is not None:
        value = shells.resolve_name(((config.data or {}).get("tools") or {}).get("default_shell"))
        if shells.is_valid(value):
            name = value
    if not shells.available(name) and shells.available(shells.CMD):
        return f"{shells.NOTES.get(shells.CMD, shells.CMD)}（配置的 {shells.label(name)} 在本机不可用，命令自动回退到 cmd.exe）"
    text = shells.NOTES.get(name, name)
    version = shells.version(name)
    return f"{text}（实际版本 {version}）" if version else text


def build_variable_context(
    config=None,
    session=None,
    workspace: Optional[Path] = None,
    project_identity: Optional[dict] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建 ^{var}^ 可用变量表"""
    from core import register as register_mod

    ws = Path(workspace or Path.cwd()).resolve()
    identity = project_identity or {}
    project_id = identity.get("project_id") or ""
    mem = memory_paths(config)
    agent_md = mem["agent_md"]
    project_md = mem["projects_dir"] / f"{project_id}.md" if project_id else mem["projects_dir"] / "unknown.md"

    git_branch = ""
    try:
        import subprocess

        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
        if r.returncode == 0:
            git_branch = (r.stdout or "").strip()
    except Exception as e:
        # git 不存在/超时等：分支名可缺省，仅留诊断痕迹
        log.debug("git 分支读取失败 %s: %s", ws, e)
        git_branch = ""

    now = datetime.now()
    tools = register_mod.list_tools() if hasattr(register_mod, "list_tools") else []
    tool_names = ",".join(t.get("name", "") for t in tools)

    model_name = getattr(config, "model_name", "") if config is not None else ""
    model_id = getattr(config, "model", "") if config is not None else ""
    provider_id = getattr(config, "provider_id", "") if config is not None else ""
    ctx_win = getattr(config, "context_window", 0) if config is not None else 0
    max_tokens = getattr(config, "max_tokens", 0) if config is not None else 0
    mode = getattr(config, "mode", "auto") if config is not None else "auto"
    theme = getattr(config, "theme", "") if config is not None else ""

    app_version = "0.1.0"
    try:
        import tomllib  # py3.11+

        pyproject = _ROOT / "pyproject.toml"
        if pyproject.exists():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            app_version = str((data.get("project") or {}).get("version") or app_version)
    except Exception:
        pass

    user_codename = ""
    if session is not None:
        facts = (getattr(session, "longterm", None) or {}).get("facts") or []
        for f in facts:
            if "BAW-" in str(f) or "代号" in str(f):
                user_codename = str(f)
                break

    variables: Dict[str, Any] = {
        "project_name": identity.get("workspace_name") or ws.name or "BAWCode",
        "project_id": project_id,
        "workspace_path": str(ws),
        "workspace_name": ws.name,
        "cwd": str(ws),
        "path_sep": os.sep,
        "git_branch": git_branch,
        "git_root": str(ws) if (ws / ".git").exists() else "",
        "os_name": platform.system(),
        "os_release": platform.release(),
        "os_platform": sys.platform,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cpu_arch": platform.machine(),
        "username": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "home_dir": str(Path.home()),
        # 必须与实际执行口径一致：execute_command 按选定执行器（core/shells.py）直接启动
        "shell_hint": _shell_hint(config),
        "model_name": model_name,
        "model_id": model_id,
        "provider_id": provider_id,
        "access_mode": mode,
        "context_window": str(ctx_win or ""),
        "max_tokens": str(max_tokens or ""),
        "tool_names": tool_names,
        "tool_count": str(len(tools)),
        "theme": theme,
        "app_version": app_version,
        "harness_name": "BAWCode",
        "current_date": now.strftime("%Y-%m-%d"),
        "current_time": now.strftime("%H:%M:%S"),
        "iso_datetime": now.isoformat(timespec="seconds"),
        "timestamp": str(int(now.timestamp())),
        "agent_memory_path": str(agent_md),
        "project_memory_path": str(project_md),
        "project_identity_path": str(ws / ".bawcode" / "identity.json"),
        "project_memory_exists": "true" if project_md.exists() else "false",
        "user_codename": user_codename,
        "plan_title": "",
        "plan_complexity": "",
        "task_summary": "",
    }
    if extra:
        variables.update({str(k): "" if v is None else str(v) for k, v in extra.items()})
    return variables


def render_text(text: str, variables: Optional[Dict[str, Any]] = None) -> str:
    """替换 ^{name}^；未知标记保留原文"""
    if not text:
        return ""
    variables = variables or {}

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in variables:
            val = variables[key]
            return "" if val is None else str(val)
        log.warn("提示词未知占位符: ^{%s}^", key)
        return m.group(0)

    return PLACEHOLDER_RE.sub(_sub, text)


def load_prompt_file(path: Path, variables: Optional[Dict[str, Any]] = None) -> Optional[PromptSegment]:
    if not path.exists():
        log.warn("提示词文件不存在: %s", path)
        return None
    # 三级解码：utf-8 → gbk（记事本 ANSI 另存）→ 有损兜底，解码失败不阻塞启动
    raw = None
    for enc in ("utf-8", "gbk"):
        try:
            raw = path.read_text(encoding=enc)
            break
        except (OSError, UnicodeDecodeError) as e:
            last_err = e
    if raw is None:
        log.warn("提示词读取失败 %s: %s", path, last_err)
        return None
    if not raw.strip():
        log.debug("提示词文件为空，跳过: %s", path)
        return None
    return PromptSegment(path.name, path, raw, render_text(raw, variables))


def load_prompt_bundle(
    names: List[str],
    variables: Optional[Dict[str, Any]] = None,
    config=None,
) -> List[PromptSegment]:
    base = prompt_dir(config)
    segs: List[PromptSegment] = []
    for name in names or []:
        path = Path(name)
        if not path.is_absolute():
            path = base / name
        seg = load_prompt_file(path, variables)
        if seg is not None:
            segs.append(seg)
    return segs


def join_segments(segments: List[PromptSegment]) -> str:
    parts = []
    for s in segments:
        parts.append(f"<!-- prompt: {s.name} -->\n{s.content_rendered.strip()}")
    return "\n\n".join(parts)


def get_system_prompt(
    config=None,
    files: Optional[List[str]] = None,
    extra_variables: Optional[Dict[str, Any]] = None,
    **ctx_kwargs,
) -> str:
    """供 llm/main/workflow 使用：渲染系统提示词（多文件拼接）

    files 显式传入时覆盖 config.prompt.system_files（工作流 system_prompt 节点用）。
    """
    variables = build_variable_context(config=config, extra=extra_variables, **ctx_kwargs)
    segs = load_prompt_bundle(files or system_prompt_files(config), variables, config=config)
    if not segs:
        log.warn("系统提示词文件均为空/缺失，使用内置兜底")
        return (
            f"你是 {variables.get('harness_name', 'BAWCode')} 编码 Agent。"
            f"工作区：{variables.get('workspace_path', '')}。"
            "复杂任务先规划再执行。回复使用简洁中文。"
        )
    out = join_segments(segs)
    # cwd 注入：模板未引用 workspace_path，缺它会自造绝对路径把文件写到工作区之外
    ws = str(variables.get("workspace_path") or "")
    if ws:
        out += f"\n\n当前工作目录：{ws}。文件操作的相对路径均基于此目录。"
    return out


def get_plan_prompt(
    task_text: str,
    config=None,
    extra_variables: Optional[Dict[str, Any]] = None,
    **ctx_kwargs,
) -> str:
    variables = build_variable_context(config=config, extra=extra_variables, **ctx_kwargs)
    summary = (task_text or "").strip().replace("\n", " ")
    if len(summary) > 200:
        summary = summary[:200] + "…"
    variables["task_summary"] = summary
    segs = load_prompt_bundle(plan_prompt_files(config), variables, config=config)
    if not segs:
        return (
            f"你是 {variables.get('harness_name', 'BAWCode')} 的任务规划器。\n"
            f"项目：{variables.get('project_name', '')}（id={variables.get('project_id', '')}）\n"
            f"任务：{summary}\n\n"
            "输出 markdown 计划：目标、步骤、风险、验收。不要执行任务。"
        )
    return join_segments(segs)


def get_compress_prompt(
    summary_token_target: int,
    config=None,
    extra_variables: Optional[Dict[str, Any]] = None,
    **ctx_kwargs,
) -> str:
    """供 memory.compress 使用：渲染上下文压缩提示词（程序计算的 token 目标经占位符注入）"""
    variables = build_variable_context(config=config, extra=extra_variables, **ctx_kwargs)
    variables["summary_token_target"] = str(int(summary_token_target))
    segs = load_prompt_bundle(compress_prompt_files(config), variables, config=config)
    if not segs:
        return (
            "请将以下对话历史压缩为结构化摘要，保留：任务目标（引用用户原话）、"
            "所有用户消息原文（逐条）、关键决策与结论、已完成工作、未决问题、"
            f"当前工作与下一步。总长约 {int(summary_token_target)} token 以内，"
            "信息密度优先，省略寒暄。只输出摘要本身。"
        )
    return join_segments(segs)


def skill_prompt_files(config=None) -> List[str]:
    return _prompt_files(config, "skill_files", ["skill.md"])


def get_skill_prompt(
    config=None,
    extra_variables: Optional[Dict[str, Any]] = None,
    **ctx_kwargs,
) -> str:
    """供 skills.summon 使用：渲染技能生成提示词

    该提示词作为末条普通用户消息拼接（不进系统提示词），
    以免改动系统提示词前缀、破坏提示词缓存命中。
    """
    variables = build_variable_context(config=config, extra=extra_variables, **ctx_kwargs)
    segs = load_prompt_bundle(skill_prompt_files(config), variables, config=config)
    if not segs:
        return (
            "请把上面的完整上下文提炼为一份可复用的 SKILL.md：YAML frontmatter 含 name"
            "（英文小写中划线）与 description（一句话用途），正文写按序步骤、命令与验证方式。"
            "只输出文件内容本身。"
        )
    return join_segments(segs)
