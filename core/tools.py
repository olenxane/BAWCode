#该部分是工具的具体实现，需要补齐常用工具，包含：基础的文件编辑、computer-use相关、终端命令调用、程序调用、计划编写、步骤生成、步骤更新、计划更新
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from core import hooks
from core import memory as memory_mod
from core import register
from core.log import get_logger

log = get_logger("tools")

# 子进程默认超时（秒）
DEFAULT_TIMEOUT = 60


def _console_output_encoding() -> str:
    """控制台输出代码页（中文 Windows 默认 cp936）；子进程输出按它解码"""
    if sys.platform == "win32":
        try:
            import ctypes

            cp = ctypes.windll.kernel32.GetConsoleOutputCP()
            if cp:
                return f"cp{cp}"
        except Exception:
            pass
    return "utf-8"


def _session():
    return memory_mod.get_session()


@register.register(
    name="execute_command",
    description="Execute a command in the terminal",
    usage="execute_command <command>",
    schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command to execute in the terminal",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory, optional",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds, optional",
            },
        },
        "required": ["command"],
    },
)
def execute_command(command: str, cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    """终端命令调用，返回 stdout/stderr"""
    log.info("执行命令: %s（cwd=%s timeout=%ds）", command, cwd or "-", timeout)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=_console_output_encoding(),
            errors="replace",
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            log.warn("命令退出码 %d: %s", result.returncode, command)
            return f"命令退出码 {result.returncode}\n{output}".strip()
        log.debug("命令完成，输出 %d 字符", len(output))
        return output.strip() or "执行完成。"
    except subprocess.TimeoutExpired:
        log.warn("命令超时（>%ds）: %s", timeout, command)
        return f"命令超时（>{timeout}s）"
    except OSError as e:
        log.error("命令执行失败: %s（%s）", e, command)
        return f"执行失败: {e}"


@register.register(
    name="read",
    description="Read the content of specified file",
    usage="read <file_path>",
    schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path of the file to read",
            },
        },
        "required": ["file_path"],
    },
)
def read(file_path: str) -> str:
    """读取指定文件内容"""
    path = Path(file_path)
    log.debug("读取文件: %s", path)
    try:
        text = path.read_text(encoding="utf-8")
        log.debug("读取完成: %d字符", len(text))
        return text
    except FileNotFoundError:
        log.warn("读取失败，文件不存在: %s", path)
        return "找不到文件"
    except UnicodeDecodeError:
        log.debug("非 UTF-8 编码，替换无效字节后读取: %s", path)
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.error("读取失败 %s: %s", path, e)
        return f"读取失败，发生错误: {e}"


@register.register(
    name="write",
    description="Write content to a file",
    usage="write <file_path> <content>",
    schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path of the file to write",
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file",
            },
        },
        "required": ["file_path", "content"],
    },
)
def write(file_path: str, content: str) -> str:
    """写入文件（覆盖）"""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("写入文件: %s（%d字符，覆盖）", path, len(content))
    return "写入成功"


@register.register(
    name="edit_file",
    description="Edit a file by replacing old_str with new_str",
    usage="edit_file <file_path> <old_str> <new_str> [replace_all]",
    schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path of the file to edit",
            },
            "old_str": {
                "type": "string",
                "description": "Exact string to replace",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement string",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences, default false",
            },
        },
        "required": ["file_path", "old_str", "new_str"],
    },
)
def edit_file(file_path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
    """基础文件编辑：按字符串替换"""
    path = Path(file_path)
    log.info("编辑文件: %s（replace_all=%s）", path, replace_all)
    if not path.exists():
        log.warn("编辑失败，文件不存在: %s", path)
        return "找不到文件"
    text = path.read_text(encoding="utf-8")
    count = text.count(old_str)
    if count == 0:
        log.warn("编辑失败，未找到待替换内容: %s", path)
        return "未找到待替换内容"
    if count > 1 and not replace_all:
        log.warn("编辑中止，匹配到 %d 处: %s", count, path)
        return f"匹配到 {count} 处，请设置 replace_all=true 或提供更精确内容"
    new_text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
    path.write_text(new_text, encoding="utf-8")
    log.debug("编辑完成: 替换 %d 处，%d -> %d 字符", count if replace_all else 1, len(text), len(new_text))
    return "编辑成功"


@register.register(
    name="list_directory",
    description="List files under a directory",
    usage="list_directory <path>",
    schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path, default current directory",
            },
        },
        "required": [],
    },
)
def list_directory(path: str = ".") -> str:
    """列出目录内容"""
    target = Path(path)
    if not target.exists():
        return "目录不存在"
    lines = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        suffix = "/" if item.is_dir() else ""
        lines.append(f"{item.name}{suffix}")
    return "\n".join(lines) or "空目录"


@register.register(
    name="run_program",
    description="Run a program/script with arguments",
    usage="run_program <program> [args] [cwd] [timeout]",
    schema={
        "type": "object",
        "properties": {
            "program": {
                "type": "string",
                "description": "Program executable or script path",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments list",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
            },
        },
        "required": ["program"],
    },
)
def run_program(
    program: str,
    args: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """程序调用：独立进程启动并收集输出"""
    cmd = [program] + list(args or [])
    log.info("运行程序: %s（cwd=%s timeout=%ds）", " ".join(cmd), cwd or "-", timeout)
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=_console_output_encoding(),
            errors="replace",
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            log.warn("程序退出码 %d: %s", result.returncode, " ".join(cmd))
        return output.strip() or f"程序退出码 {result.returncode}"
    except subprocess.TimeoutExpired:
        log.warn("程序超时（>%ds）: %s", timeout, " ".join(cmd))
        return f"程序超时（>{timeout}s）"
    except OSError as e:
        log.error("程序启动失败: %s（%s）", e, " ".join(cmd))
        return f"启动失败: {e}"


@register.register(
    name="write_plan",
    description="Write or replace the current task plan",
    usage="write_plan <title> <content> [complexity]",
    schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Plan title"},
            "content": {"type": "string", "description": "Plan body in markdown"},
            "complexity": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Task complexity",
            },
        },
        "required": ["title", "content"],
    },
)
def write_plan(title: str, content: str, complexity: str = "medium", external_handler=None) -> str:
    """计划编写；用户参与型，预留外部 API 接口"""
    session = _session()
    if session is None:
        log.warn("write_plan 失败：记忆会话未初始化")
        return "记忆会话未初始化"
    plan = session.set_plan(title, content, complexity=complexity)
    hooks.call_user_participating(
        "plan_generate",
        {"plan": plan, "mode": "write"},
        handler=external_handler,
    )
    return json.dumps(plan, ensure_ascii=False)


@register.register(
    name="update_plan",
    description="Update fields of the current task plan",
    usage="update_plan [title] [content] [complexity] [status]",
    schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "New plan title"},
            "content": {"type": "string", "description": "New plan body"},
            "complexity": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "New complexity",
            },
            "status": {
                "type": "string",
                "enum": ["draft", "confirmed", "executing", "done"],
                "description": "Plan status",
            },
        },
        "required": [],
    },
)
def update_plan(
    title: Optional[str] = None,
    content: Optional[str] = None,
    complexity: Optional[str] = None,
    status: Optional[str] = None,
    external_handler=None,
) -> str:
    """计划更新；用户参与型，预留外部 API 接口"""
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    if title is not None:
        session.plan["title"] = title
    if content is not None:
        session.plan["content"] = content
    if complexity is not None:
        session.plan["complexity"] = complexity
    if status is not None:
        session.plan["status"] = status
    hooks.call_user_participating(
        "plan_generate",
        {"plan": session.plan, "mode": "update"},
        handler=external_handler,
    )
    return json.dumps(session.plan, ensure_ascii=False)


@register.register(
    name="generate_steps",
    description="Generate execution steps for the current task",
    usage="generate_steps <steps>",
    schema={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered step titles",
            },
        },
        "required": ["steps"],
    },
)
def generate_steps(steps: List[str], external_handler=None) -> str:
    """步骤生成；用户参与型，预留外部 API 接口"""
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    payload = {"steps": steps}
    external_result = hooks.call_user_participating(
        "step_generate",
        payload,
        handler=external_handler,
        default=None,
    )
    if isinstance(external_result, dict) and external_result.get("steps"):
        steps = external_result["steps"]
    session.set_steps(steps)
    if session.plan.get("status") in ("draft", "confirmed"):
        session.update_plan_status("executing")
    return json.dumps(session.steps, ensure_ascii=False)


@register.register(
    name="update_step_status",
    description="Update status of one execution step",
    usage="update_step_status <step_id> <status> [detail]",
    schema={
        "type": "object",
        "properties": {
            "step_id": {"type": "integer", "description": "Step id"},
            "status": {
                "type": "string",
                "enum": ["pending", "running", "done", "failed"],
                "description": "New step status",
            },
            "detail": {"type": "string", "description": "Optional detail"},
        },
        "required": ["step_id", "status"],
    },
)
def update_step_status(step_id: int, status: str, detail: str = "", external_handler=None) -> str:
    """步骤状态更新；用户参与型，预留外部 API 接口"""
    session = _session()
    if session is None:
        log.warn("update_step_status 失败：记忆会话未初始化")
        return "记忆会话未初始化"
    target = session.update_step_status(step_id, status, detail=detail)
    if target is None:
        return f"步骤不存在: {step_id}"
    # memory 内部已触发 step_update；此处支持调用方临时注入外部 handler
    if external_handler is not None:
        hooks.call_user_participating("step_update", {"step": target}, handler=external_handler)
    return json.dumps(target, ensure_ascii=False)


@register.register(
    name="computer_use",
    description="External computer-use interface for GUI operations",
    usage="computer_use <action> [params]",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Action name, e.g. click, type, screenshot",
            },
            "params": {
                "type": "object",
                "description": "Action parameters",
            },
        },
        "required": ["action"],
    },
)
def computer_use(action: str, params: Optional[dict] = None, external_handler=None) -> str:
    """computer-use 相关能力，必须通过外部 API 接入"""
    log.info("computer_use: %s params=%s", action, sorted((params or {}).keys()))
    result = hooks.call_user_participating(
        "computer_use",
        {"action": action, "params": params or {}},
        handler=external_handler,
        default=None,
    )
    if result is None:
        log.warn("computer_use 未配置外部接口: action=%s", action)
        return json.dumps(
            {
                "ok": False,
                "message": "未配置 computer_use 外部接口",
                "hint": "在 config.external_apis.computer_use 填入 URL 或运行时 register_hook",
            },
            ensure_ascii=False,
        )
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


@register.register(
    name="memory_add_fact",
    description="Write a long-term memory fact to Agent.md or project memory",
    usage="memory_add_fact <fact> [scope=agent|project]",
    schema={
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "Fact to remember"},
            "scope": {
                "type": "string",
                "enum": ["agent", "project"],
                "description": "agent=Agent.md global, project=current project memory",
            },
        },
        "required": ["fact"],
    },
)
def memory_add_fact(fact: str, scope: str = "agent") -> str:
    """长期记忆写入工具接口 → Agent.md / 项目记忆 md"""
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    session.add_fact(fact, scope=scope if scope in ("agent", "project") else "agent")
    return f"已写入长期记忆({scope})"


@register.register(
    name="memory_add_project_note",
    description="Write a note into the current project long-term memory",
    usage="memory_add_project_note <note>",
    schema={
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "Project note / convention"},
        },
        "required": ["note"],
    },
)
def memory_add_project_note(note: str) -> str:
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    session.add_project_note(note)
    return "已写入项目记忆"


@register.register(
    name="rag_add",
    description="Add a document into project RAG store",
    usage="rag_add <text> [source]",
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Document text"},
            "source": {"type": "string", "description": "Document source path or name"},
        },
        "required": ["text"],
    },
)
def rag_add(text: str, source: str = "", external_handler=None) -> str:
    """项目 RAG 写入；向量库预留外部接口"""
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    session.rag_add(text, source=source, external_handler=external_handler)
    return "已写入项目 RAG"
