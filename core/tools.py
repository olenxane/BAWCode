#工具的具体实现：文件读写与编辑、终端命令与程序调用、内容搜索、网页抓取、计划与步骤、关键词记忆、技能加载、computer-use 外部接口
import base64
import difflib
import glob as glob_lib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Union

from core import hooks
from core import memory as memory_mod
from core import policy
from core import register
from core import snapshot
from core import toolstore
from core.log import get_logger

log = get_logger("tools")

# 子进程默认超时
DEFAULT_TIMEOUT = 320
MAX_TIMEOUT = 3200

# 文件工具输出防护参数
_READ_MAX_LINES = 2000       # read 单次返回行数上限，防大文件拖爆上下文，续读用 offset 分页
_READ_MAX_LINE_CHARS = 2000  # read 单行字符上限，防超长单行炸上下文


def _max_tool_timeout() -> int:
    """超时上限"""
    try:
        session = memory_mod.get_session()
        data = getattr(getattr(session, "config", None), "data", None) or {}
        value = int((data.get("tools") or {}).get("max_timeout") or 0)
        return value if value >= 1 else MAX_TIMEOUT
    except Exception:
        return MAX_TIMEOUT


def _clamp_timeout(value) -> int:
    """超时参数钳制到 [1, 上限]，非法值回退默认"""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(1, min(seconds, _max_tool_timeout()))


def _console_output_encoding() -> str:
    """控制台输出代码页；子进程输出按它解码"""
    if sys.platform == "win32":
        try:
            import ctypes

            cp = ctypes.windll.kernel32.GetConsoleOutputCP()
            if cp:
                return f"cp{cp}"
        except Exception:
            pass
    return "utf-8"


def _terminate_command_tree(proc: subprocess.Popen, command: str) -> None:
    """超时后终止本次命令进程树，并以有界时间回收 shell 进程"""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warn("命令进程树终止失败 pid=%s: %s", proc.pid, exc)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        log.warn("命令 shell 进程未在清理期限内退出 pid=%s: %s", proc.pid, command)
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            log.warn("命令 shell 进程回收超时 pid=%s", proc.pid)
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        log.warn("命令输出管道回收超时 pid=%s", proc.pid)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _session():
    return memory_mod.get_session()


def _covers_trash(path: Path, trash: Path) -> bool:
    """目标包含回收站目录时不可搬移，否则会自我嵌套"""
    try:
        path, trash = path.resolve(), trash.resolve()
    except OSError:
        pass
    if path == trash:
        return True
    try:
        trash.relative_to(path)
        return True
    except ValueError:
        return False


def _trash_result_text(moved, missing, skipped, failed, trash) -> str:
    """删除命令拦截结果：告知模型文件未真正删除，需用户 /clear-trash"""
    lines = [
        "删除命令已被安全策略拦截，文件没有真正删除，已移入项目回收站；"
        "用户需手动执行 /clear-trash 才会彻底删除。原命令未执行。",
        f"项目回收站: {trash}",
    ]
    for src, dest in moved:
        lines.append(f"- 已移入回收站: {src} -> {dest}")
    for path in missing:
        lines.append(f"- 未找到，跳过: {path}")
    for path in skipped:
        lines.append(f"- 跳过，该目标包含回收站目录: {path}")
    for path, err in failed:
        lines.append(f"- 移入回收站失败: {path}: {err}")
    if not moved and not skipped and not failed:
        lines.append("- 未解析出可删除的目标")
    lines.append("后续删除文件请使用 delete_file 工具，不要再用 rm/del 等命令。")
    return "\n".join(lines)


def _join_argv(parts: List[str]) -> str:
    """按 shell 习惯拼接参数：含空白的参数加引号，保住删除目标边界"""
    return " ".join(f'"{p}"' if any(c.isspace() for c in p) else p for p in parts)


def _trash_delete_command(command: str, cwd: Optional[str], tool: str = "execute_command") -> Optional[str]:
    """护栏开启时把删除命令的目标移入回收站；非删除类命令返回 None"""
    session = _session()
    config = getattr(session, "config", None)
    safety_cfg = (getattr(config, "data", None) or {}).get("security") or {}
    if not safety_cfg.get("delete_guard", True):
        return None
    targets = policy.delete_targets(command)
    if targets is None:
        return None
    log.info("删除命令被拦截，改为移入回收站: %s", command)
    base = Path(cwd) if cwd else Path.cwd()
    trash = snapshot.trash_base()
    moved, missing, skipped, failed = [], [], [], []
    seen = set()
    for raw in targets:
        pattern = str(raw).strip()
        if not pattern:
            continue
        literal = base / pattern
        matches = glob_lib.glob(str(literal))
        if not matches and not any(ch in pattern for ch in "*?["):
            matches = [str(literal)]
        for item in matches:
            try:
                path = Path(item).resolve()
            except OSError:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if not path.exists():
                missing.append(path)
                continue
            if _covers_trash(path, trash):
                skipped.append(path)
                continue
            snapshot.capture_before(path, tool=tool)
            try:
                dest = snapshot.move_to_trash(path)
            except (OSError, shutil.Error) as e:
                log.error("删除命令移入回收站失败 %s: %s", path, e)
                failed.append((path, e))
                continue
            moved.append((path, dest))
    return _trash_result_text(moved, missing, skipped, failed, trash)


@register.register(
    name="execute_command",
    description=(
        "Run a shell command through the system shell; stdout and stderr are "
        "merged in the result, and a non-zero exit code is reported with the "
        "output. Use mode='background' for long-running commands. Prefer "
        "dedicated tools (read/write/edit_file/search/...) over shell commands"
    ),
    usage="execute_command <command>",
    schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to execute",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory, optional",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds, foreground mode only",
            },
            "mode": {
                "type": "string",
                "enum": ["foreground", "background"],
                "description": (
                    "foreground = wait for completion and return output directly; "
                    "background = return immediately, output goes to a file on disk "
                    "and the system notifies on completion (timeout is ignored)"
                ),
            },
        },
        "required": ["command"],
    },
)
def execute_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    mode: str = "foreground",
) -> str:
    """终端命令调用：前台阻塞收输出，后台落盘+完成通知"""
    trash_result = _trash_delete_command(command, cwd)
    if trash_result is not None:
        return trash_result
    timeout = _clamp_timeout(timeout)
    if str(mode or "").strip().lower() == "background":
        return _execute_command_background(command, cwd)
    log.info("执行命令: %s（cwd=%s timeout=%ds）", command, cwd or "-", timeout)
    proc = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=_console_output_encoding(),
            errors="replace",
            start_new_session=sys.platform != "win32",
        )
        output, _ = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            log.warn("命令退出码 %d: %s", proc.returncode, command)
            return f"命令退出码 {proc.returncode}\n{output or ''}".strip()
        log.debug("命令完成，输出 %d 字符", len(output or ""))
        return (output or "").strip() or "执行完成。"
    except subprocess.TimeoutExpired:
        if proc is not None:
            _terminate_command_tree(proc, command)
        log.warn("命令超时（>%ds）: %s", timeout, command)
        return f"命令超时（>{timeout}s）"
    except OSError as e:
        if proc is not None and proc.poll() is None:
            _terminate_command_tree(proc, command)
        log.error("命令执行失败: %s（%s）", e, command)
        return f"执行失败: {e}"


# execute_command 后台执行：监视线程在进程退出后经 main 注册的 runner.notify 发起新回合通知

_bg_notifier = None  # Callable[[str], None]：main.py 启动 _AgentRunner 后注册 runner.notify


def set_background_notifier(fn) -> None:
    """注册后台任务完成通知通道"""
    global _bg_notifier
    _bg_notifier = fn


def _bg_output_file() -> Path:
    """后台输出文件：沿用外置存储目录 data/toolcalls/{project}/{session}/，无会话退项目根 data/bgtasks"""
    session = memory_mod.get_session()
    if session is not None:
        d = toolstore.store_dir(session.config, session.project_id, session.session_id)
    else:
        d = Path(__file__).resolve().parent.parent / "data" / "bgtasks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"bg_{time.strftime('%H%M%S')}_{os.getpid()}_{threading.get_ident() % 10000}.log"


def _execute_command_background(command: str, cwd: Optional[str]) -> str:
    out_file = _bg_output_file()
    try:
        fh = open(out_file, "w", encoding=_console_output_encoding(), errors="replace")
    except OSError as e:
        return f"后台启动失败（输出文件不可写）: {e}"
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    except OSError as e:
        fh.close()
        log.error("后台启动失败: %s（%s）", e, command)
        return f"后台启动失败: {e}"
    task_id = out_file.stem
    threading.Thread(
        target=_bg_watch,
        args=(proc, fh, command, str(out_file), task_id, started),
        daemon=True,
        name=f"bawcode-bg-{task_id}",
    ).start()
    log.info("后台任务启动 id=%s pid=%s 输出=%s", task_id, proc.pid, out_file)
    return (
        f"已在后台启动（id={task_id} pid={proc.pid}，cwd={cwd or '当前目录'}）\n"
        f"输出文件: {out_file}\n"
        f"完成后系统会自动通知（届时用 read 工具读取输出文件）；期间也可随时用 read 查看该文件获取当前输出。"
    )


def _bg_watch(proc: subprocess.Popen, fh, command: str, out_file: str, task_id: str, started: float) -> None:
    """后台监视线程：进程退出后关文件句柄并通知，无通道时仅落日志"""
    try:
        code = proc.wait()
    except Exception as exc:
        log.error("后台任务等待异常 id=%s: %r", task_id, exc)
        code = -1
    finally:
        try:
            fh.close()
        except Exception:
            pass
    elapsed = time.monotonic() - started
    note = (
        f"[后台任务完成] id={task_id}\n命令: {command}\n退出码: {code}（耗时 {elapsed:.1f}s）\n"
        f"输出文件: {out_file}\n请用 read 工具读取输出文件查看执行结果。"
    )
    fn = _bg_notifier
    if fn is None:
        log.info("后台任务完成（无通知通道，未投递）: id=%s 退出码=%s", task_id, code)
        return
    try:
        fn(note)
    except Exception as exc:
        log.error("后台任务通知投递失败 id=%s: %r", task_id, exc)


# ask_user：询问用户意见
# 交互经UI请求桥main._agent_turn_impl回合内绑定，回合末解绑

_ask_bridge = None  # Callable[[dict], dict]

# 自动超时秒数：开关型配置，开启即固定 5 分钟
ASK_USER_TIMEOUT = 300


def set_ask_user_bridge(fn) -> None:
    """注册 ask_user 的 UI 交互桥：payload 进，result dict 出"""
    global _ask_bridge
    _ask_bridge = fn


def _ask_user_timeout() -> int:
    """自动超时秒数：开关开启固定 5 分钟，数字配置 >0 视为开，关=0 禁用"""
    try:
        session = memory_mod.get_session()
        data = getattr(getattr(session, "config", None), "data", None) or {}
        raw = (data.get("tools") or {}).get("ask_user_timeout", True)
        if isinstance(raw, str):
            raw = raw.strip().lower() in ("true", "1", "on")
        return ASK_USER_TIMEOUT if raw else 0
    except Exception:
        return ASK_USER_TIMEOUT


@register.register(
    name="ask_user",
    description=(
        "Ask the user a clarifying question with selectable options; use when "
        "requirements are ambiguous or an approach needs confirmation. The user "
        "may also type a free-form answer; on timeout or skip the result says so "
        "and you should proceed with existing information instead of waiting"
    ),
    usage="ask_user <question> <options...>",
    schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask, one sentence",
            },
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short option label"},
                        "description": {"type": "string", "description": "What the option does, its consequence or rationale"},
                    },
                    "required": ["title"],
                },
                "description": "Candidate options, 2-4 recommended",
            },
        },
        "required": ["question", "options"],
    },
)
def ask_user(question: str, options: Optional[List[dict]] = None) -> str:
    """询问用户意见：弹概述+描述选项交互框，末位自由输入，超时自动跳过"""
    if not isinstance(question, str) or not question.strip():
        return "参数错误: question 不能为空"
    opts = []
    for o in options or []:
        if isinstance(o, dict) and str(o.get("title") or "").strip():
            opts.append({"title": str(o["title"]).strip(), "description": str(o.get("description") or "").strip()})
    if not opts:
        return "参数错误: options 至少需要一项（含 title）"
    bridge = _ask_bridge
    if bridge is None:
        return "当前环境无交互通道，无法询问用户；请基于现有信息自行决策。"
    payload = {"question": question.strip(), "options": opts, "timeout": _ask_user_timeout()}
    try:
        result = bridge(payload)
    except Exception as exc:
        log.error("ask_user 交互桥异常: %r", exc)
        return f"询问用户失败: {exc}"
    if not isinstance(result, dict):
        return "用户未作出选择（交互中断），请基于现有信息继续。"
    status = str(result.get("status") or "")
    if status == "timeout":
        secs = int(result.get("timeout") or payload["timeout"] or 0)
        return f"超时：{secs}秒内用户未作出选择，已自动跳过。请基于现有信息继续推进，不要等待。"
    if status == "declined":
        return "用户按 Esc 跳过了本次询问（未作出选择），请基于现有信息继续推进。"
    if status == "cancelled":
        return "询问被取消（回合中断）。"
    answer = str(result.get("answer") or "").strip()
    if not answer:
        return "用户未提供有效选择，请基于现有信息继续。"
    idx = result.get("index")
    if isinstance(idx, int):
        return f"用户选择了: {answer}"
    return f"用户自行输入: {answer}"


@register.register(
    name="read",
    description=(
        "Read a file. Text files come back with line-number prefixes; use the "
        "numbers to anchor edit_file. Long files are paged via offset/limit, and "
        "each read registers the file baseline required before edit_file or "
        "positional write — read a file before editing it. Image files "
        "(png/jpg/jpeg/gif/webp/bmp) are returned as pictures you can see "
        "(requires a vision-capable model)"
    ),
    usage="read <file_path> [offset] [limit]",
    schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path of the file to read (image paths return the picture itself)",
            },
            "offset": {
                "type": "integer",
                "description": "1-based start line; omit to read from the beginning (text files only)",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of lines to return; omit to read to the end (text files only)",
            },
        },
        "required": ["file_path"],
    },
)
def read(file_path: str, offset: int = 0, limit: int = 0) -> Union[str, dict]:
    """读取文件内容，带行号前缀供 edit_file 锚定；offset/limit 可选按行分页；图片按后缀分派回注多模态"""
    path = Path(file_path)
    if path.suffix.lower() in memory_mod.IMAGE_SUFFIXES:
        return _read_image(path)
    log.debug("读取文件: %s offset=%s limit=%s", path, offset or "-", limit or "-")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        log.warn("读取失败，文件不存在: %s", path)
        return "找不到文件"
    except OSError as e:
        log.error("读取失败 %s: %s", path, e)
        return f"读取失败，发生错误: {e}"
    if b"\x00" in raw[:512]:
        log.warn("读取拒绝，二进制文件: %s", path)
        return "二进制文件，read 工具不适用"
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        log.warn("读取拒绝，UTF-32 编码文件: %s", path)
        return "二进制文件，read 工具不适用（检测到 UTF-32 编码标记）"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        log.warn("读取拒绝，UTF-16 编码文件: %s", path)
        return "二进制文件，read 工具不适用（检测到 UTF-16 编码标记）"
    text, _enc, _ok = _decode_best_effort(raw)
    toolstore.ledger_register(path, text, source="read")
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return "（空文件）"
    start = max(int(offset or 0), 1) - 1
    if start >= total:
        return f"offset 超出范围：文件共 {total} 行"
    end = start + int(limit) if int(limit or 0) > 0 else total
    capped = end - start > _READ_MAX_LINES
    if capped:
        end = start + _READ_MAX_LINES
    page_lines = []
    for t in lines[start:end]:
        if len(t) > _READ_MAX_LINE_CHARS:
            t = t[:_READ_MAX_LINE_CHARS] + f"…（本行超 {_READ_MAX_LINE_CHARS} 字符已截断）"
        page_lines.append(t)
    page = "\n".join(f"{n:>5}| {t}" for n, t in enumerate(page_lines, start=start + 1))
    if capped:
        return (
            f"[已截断：共 {total} 行，本次显示第 {start + 1}-{end} 行，"
            f"继续读取用 offset={end + 1}]\n{page}"
        )
    if start > 0 or end < total:
        return f"[第 {start + 1}-{min(end, total)} 行 / 共 {total} 行]\n{page}"
    return page


def _read_image(path: Path) -> Union[str, dict]:
    """read 的图片分支：返回 {"content", "images"}，执行层摘出 images 落为 API 数组形态 content"""
    sess = _session()
    cfg = getattr(sess, "config", None) if sess is not None else None
    if cfg is not None and not cfg.supports_vision:
        return "当前模型不支持视觉输入，图片内容无法查看；请用文本方式获取该文件的相关信息"
    if not path.is_file():
        return f"图片不存在: {path}"
    size = path.stat().st_size
    if size > memory_mod._IMAGE_MAX_BYTES:
        return f"图片超过 {memory_mod._IMAGE_MAX_BYTES // (1024 * 1024)}MB 上限（当前 {size // 1024}KB）：请压缩后重试"
    kb = max(size // 1024, 1)
    log.info("读取图片: %s（%dKB）", path, kb)
    return {"content": f"已加载图片: {path}（{kb}KB），图片内容已随本结果提供", "images": [str(path)]}


@register.register(
    name="write",
    description=(
        "Write content to a file. With optional start_line: positional write that "
        "overwrites line-by-line starting at that line and keeps all other lines "
        "(appends when start_line is past EOF); positional writes require a prior "
        "read of the file, full overwrite does not."
    ),
    usage="write <file_path> <content> [start_line]",
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
            "start_line": {
                "type": "integer",
                "description": (
                    "1-based line number; omit or 0 = overwrite the whole file. "
                    "When set, content overwrites lines from here (same line count as "
                    "content), all other lines are kept, and writing past EOF appends"
                ),
            },
        },
        "required": ["file_path", "content"],
    },
)
def write(file_path: str, content: str, start_line: int = 0) -> str:
    """写入文件；缺省整文件覆盖；start_line≥1 为位置写入，同 edit 门禁，其余行保留，越界追加"""
    path = Path(file_path)
    start_line = int(start_line or 0)
    if start_line < 0:
        return "start_line 须 ≥0（0/缺省=整文件覆盖，≥1=自该行起位置写入）"
    if start_line == 0:
        snapshot.capture_before(path, tool="write")
        if path.exists():
            if path.is_dir():
                return f"写入失败：{path} 是目录，不是文件"
            try:
                _old, is_crlf, enc, err, bom = _load_editable(path)
            except OSError as e:
                return f"读取失败，发生错误: {e}"
            if err:
                log.warn("写入中止: %s（%s）", path, err)
                return (
                    f"拒绝写入：{path} 含非文本字节（可能是二进制或 UTF-16/32 编码），已保持原文件不变；"
                    "如确需替换，请先 delete_file 再用 write 新建（新文件将使用 UTF-8）"
                )
            # 已有文件：保留原编码与行尾风格，与位置写入/edit_file 同口径
            _written, serr = _save_editable_msg(path, content, is_crlf, enc, bom)
            if serr:
                return serr
            toolstore.ledger_register(path, content, source="write")
            log.info("写入文件: %s（%d字符，覆盖，编码 %s）", path, len(content), enc)
            suffix = f"（原编码 {enc} 已保留）" if enc != "utf-8" else ""
            # 返回带路径与体量：区分不同文件的写入结果，防空转误判
            return f"写入成功: {path}（{len(content)} 字符）{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        # 新建文件：UTF-8 + 系统默认行尾风格
        _written, serr = _save_editable_msg(path, content, os.linesep != "\n", "utf-8", False)
        if serr:
            return serr
        toolstore.ledger_register(path, content, source="write")
        log.info("写入文件: %s（%d字符，新建）", path, len(content))
        return f"写入成功: {path}（{len(content)} 字符）"
    # ---- 位置写入，行号口径与 read 的 splitlines 编号一致 ----
    if not content:
        return "位置写入的 content 不能为空（清空文件请省略 start_line 整文件覆盖）"
    if not path.exists():
        return "找不到文件（位置写入要求文件已存在；新建文件请省略 start_line）"
    refusal = toolstore.ledger_check(path)
    if refusal:
        log.warn("位置写入门禁拦截: %s", path)
        return refusal
    try:
        old_content, is_crlf, enc, err, _bom = _load_editable(path)
    except OSError as e:
        return f"读取失败，发生错误: {e}"
    if err:
        log.warn("写入中止: %s（%s）", path, err)
        return err
    lines = old_content.splitlines()
    new_lines = content.splitlines()
    idx = start_line - 1
    if idx > len(lines):
        return f"start_line 超出范围：文件共 {len(lines)} 行；从末尾追加请用 start_line={len(lines) + 1}"
    merged = lines[:idx] + new_lines + lines[idx + len(new_lines):]
    new_content = "\n".join(merged)
    if old_content.endswith("\n") or content.endswith("\n"):
        new_content += "\n"
    snapshot.capture_before(path, tool="write")
    _written, serr = _save_editable_msg(path, new_content, is_crlf, enc, _bom)
    if serr:
        return serr
    toolstore.ledger_register(path, new_content, source="write")
    total = len(new_content.splitlines())
    log.info("位置写入: %s 自第%d行起写入%d行（现%d行）", path, start_line, len(new_lines), total)
    return f"位置写入成功：自第 {start_line} 行起写入 {len(new_lines)} 行（文件现 {total} 行）"


@register.register(
    name="edit_file",
    description=(
        "Edit a file by replacing old_str with new_str. old_str must be copied "
        "verbatim from the file (exact indentation and whitespace) and be unique "
        "unless replace_all is true; include surrounding lines for context when needed. "
        "If no exact match exists, a line-anchored match tolerant of trailing "
        "whitespace is attempted automatically (leading indentation is never "
        "relaxed) and the success message says so; full-width punctuation "
        "variants are only hinted at on failure, never auto-replaced. "
        "For several changes to one file, issue one edit_file call per change in the "
        "same message — calls run in order and each one sees the previous result, so "
        "write every old_str against the state left by the earlier edits. Each call "
        "is atomic on its own and an earlier failure does not roll back the rest"
    ),
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
                "description": (
                    "Exact text from the file to replace; copy verbatim including "
                    "indentation. Must be unique in the file unless replace_all is "
                    "true — extend it with surrounding lines for a unique anchor."
                ),
            },
            "new_str": {
                "type": "string",
                "description": "Replacement text (the edited version of old_str); must differ from old_str",
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
    """基础文件编辑：按字符串替换；失败给行号级定位反馈，成功回显变更片段，写回保留原行尾与编码"""
    path = Path(file_path)
    log.info("编辑文件: %s（replace_all=%s）", path, replace_all)
    if not path.exists():
        log.warn("编辑失败，文件不存在: %s", path)
        return "找不到文件"
    refusal = toolstore.ledger_check(path)
    if refusal:
        log.warn("编辑门禁拦截: %s", path)
        return refusal
    if not old_str:
        return "old_str 不能为空"
    try:
        content, is_crlf, enc, err, bom = _load_editable(path)
    except OSError as e:
        return f"读取失败，发生错误: {e}"
    if err:
        log.warn("编辑中止: %s（%s）", path, err)
        return err
    if old_str == new_str:
        hint = "old_str 与 new_str 相同，未做修改。new_str 应为修改后的目标内容。"
        probe = next((ln.strip() for ln in old_str.splitlines() if ln.strip()), "")
        if probe and probe not in content:
            hint += f"另外 old_str 首行「{probe[:80]}」当前不在文件中，old_str 必须与文件现有内容精确一致。"
        return hint
    kind, new_content, info = _apply_replacement(content, old_str, new_str, replace_all)
    if kind == _REPLACE_MISS:
        log.warn("编辑失败，未找到待替换内容: %s", path)
        return _edit_miss_feedback(old_str, content)
    if kind in (_REPLACE_MULTI, _REPLACE_TOL_MULTI):
        count, spots = info
        prefix = "" if kind == _REPLACE_MULTI else "精确匹配 0 处；按行尾空白容差"
        lines = content.splitlines()
        ctx = []
        for n in spots[:5]:
            window = lines[max(0, n - 2) : min(len(lines), n + 1)]
            ctx.append(f"  L{n}: {' | '.join(x.strip() for x in window)[:200]}")
        log.warn("编辑中止，匹配到 %d 处: %s", count, path)
        return (
            f"{prefix}匹配到 {count} 处（行号: {'、'.join(str(n) for n in spots)}），"
            "请为 old_str 扩展上下文精确锚定，或设置 replace_all=true\n"
            "各匹配处上下文：\n" + "\n".join(ctx)
        )
    count = info
    note = _changed_note(content, new_content)
    snapshot.capture_before(path, tool="edit_file")
    _written, serr = _save_editable_msg(path, new_content, is_crlf, enc, bom)
    if serr:
        return serr
    toolstore.ledger_register(path, new_content, source="edit")
    log.debug("编辑完成: 替换 %d 处，写入 %d 字节", count, _written)
    parts = []
    if kind == _REPLACE_TOL:
        parts.append("经行尾空白容差匹配")
    if enc != "utf-8":
        parts.append(f"原编码 {enc} 已保留")
    suffix = f"（{'，'.join(parts)}）" if parts else ""
    return f"编辑成功：替换 {count} 处{suffix}\n{note}"


def _load_editable(path: Path) -> tuple:
    """读取待编辑文件：解码探测 utf-8→gbk→有损拒改，UTF-16/32 与 NUL 拒改，UTF-8 BOM 剥离，行尾归一。

    返回 (归一文本, 是否 CRLF 主导, 编码名, 错误消息, 是否带 UTF-8 BOM)，错误消息非 None 时其余值无意义。
    BOM 剥离后由 _save_editable 按读取状态还原，模型锚定文件首行时无需输入不可见的 BOM 字符。
    """
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xff\xfe", b"\xfe\xff")):
        return "", False, "utf-8", "文件带 UTF-16/32 编码标记（或非文本字节），拒绝编辑以免损坏", False
    if b"\x00" in raw[:512]:
        # 无 BOM 的二进制：GBK 能把控制字节当文本解码成功，必须 NUL 嗅探兜底
        return "", False, "utf-8", "文件含非文本字节（NUL），拒绝编辑以免损坏", False
    text, enc, decodable = _decode_best_effort(raw)
    if not decodable:
        return "", False, "utf-8", "文件含非文本字节（UTF-8/GBK 均无法解码），拒绝编辑以免损坏", bom
    crlf = text.count("\r\n")
    is_crlf = crlf > text.count("\n") - crlf
    content = text.replace("\r\n", "\n") if is_crlf else text
    return content, is_crlf, enc, None, bom


def _save_editable(path: Path, content: str, is_crlf: bool, enc: str, bom: bool = False) -> int:
    """按原文件行尾风格与编码字节写回，BOM 按读取时状态还原，返回写入字节数"""
    if is_crlf:
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    if bom:
        content = "\ufeff" + content
    encoded = content.encode(enc)
    _atomic_write_bytes(path, encoded)
    return len(encoded)


def _save_editable_msg(path: Path, content: str, is_crlf: bool, enc: str, bom: bool = False) -> tuple:
    """_save_editable 兜底包装：目标编码无法表示新内容时转为可读错误。返回 (written, err_msg)"""
    try:
        return _save_editable(path, content, is_crlf, enc, bom), ""
    except UnicodeEncodeError as e:
        bad = content[e.start : e.start + 1]
        log.warn("写入编码失败: %s（%s 无法表示 %r）", path, enc, bad)
        return 0, (
            f"写入失败：新内容含「{enc}」编码无法表示的字符（{bad!r}），文件未改动。"
            "请改用该编码可表示的字符；或先 delete_file 再用 write 新建（新文件将使用 UTF-8）"
        )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子落盘：同目录临时文件写全后 os.replace，防进程被杀或断电留下截断损坏的半截文件"""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


# _apply_replacement 结果类别
_REPLACE_OK = "ok"                # 精确命中，单处或 replace_all
_REPLACE_TOL = "tol"              # 精确 0 命中，行尾空白容差命中，单处或 replace_all
_REPLACE_MISS = "miss"            # 精确与容差均 0 命中
_REPLACE_MULTI = "multi"          # 精确多处命中且未开 replace_all
_REPLACE_TOL_MULTI = "tol_multi"  # 容差多处命中且未开 replace_all


def _apply_replacement(content: str, old_str: str, new_str: str, replace_all: bool) -> tuple:
    """edit_file 的单项替换核心；只在内存文本上替换，落盘由调用方负责。

    返回 (kind, 新内容或 None, info)，OK 类 info=替换处数，MULTI 类 info=(处数, 起始行号列表)。
    容差命中取磁盘原文行并保留其原行尾空白，行首缩进绝不放宽。"""
    count = content.count(old_str)
    if count == 1:
        return _REPLACE_OK, content.replace(old_str, new_str, 1), 1
    if count > 1:
        if replace_all:
            return _REPLACE_OK, content.replace(old_str, new_str), count
        return _REPLACE_MULTI, None, (count, _match_spots(content, old_str))
    spans = _tolerance_spans(content, old_str)
    if spans:
        if len(spans) == 1 or replace_all:
            return _REPLACE_TOL, _splice_spans(content, spans, new_str), len(spans)
        return _REPLACE_TOL_MULTI, None, (len(spans), [t[0] for t in spans])
    return _REPLACE_MISS, None, None


def _tolerance_spans(content: str, old_str: str) -> list:
    """二级行尾空白容差匹配：old_str 与文件逐行比较，双方 rstrip 后相等即命中。

    返回命中列表 [(起始行号1基, 区间起, 区间止)]，old_str 以换行结尾时区间吞掉该换行，无命中返回 []。
    纯空白 old_str 直接不匹配，防误吞整文件。"""
    if not old_str or not old_str.strip():
        return []
    needle = old_str.split("\n")
    ends_nl = len(needle) > 1 and needle[-1] == ""
    if ends_nl:
        needle = needle[:-1]
    lines = content.split("\n")
    n = len(needle)
    if n == 0 or len(lines) < n:
        return []
    cmp_needle = [s.rstrip() for s in needle]
    starts = [
        i
        for i in range(len(lines) - n + 1)
        if lines[i].rstrip() == cmp_needle[0]
        and all(lines[i + k].rstrip() == cmp_needle[k] for k in range(1, n))
    ]
    if not starts:
        return []
    bounds = [0]
    for ln in lines:
        bounds.append(bounds[-1] + len(ln) + 1)
    spans = []
    for i in starts:
        s = bounds[i]
        e = bounds[i + n] - 1
        if ends_nl and e < len(content) and content[e] == "\n":
            e += 1
        spans.append((i + 1, s, e))
    return spans


def _splice_spans(content: str, spans: list, new_str: str) -> str:
    """把 content 中各 (行号, start, end) 区间依次替换为 new_str；行锚定区间天然不重叠"""
    out, prev = [], 0
    for _line, s, e in spans:
        out.append(content[prev:s])
        out.append(new_str)
        prev = e
    out.append(content[prev:])
    return "".join(out)


def _match_spots(content: str, needle: str) -> list:
    """精确匹配各处起始行号，1 基，多处命中反馈用"""
    spots = []
    start = 0
    while True:
        i = content.find(needle, start)
        if i < 0:
            break
        spots.append(content.count("\n", 0, i) + 1)
        start = i + len(needle)
    return spots


@register.register(
    name="delete_file",
    description=(
        "Move a file or directory to the project trash instead of permanently "
        "deleting it. The item can be restored with /undo when applicable; "
        "/clear-trash permanently removes items from the trash. Use this tool "
        "for file and directory deletion; shell delete commands are redirected "
        "to the project trash while the delete safety guard is enabled."
    ),
    usage="delete_file <file_or_directory_path>",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "File or directory path to move to the project trash, absolute or relative to the current directory"},
        },
        "required": ["file_path"],
    },
)
def delete_file(file_path: str) -> str:
    """将文件或目录移入项目回收站，可 /undo 回滚、/clear-trash 真正清空"""
    path = Path(file_path)
    if not path.exists():
        return f"找不到文件或目录: {file_path}"
    snapshot.capture_before(path, tool="delete_file")
    try:
        dest = snapshot.move_to_trash(path)
    except (OSError, shutil.Error) as e:
        log.error("移入回收站失败 %s: %s", path, e)
        return f"删除失败（移入回收站出错）: {e}"
    log.info("文件或目录已移入回收站: %s -> %s", path, dest)
    return f"已移入回收站: {dest}\n（/undo 可回滚本次删除 · /clear-trash 真正清空回收站）"


# edit_file 失败反馈/成功回显参数
_SUGGEST_MAX_LINES = 20000
_SNIPPET_CONTEXT = 3
_SNIPPET_MAX_LINES = 30
_NOTE_REGIONS = 3             # 成功回显最多展示的变更区间数，超出提示另有 K 处
_NOTE_DIFF_MAX_LINES = 20000  # 超过此行数退回单区间首尾扫描，difflib 全量太慢

# 工具结果失败启发式模式：UI 状态展示与工作流轮次续期判定共用
_TOOL_FAILURE_PATTERNS = (
    "命令退出码 ",
    "工具执行错误",
    "工具参数错误",
    "未找到待替换内容",
    "匹配到 ",  # edit_file 多处匹配中止
    "找不到文件",
    "读取失败",
    "写入失败",
    "拒绝编辑",
    "old_str 不能为空",
    "old_str 与 new_str 相同",
    "删除命令已被安全策略拦截",
    "已拒绝",
    "拒绝写入",
    # MCP 工具的 isError 结果与调用失败统一前缀
    "MCP 工具返回错误",
    "MCP 调用失败",
)


def tool_failure_hint(content: str) -> bool:
    """启发式判定工具结果是否失败；子串匹配可能误判，仅用于展示与续期决策"""
    text = str(content or "")
    for pat in _TOOL_FAILURE_PATTERNS:
        if pat not in text:
            continue
        if pat == "命令退出码 ":
            m = re.search(r"命令退出码\s+(\d+)", text)
            if m and m.group(1) == "0":
                continue  # 退出码 0 视为成功
        return True
    return False


# 变更类工具的成功回显彼此高度同构（批量重构实测相邻相似度 0.85-0.92），只按整串精确
# 判重：模糊判重会把同一轮里的连续编辑误判为空转，在编辑已落盘后终止回合
_EXACT_DEDUP_TOOLS = {"write", "edit_file", "delete_file"}


class SpinGuard:
    """空转计数器：结果重复出现才开始计数，计满才终止回合，主循环与子代理循环共用。

    结果键取工具名+目标，read 以 file_path 为目标，变更类工具取整串内容精确判重，
    其余工具取去空白内容并辅以 ≥0.9 前缀相似度判重，覆盖读变化中文件、结果仅数字微变
    这类空转；本轮含重复则计数累加，全部为新结果则清零；达到 kill_count 即 spun_out() 为真。
    """

    FUZZ_RATIO = 0.9
    FUZZ_PREFIX = 512
    RECENT_CAP = 32

    def __init__(self, kill_count: int = 12):
        self.kill_count = max(1, int(kill_count))
        self.seen: set = set()
        self.recent: dict = {}
        self.count = 0

    def feed(self, results) -> None:
        """喂入本轮全部工具结果消息并更新计数，空轮或无结果为清零"""
        dups = 0
        for m in results or []:
            tool = str(m.get("tool_name") or "")
            target = str(m.get("file_path") or "")
            if target:
                key = f"{tool}|{target}"
                dup = key in self.seen
                if not dup:
                    self.seen.add(key)
            else:
                body = re.sub(r"\s+", "", memory_mod.content_text(m.get("content")))
                key = f"{tool}|{body}"
                if tool in _EXACT_DEDUP_TOOLS:
                    dup = key in self.seen
                    if not dup:
                        self.seen.add(key)
                else:
                    probe = body[: self.FUZZ_PREFIX]
                    recent = self.recent.setdefault(tool, [])
                    dup = key in self.seen or any(
                        difflib.SequenceMatcher(None, probe, old).ratio() >= self.FUZZ_RATIO
                        for old in recent
                    )
                    if not dup:
                        self.seen.add(key)
                        recent.append(probe)
                        del recent[: -self.RECENT_CAP]
            if dup:
                dups += 1
        self.count = self.count + dups if dups else 0

    def spun_out(self) -> bool:
        return self.count >= self.kill_count


def _decode_best_effort(raw: bytes) -> tuple:
    """文件解码探测：utf-8→gbk→有损兜底，返回 (文本, 编码名, 是否纯文本)；BOM 剥离，写回侧由 _save_editable 还原"""
    data = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc), enc, True
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8", False


def _edit_miss_feedback(old_str: str, content: str) -> str:
    """0 匹配时的定位反馈：对 old_str 较长行做全文相似度匹配，给出最接近的现有行原文"""
    parts = ["未找到待替换内容（检查空白、缩进与全半角字符差异）"]
    lines = content.splitlines()
    if len(lines) <= _SUGGEST_MAX_LINES:
        probes = sorted(
            {ln.strip() for ln in old_str.splitlines() if len(ln.strip()) >= 4},
            key=len,
            reverse=True,
        )[:2]
        hits = []
        if probes:
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if not stripped:
                    continue
                ratio = max(difflib.SequenceMatcher(None, p, stripped).ratio() for p in probes)
                if ratio >= 0.7:
                    hits.append((ratio, idx + 1, line.rstrip()))
        hits.sort(key=lambda t: (-t[0], t[1]))
        if hits:
            parts.append("文件中最接近的现有行原文（替换时需保留其精确缩进）：")
            parts.extend(f"  L{n}: {t[:200]}" for _, n, t in hits[:3])
    hint = _unicode_hint(old_str, content)
    if hint:
        parts.append(hint)
    parts.append("可先 read 该文件核对实际内容再试")
    return "\n".join(parts)


# 三级提示只提示不代改的保守规整表：仅同形异码字符，不含全角冒号/逗号等语义敏感字符
_UNICODE_EQUIV = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2007": " ",
    "\u2008": " ", "\u2009": " ", "\u200a": " ", "\u3000": " ",
    "\u200b": "", "\ufeff": "",
})


def _unicode_hint(old_str: str, content: str) -> Optional[str]:
    """0 匹配时的三级提示：全角引号/破折号/特殊空格规整为半角后能否命中，命中只报告位置"""
    idx = content.translate(_UNICODE_EQUIV).find(old_str.translate(_UNICODE_EQUIV))
    if idx < 0:
        return None
    line = content.count("\n", 0, idx) + 1
    return (
        f"提示：把全角引号/破折号/不间断空格等规整为半角后，可在第 {line} 行附近匹配——"
        "请 read 核对该处的实际字符（可能是全角标点或特殊空格）后修正 old_str"
    )


def _changed_note(old_content: str, new_content: str) -> str:
    """成功回显：新内容中的变更区间，最多 _NOTE_REGIONS 个各带前后 _SNIPPET_CONTEXT 行，附增删行统计；
    单区间沿用「变更片段（第 a-b 行 / 共 N 行）」既有格式"""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    def _snippet(sa: int, sb: int, stat: str = "") -> str:
        body_lines = new_lines[sa:sb]
        omitted = ""
        if sb - sa > _SNIPPET_MAX_LINES:
            body_lines = body_lines[:_SNIPPET_MAX_LINES]
            omitted = f"，片段超过 {_SNIPPET_MAX_LINES} 行已截断"
        body = "\n".join(f"{n:>5}| {t}" for n, t in enumerate(body_lines, start=sa + 1))
        return f"（第 {sa + 1}-{sa + len(body_lines)} 行 / 共 {len(new_lines)} 行{stat}{omitted}）:\n{body}"

    if max(len(old_lines), len(new_lines)) <= _NOTE_DIFF_MAX_LINES:
        regions = []
        added = removed = 0
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, a1, a2, b1, b2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            added += b2 - b1
            removed += a2 - a1
            regions.append((b1, b2))
        if regions:
            stat = f"，+{added}/-{removed} 行" if (added or removed) else ""
            if len(regions) == 1:
                a, b = regions[0]
                sa = max(0, a - _SNIPPET_CONTEXT)
                sb = min(len(new_lines), (a if b == a else b) + _SNIPPET_CONTEXT)
                return f"变更片段{_snippet(sa, sb, stat)}"
            parts = [f"变更统计：+{added}/-{removed} 行（共 {len(regions)} 处变更）"]
            for r, (a, b) in enumerate(regions[:_NOTE_REGIONS], 1):
                sa = max(0, a - _SNIPPET_CONTEXT)
                sb = min(len(new_lines), (a if b == a else b) + _SNIPPET_CONTEXT)
                parts.append(f"变更片段{r}{_snippet(sa, sb)}")
            if len(regions) > _NOTE_REGIONS:
                parts.append(f"（另有 {len(regions) - _NOTE_REGIONS} 处变更未展示）")
            return "\n".join(parts)
    # 超大文件：退回首尾扫描取单区间，不含统计
    i = 0
    while i < min(len(old_lines), len(new_lines)) and old_lines[i] == new_lines[i]:
        i += 1
    j = 0
    while j < min(len(old_lines), len(new_lines)) - i and old_lines[len(old_lines) - 1 - j] == new_lines[len(new_lines) - 1 - j]:
        j += 1
    a, b = i, len(new_lines) - j  # 新内容中变更区间 [a, b)，0 基
    sa = max(0, a - _SNIPPET_CONTEXT)
    sb = min(len(new_lines), b + _SNIPPET_CONTEXT)
    return f"变更片段{_snippet(sa, sb)}"


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


_GLOB_MAX_RESULTS = 200
_GLOB_RECENT_SECONDS = 86400


@register.register(
    name="glob",
    description=(
        "Find files by glob pattern across a directory tree (** crosses directories); "
        "recently modified files (last 24h) are listed first, the rest by path"
    ),
    usage="glob <pattern> [path]",
    schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. *.py, src/**/*.ts, **/*.md",
            },
            "path": {
                "type": "string",
                "description": "Directory to search, default current working directory",
            },
        },
        "required": ["pattern"],
    },
)
def glob(pattern: str, path: str = ".") -> str:
    """文件名模式匹配：** 跨目录；24h 内修改的按新→旧置顶，其余按路径；上限 _GLOB_MAX_RESULTS"""
    if not pattern:
        return "pattern 不能为空"
    root = Path(path or ".")
    if not root.exists():
        return f"路径不存在: {root}"
    root_abs = root.resolve()
    matcher = _file_matcher(pattern.replace("\\", "/"))
    entries = []
    for dirpath, dirnames, filenames in os.walk(str(root_abs)):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in _SKIP_DIRS)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, str(root_abs)).replace("\\", "/")
            if matcher(rel, fn):
                entries.append((full, rel))
    if not entries:
        return "未找到匹配文件"

    def _sort_key(entry):
        full, rel = entry
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            mtime = 0.0
        recent = (time.time() - mtime) < _GLOB_RECENT_SECONDS
        return (0 if recent else 1, -mtime if recent else 0.0, rel)

    entries.sort(key=_sort_key)
    total = len(entries)
    lines = [f"找到 {total} 个文件（pattern: {pattern}，近期修改优先）:"]
    lines.extend(f"  {rel}" for _full, rel in entries[:_GLOB_MAX_RESULTS])
    if total > _GLOB_MAX_RESULTS:
        lines.append(f"（仅显示前 {_GLOB_MAX_RESULTS} 个，可收窄 pattern）")
    return "\n".join(lines)


# search/glob 目录遍历统一跳过的目录名，小写比较，rg 与纯 Python 兜底共用
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", ".idea", ".vscode", "dist", "build", "_recycle",
}
# 纯 Python 路径单文件扫描上限，rg 路径不设限自行流式截断
_SEARCH_MAX_FILE_BYTES = 8 * 1024 * 1024
_MATCH_LINE_MAX_CHARS = 200


@register.register(
    name="search",
    description=(
        "Search file contents with a regex across a directory tree. "
        "Returns matches grouped by file as path + line number + line text; "
        "supports filename glob filter and per-match context blocks."
    ),
    usage="search <pattern> [path] [glob] [context] [max_matches] [case_sensitive]",
    schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern (Python re syntax)",
            },
            "path": {
                "type": "string",
                "description": "Directory or single file to search, default current working directory",
            },
            "glob": {
                "type": "string",
                "description": "Filename filter, e.g. *.py or src/**/*.ts, optional",
            },
            "context": {
                "type": "integer",
                "description": "Context lines around each match, 0-5, default 0",
            },
            "max_matches": {
                "type": "integer",
                "description": "Max total matches to return, default 50",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case sensitive matching, default false",
            },
        },
        "required": ["pattern"],
    },
)
def search(
    pattern: str,
    path: str = ".",
    glob: str = "",
    context: int = 0,
    max_matches: int = 50,
    case_sensitive: bool = False,
) -> str:
    """内容搜索：正则匹配文件内容，按文件分组返回 行号:内容，可附上下文块"""
    try:
        rx = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as e:
        return f"正则表达式无效: {e}"
    root = Path(path or ".")
    if not root.exists():
        return f"路径不存在: {root}"
    root_abs = root.resolve()
    single_file = root.is_file()
    max_matches = max(1, int(max_matches or 50))
    context = min(max(int(context or 0), 0), 5)
    # rg 快路径，rust 正则不兼容 python 语法时退出码 2，落到纯 Python 兜底
    rg_bin = shutil.which("rg")
    matches = None
    if rg_bin:
        matches = _rg_search(rg_bin, root_abs, pattern, case_sensitive, glob, max_matches)
        if matches is not None:
            log.debug("search ripgrep: %s（%d 处）", pattern, len(matches))
    if matches is None:
        matches = _python_search(root_abs, rx, glob, max_matches, single_file)
        log.debug("search 纯Python: %s（%d 处）", pattern, len(matches))
    if not matches:
        return "未找到匹配"
    return _render_search(root_abs, matches, context, single_file, len(matches) >= max_matches)


def _glob_regex(pattern: str) -> "re.Pattern":
    """glob → 正则：** 跨目录含零层，* 单层，? 单字符；不区分大小写"""
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i + 1 : i + 2] == "*":
                if pattern[i + 2 : i + 3] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


def _file_matcher(glob: str):
    """glob 过滤器：无路径分隔符按文件名匹配，含分隔符按相对路径匹配"""
    if "/" in glob or "\\" in glob:
        rx = _glob_regex(glob.replace("\\", "/"))
        return lambda rel, name: rx.match(rel) is not None
    rx = _glob_regex(glob)
    return lambda rel, name: rx.match(name) is not None


def _rg_search(
    rg_bin: str,
    root: Path,
    pattern: str,
    case_sensitive: bool,
    glob: str,
    max_matches: int,
) -> Optional[List[tuple]]:
    """ripgrep 快路径：--json 流式解析，达到上限即终止。

    返回 (绝对路径, 行号, 行文本) 列表；None 表示 rg 不可用或正则不兼容，走纯 Python 兜底。
    rg 默认按 UTF-8 解码，GBK 等编码文件会被当作二进制跳过，与 read 工具 utf-8 口径一致。
    """
    cmd = [rg_bin, "--json", "--no-messages", "--no-require-git"]
    if not case_sensitive:
        cmd.append("--ignore-case")
    for d in sorted(_SKIP_DIRS):
        # rg 以绝对路径为搜索根时 glob 对完整路径匹配，须用 **/ 前缀锚任意层级
        cmd += ["--glob", f"!**/{d}/**"]
    if glob:
        cmd += ["--glob", glob]
    cmd += ["--regexp", pattern, str(root)]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    matches = []
    # 达到条数上限主动停读时进程会被 kill，退出码不可作为执行失败依据
    hit_limit = False
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_text = (data.get("path") or {}).get("text") or ""
            text = (data.get("lines") or {}).get("text") or ""
            matches.append((path_text, int(data.get("line_number") or 0), text.rstrip("\r\n")))
            if len(matches) >= max_matches:
                hit_limit = True
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    # 退出码 2 为执行出错，典型是 rust 正则不兼容，交兜底路径处理
    if not hit_limit and proc.returncode and proc.returncode not in (0, 1):
        return None
    return matches


def _python_search(
    root: Path,
    rx: "re.Pattern",
    glob: str,
    max_matches: int,
    single_file: bool,
) -> List[tuple]:
    """纯 Python 兜底：os.walk + 逐行扫描，支持 GBK，经 _decode_best_effort"""
    matcher = _file_matcher(glob) if glob else None
    root_s = str(root)
    if single_file:
        files = [root_s]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(root_s):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in _SKIP_DIRS)
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                if matcher is not None:
                    rel = os.path.relpath(full, root_s).replace("\\", "/")
                    if not matcher(rel, fn):
                        continue
                files.append(full)
    matches = []
    for full in files:
        if len(matches) >= max_matches:
            break
        try:
            if not single_file and os.path.getsize(full) > _SEARCH_MAX_FILE_BYTES:
                continue
            raw = Path(full).read_bytes()
            if b"\x00" in raw[:512]:  # 二进制嗅探，与 rg 行为对齐
                continue
            text, _enc, _ok = _decode_best_effort(raw)
        except OSError:
            continue
        for i, line in enumerate(text.splitlines()):
            if rx.search(line):
                matches.append((full, i + 1, line))
                if len(matches) >= max_matches:
                    break
    return matches


def _render_search(
    root: Path,
    matches: List[tuple],
    context: int,
    single_file: bool,
    truncated: bool,
) -> str:
    """渲染：按文件分组；context=0 逐行 L行号: 内容，context>0 合并相邻区间为上下文块"""
    root_s = str(root)
    rows = []
    for full, line_no, text in matches:
        rel = root.name if single_file else os.path.relpath(full, root_s).replace("\\", "/")
        rows.append((rel, line_no, text, full))
    rows.sort(key=lambda t: (t[0], t[1]))
    by_file = {}
    for rel, n, text, full in rows:
        by_file.setdefault(rel, []).append((n, text, full))
    parts = [f"找到 {len(rows)} 处匹配（{len(by_file)} 个文件）:"]
    for rel, items in by_file.items():
        if context <= 0:
            parts.append(rel)
            for n, text, _full in items:
                stripped = text.strip()
                parts.append(f"  L{n}: {stripped[:_MATCH_LINE_MAX_CHARS]}")
            continue
        match_nums = {n for n, _t, _full in items}
        blocks = []
        a = b = None
        for n in sorted(match_nums):
            if a is None:
                a = b = n
            elif n <= b + context + 1:
                b = max(b, n)
            else:
                blocks.append((a, b))
                a = b = n
        blocks.append((a, b))
        file_lines = _load_lines(items[0][2])
        for ba, bb in blocks:
            ba, bb = max(1, ba - context), bb + context  # 向外扩 context 行
            if file_lines is None:
                parts.append(f"--- {rel} ---")
                for n, text, _full in items:
                    if ba <= n <= bb:
                        parts.append(f"> {n}: {text.strip()[:_MATCH_LINE_MAX_CHARS]}")
                continue
            bb = min(bb, len(file_lines))
            parts.append(f"--- {rel} L{ba}-{bb} ---")
            for n in range(ba, bb + 1):
                prefix = ">" if n in match_nums else " "
                line = file_lines[n - 1] if n - 1 < len(file_lines) else ""
                parts.append(f"{prefix} {n}: {line.strip()[:_MATCH_LINE_MAX_CHARS]}")
    if truncated:
        parts.append(f"（已达 max_matches 上限，结果可能不完整；可收窄 pattern/glob 或调大 max_matches）")
    return "\n".join(parts)


def _load_lines(full: str) -> Optional[List[str]]:
    """上下文块渲染用：读全文行，失败返回 None，该文件退化为仅匹配行"""
    try:
        text, _enc, _ok = _decode_best_effort(Path(full).read_bytes())
        return text.splitlines()
    except OSError:
        return None


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
                "description": "Timeout in seconds, clamped to 1-600",
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
    trash_result = _trash_delete_command(_join_argv([str(program)] + [str(a) for a in (args or [])]), cwd, tool="run_program")
    if trash_result is not None:
        return trash_result
    timeout = _clamp_timeout(timeout)
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
            # 退出码进返回值：与 execute_command 同口径，供失败启发式与模型感知
            return f"命令退出码 {result.returncode}\n{output}".strip()
        return output.strip() or "命令退出码 0"
    except subprocess.TimeoutExpired:
        log.warn("程序超时（>%ds）: %s", timeout, " ".join(cmd))
        return f"程序超时（>{timeout}s）"
    except OSError as e:
        log.error("程序启动失败: %s（%s）", e, " ".join(cmd))
        return f"启动失败: {e}"


# ----- webfetch：以真实浏览器头抓取页面，剥离脚本/导航/广告等噪音，
# 块级结构转 markdown 文本返回；bs4+lxml 缺失时优雅降级为依赖提示 -----

_WEBFETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}
_WEBFETCH_MAX_BYTES = 5 * 1024 * 1024  # 响应体上限，防超大页面
_WEBFETCH_MAX_CHARS = 50000            # 返回文本硬顶，webfetch 无外置兜底，防撑爆上下文
_WEBFETCH_DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "iframe", "object", "embed",
    "link", "meta", "head", "nav", "header", "footer", "aside", "form", "button",
    "select", "option", "input", "textarea", "label", "dialog",
})
_WEBFETCH_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "search"})
# 启发式噪音 class/id 关键词，保守集合，只打明显广告/追踪/装饰，避免误伤正文
_WEBFETCH_NOISE_HINTS = (
    "advert", "sponsor", "promo", "cookie", "consent", "gdpr", "newsletter",
    "subscribe", "breadcrumb", "social-share", "share-bar", "popup", "banner",
)
_WEBFETCH_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_WEBFETCH_INLINE_TAGS = frozenset({
    "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "i",
    "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup",
    "time", "u", "var", "wbr", "font", "big", "strike", "del", "ins",
})
# 正文图片保留：下载落盘并原位标注路径，供多模态模型读取理解
_WEBFETCH_MAX_IMAGES = 20              # 单页保留上限，防图片瀑布页
_WEBFETCH_IMG_MAX_BYTES = 10 * 1024 * 1024
_WEBFETCH_IMG_TIMEOUT = 10             # 单图下载超时
_WEBFETCH_IMG_WORKERS = 8
# 图片生命周期跟随所属会话活跃度，启动时探测会话文件 mtime：
# 会话 1 天无更新或已不存在则整目录回收，活跃会话的单图按 mtime 封顶 30 天
_WEBFETCH_SESSION_IDLE = 86400
_WEBFETCH_IMG_MAX_AGE = 30 * 86400
_WEBFETCH_IMG_SKIP_HINTS = (           # 装饰图 URL 启发式：icon/logo/头像/占位
    "icon", "favicon", "logo", "sprite", "avatar", "emoji", "spacer",
    "pixel", "blank", "badge", "rating",
)
_WEBFETCH_IMG_LAZY_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-srcset")


def _webfetch_deps():
    """webfetch 依赖 requests/bs4/lxml，缺失返回 None 由调用方给依赖提示"""
    try:
        import requests
        from bs4 import BeautifulSoup, NavigableString, Tag
        return requests, BeautifulSoup, NavigableString, Tag
    except ImportError as e:
        log.warn("webfetch 依赖缺失: %s", e)
        return None


def _webfetch_is_noise(node) -> bool:
    """噪音节点：hidden/aria/role 装饰件、内联隐藏或明显广告追踪 class/id"""
    if node.get("aria-hidden") == "true" or node.get("hidden") is not None:
        return True
    if str(node.get("role") or "").lower() in _WEBFETCH_ROLES:
        return True
    style = str(node.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    sig = " ".join([" ".join(node.get("class") or []), str(node.get("id") or "")]).lower()
    return any(h in sig for h in _WEBFETCH_NOISE_HINTS)


def _webfetch_clean(root) -> None:
    """整棵摘除噪音标签与启发式噪音节点，先收集后摘除，避免遍历中改树"""
    from bs4 import Comment
    doomed = [
        node
        for node in root.find_all(True)
        if node.name.lower() in _WEBFETCH_DROP_TAGS or _webfetch_is_noise(node)
    ]
    for node in doomed:
        node.decompose()
    for comment in root.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()


def _webfetch_img_dir() -> Path:
    """图片临时保存目录：外置存储下 webfetch_imgs/，无会话退项目根 data/webfetch_imgs，锚定绝对路径防 cwd 漂移"""
    session = memory_mod.get_session()
    if session is not None:
        d = toolstore.store_dir(session.config, session.project_id, session.session_id) / "webfetch_imgs"
    else:
        d = Path(__file__).resolve().parent.parent / "data" / "webfetch_imgs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def webfetch_gc() -> None:
    """启动时图片回收：遍历各会话的 webfetch_imgs，会话文件 1 天无更新或不存在则整目录释放，
    活跃会话内单图 mtime 超 30 天封顶删除；异常不阻塞启动。"""
    try:
        session = memory_mod.get_session()
        config = getattr(session, "config", None) if session is not None else None
        data = (getattr(config, "data", None) or {}) if config is not None else {}
        ctx_cfg = data.get("context") or {}
        base_raw = ctx_cfg.get("persist_dir") or "data/toolcalls"
        root = Path(base_raw)
        if not root.is_absolute():
            root = Path(__file__).resolve().parent.parent / base_raw
        sessions_root = Path(__file__).resolve().parent.parent / "data" / "sessions"
        if not root.is_dir():
            return
        now = time.time()
        freed = 0
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for sid_dir in project_dir.iterdir():
                imgs_dir = sid_dir / "webfetch_imgs"
                if not imgs_dir.is_dir():
                    continue
                session_file = sessions_root / project_dir.name / f"{sid_dir.name}.json"
                if not session_file.exists() or now - session_file.stat().st_mtime > _WEBFETCH_SESSION_IDLE:
                    shutil.rmtree(imgs_dir, ignore_errors=True)
                    freed += 1
                    log.info("webfetch 回收：会话 %s/%s 已闲置，释放图片目录", project_dir.name, sid_dir.name)
                    continue
                for img in list(imgs_dir.iterdir()):
                    try:
                        if img.is_file() and now - img.stat().st_mtime > _WEBFETCH_IMG_MAX_AGE:
                            img.unlink()
                    except OSError as exc:
                        log.warn("webfetch 图片清理失败 %s: %s", img, exc)
        if freed:
            log.info("webfetch 启动回收完成：%d 个闲置会话图片目录已释放", freed)
    except Exception as exc:
        log.warn("webfetch 图片回收异常（忽略）: %r", exc)


def _webfetch_img_src(img) -> str:
    """取图片地址：src 优先，依次回退常见懒加载属性；srcset 取首个 URL"""
    for attr in _WEBFETCH_IMG_LAZY_ATTRS:
        raw = str(img.get(attr) or "").strip()
        if raw:
            return raw.split(",")[0].strip().split(" ")[0] if attr == "data-srcset" else raw
    return ""


def _webfetch_img_rejected(src: str, img) -> bool:
    """装饰图判定：URL 含 icon/logo/头像等提示词，或声明确尺寸小于 64px"""
    low = src.lower()
    if any(h in low for h in _WEBFETCH_IMG_SKIP_HINTS):
        return True
    for dim in ("width", "height"):
        m = re.search(r"\d+", str(img.get(dim) or ""))
        if m and int(m.group()) < 64:
            return True
    return False


def _webfetch_collect_images(root, Tag) -> list:
    """收集正文根内有效图片：[(img_tag, key, alt, raw_src)]；key=绝对 URL 或 data URI 的 sha1"""
    from urllib.parse import urljoin
    targets, seen = [], set()
    for img in root.find_all("img"):
        raw = _webfetch_img_src(img)
        if not raw or _webfetch_img_rejected(raw, img):
            continue
        if raw.startswith("data:image/"):
            key = "data:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        else:
            key = urljoin(_webfetch_page_url[0], raw) if _webfetch_page_url[0] else raw
        if key in seen:
            continue
        seen.add(key)
        targets.append((img, key, str(img.get("alt") or "").strip(), raw))
    return targets[:_WEBFETCH_MAX_IMAGES]


_webfetch_page_url = [""]  # 当前抓取页面 URL，相对图片地址拼接用


def _webfetch_save_image(key: str, content: bytes, mime: str) -> Path:
    """图片落盘：文件名=sha1 前 12 位+扩展名，扩展名按 mime→URL 后缀→.img 兜底"""
    ext = {
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp",
        "image/x-icon": ".ico", "image/avif": ".avif",
    }.get(mime.split(";")[0].strip().lower())
    if not ext:
        m = re.search(r"\.(\w{2,5})(?:[?#]|$)", key)
        ext = f".{m.group(1).lower()}" if m else ".img"
    path = _webfetch_img_dir() / f"img_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}{ext}"
    if not path.exists():
        path.write_bytes(content)
    return path


def _webfetch_download_images(targets: list) -> dict:
    """并发下载图片 → {key: 原位标注文本}；失败单图标注不阻塞正文"""
    deps = _webfetch_deps()
    if deps is None:
        return {}
    requests = deps[0]
    from urllib.parse import urljoin
    from concurrent.futures import ThreadPoolExecutor

    def fetch_one(item: tuple):
        _, key, _, raw = item
        try:
            if raw.startswith("data:image/"):
                head, _, b64 = raw.partition(",")
                mime = head[5:].split(";")[0] or "image/png"
                content = base64.b64decode(b64, validate=False)
            else:
                headers = dict(_WEBFETCH_HEADERS)
                headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
                headers["Referer"] = _webfetch_page_url[0]
                headers["Sec-Fetch-Dest"] = "image"
                headers["Sec-Fetch-Mode"] = "no-cors"
                headers["Sec-Fetch-Site"] = "cross-site"
                headers["Cache-Control"] = ""
                with requests.get(key, headers=headers, timeout=_WEBFETCH_IMG_TIMEOUT, stream=True) as r:
                    r.raise_for_status()
                    mime = (r.headers.get("Content-Type") or "").lower()
                    buf = []
                    total = 0
                    for chunk in r.iter_content(65536):
                        buf.append(chunk)
                        total += len(chunk)
                        if total > _WEBFETCH_IMG_MAX_BYTES:
                            return key, "超限未保存"
                    content = b"".join(buf)
            path = _webfetch_save_image(key, content, mime)
            return key, f"已保存: {path}"
        except Exception as exc:
            log.debug("webfetch 图片下载失败 %s: %s", key[:120], exc)
            return key, "下载失败"

    with ThreadPoolExecutor(max_workers=_WEBFETCH_IMG_WORKERS) as pool:
        results = dict(pool.map(fetch_one, targets))
    return {key: f"[图: {alt} | {results[key]} | read 可查看]" if alt else f"[图 | {results[key]} | read 可查看]"
            for _, key, alt, _raw in targets}


def _webfetch_inline(node, Tag, img_map: dict) -> str:
    """行内聚合并保留结构：链接 [text](href)、图片原位标注保存路径；块级/列表文本交给 block 层"""
    parts = []
    for child in node.children:
        if isinstance(child, Tag):
            name = child.name.lower()
            if name == "br":
                parts.append("\n")
            elif name in ("ul", "ol"):
                continue  # 嵌套列表由 block 层负责，避免重复渲染
            elif name == "a":
                text = _webfetch_inline(child, Tag, img_map).strip()
                href = str(child.get("href") or "").strip()
                if text and href and not href.startswith(("javascript:", "#")):
                    parts.append(f"[{text}]({href})" if href != text else text)
                else:
                    parts.append(text)
            elif name == "img":
                src = _webfetch_img_src(child)
                key = None
                if src:
                    if src.startswith("data:image/"):
                        key = "data:" + hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]
                    else:
                        from urllib.parse import urljoin
                        key = urljoin(_webfetch_page_url[0], src) if _webfetch_page_url[0] else src
                parts.append(img_map.get(key, "") if key else "")
            elif name in _WEBFETCH_INLINE_TAGS:
                parts.append(_webfetch_inline(child, Tag, img_map))
            else:
                parts.append(_webfetch_inline(child, Tag, img_map))
        else:
            parts.append(str(child))
    return "".join(parts)


def _webfetch_blocks(node, Tag, NavigableString, lines: list, img_map: dict, indent: int = 0) -> None:
    """块级渲染：标题/段落/列表/表格/代码块各自成行，容器递归、行内子聚合成分段"""
    name = (node.name or "").lower()

    def push(text: str) -> None:
        text = re.sub(r"[ \t\r\xa0]+", " ", text).strip()
        if text:
            lines.append(("    " * indent + text) if indent else text)
            lines.append("")

    if name in _WEBFETCH_HEADINGS:
        push("#" * int(name[1]) + " " + _webfetch_inline(node, Tag, img_map))
    elif name == "pre":
        text = node.get_text().strip("\n")
        if text.strip():
            lines.append("```")
            lines.extend(text.splitlines())
            lines.append("```")
            lines.append("")
    elif name in ("ul", "ol"):
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            marker = f"{i}. " if name == "ol" else "- "
            lines.append("    " * indent + marker + re.sub(r"\s+", " ", _webfetch_inline(li, Tag, img_map)).strip())
            for sub in li.find_all(["ul", "ol"], recursive=False):
                _webfetch_blocks(sub, Tag, NavigableString, lines, img_map, indent + 1)
        lines.append("")
    elif name == "table":
        for tr in node.find_all("tr"):
            cells = [re.sub(r"\s+", " ", _webfetch_inline(c, Tag, img_map)).strip() for c in tr.find_all(["td", "th"], recursive=False)]
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    elif name in ("p", "blockquote", "figcaption", "dt", "dd", "summary", "dl"):
        push(_webfetch_inline(node, Tag, img_map))
    else:
        buf = []
        for child in node.children:
            if isinstance(child, Tag):
                cname = child.name.lower()
                if cname == "br":
                    buf.append("\n")
                elif cname in _WEBFETCH_INLINE_TAGS or cname == "a" or cname == "img":
                    buf.append(_webfetch_inline(child, Tag, img_map))
                else:
                    push(" ".join(buf))
                    buf = []
                    _webfetch_blocks(child, Tag, NavigableString, lines, img_map, indent)
            else:
                buf.append(str(child))
        push(" ".join(buf))


def _webfetch_render(soup, page_url: str) -> tuple:
    from bs4 import NavigableString, Tag
    title = str(soup.title.string).strip() if soup.title and soup.title.string else ""  # head 剥离前取
    _webfetch_page_url[0] = page_url
    _webfetch_clean(soup)
    candidates = list(soup.find_all(["main", "article"]))
    candidates += list(soup.find_all(attrs={"role": "main"}))
    candidates += list(soup.find_all(attrs={"itemprop": "articleBody"}))
    root = max(candidates, key=lambda c: len(c.get_text()), default=None) or soup.body or soup
    targets = _webfetch_collect_images(root, Tag)
    img_map = _webfetch_download_images(targets) if targets else {}
    lines: list = []
    _webfetch_blocks(root, Tag, NavigableString, lines, img_map)
    return title, "\n".join(lines)


@register.register(
    name="webfetch",
    description=(
        "Fetch a web page with real-browser headers and return the cleaned main "
        "text as markdown (scripts/nav/ads stripped). Content images are saved "
        "to disk and annotated in place with their paths — use read to "
        "view them. Use this instead of curl/wget in execute_command"
    ),
    usage="webfetch <url> [timeout]",
    schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL, must start with http(s)://",
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds, optional, default 30, clamped to 1-120",
            },
        },
        "required": ["url"],
    },
)
def webfetch(url: str, timeout: int = 30) -> str:
    """网页抓取：真实浏览器行为+噪音剥离+正文提取；正文图片下载落盘并原位标注路径供多模态读取"""
    deps = _webfetch_deps()
    if deps is None:
        return "webfetch 依赖缺失（requests/beautifulsoup4/lxml），请 pip install -r requirements.txt"
    requests, BeautifulSoup, _, Tag = deps

    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return f"URL 需以 http(s):// 开头: {url}"
    try:
        seconds = int(timeout)
    except (TypeError, ValueError):
        seconds = 30
    seconds = max(1, min(seconds, 120))

    log.info("webfetch: %s（timeout=%ds）", url, seconds)
    try:
        with requests.get(
            url,
            headers=_WEBFETCH_HEADERS,
            timeout=seconds,
            allow_redirects=True,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                chunks.append(chunk)
                total += len(chunk)
                if total > _WEBFETCH_MAX_BYTES:
                    log.warn("webfetch 响应超限截断: %s（>%d bytes）", url, _WEBFETCH_MAX_BYTES)
                    break
            content = b"".join(chunks)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            final_url = str(resp.url)
    except requests.exceptions.Timeout:
        return f"抓取超时（>{seconds}s）: {url}"
    except requests.exceptions.SSLError as e:
        return f"SSL 错误: {e}"
    except requests.exceptions.RequestException as e:
        log.warn("webfetch 失败: %s（%s）", url, e)
        return f"抓取失败: {e}"

    if "application/json" in ctype:
        text = content.decode("utf-8", errors="replace")
    elif "html" in ctype or "xml" in ctype or "text" in ctype or not ctype:
        soup = BeautifulSoup(content, "lxml")
        if soup.find("html") is None and b"<html" not in content[:2048].lower():
            # 非 HTML 响应如 text/plain 纯文本，按文本直出
            text = content.decode("utf-8", errors="replace")
        else:
            title, body = _webfetch_render(soup, final_url)
            meta = "\n".join(filter(None, [title and f"# {title}", f"来源: {final_url}"]))
            text = (meta + "\n\n" + body) if meta else body
    else:
        return f"不支持的内容类型: {ctype or '未知'}（仅支持 html/xml/json/text）"

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > _WEBFETCH_MAX_CHARS:
        text = text[:_WEBFETCH_MAX_CHARS] + f"\n\n[内容已截断：原始 {len(text)} 字符，上限 {_WEBFETCH_MAX_CHARS}]"
    log.debug("webfetch 完成: %s（%d 字符）", final_url, len(text))
    return text or "（页面无有效正文）"


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
    """步骤生成，用户参与型预留外部 API；步骤由主 LLM 结构化传入，清洗空行并钳制数量，超限报错交模型自纠"""
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    if isinstance(steps, str):
        steps = steps.splitlines()  # 弱模型可能传单个字符串而非数组：按行容错
    cleaned = [str(s).strip() for s in (steps or []) if str(s).strip()]
    if not cleaned:
        return "步骤列表为空：请提供 3-8 条可执行步骤（动词开头，每步具体可验证）"
    if len(cleaned) > 15:
        return f"步骤过多（{len(cleaned)} 条）：请合并为 3-8 条关键步骤后重新调用"
    payload = {"steps": cleaned}
    external_result = hooks.call_user_participating(
        "step_generate",
        payload,
        handler=external_handler,
        default=None,
    )
    if isinstance(external_result, dict) and external_result.get("steps"):
        steps = external_result["steps"]
    else:
        steps = cleaned
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
    description=(
        "External computer-use interface for native GUI operations such as "
        "screenshot, click and type. Requires an external handler configured "
        "via config.external_apis.computer_use or a runtime hook; without one "
        "the call returns a setup hint"
    ),
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
    """computer-use 能力，必须经外部 API 接入；处理器返回 dict 可携带 images 本地图片路径列表，
    执行层摘出随工具结果回注，多模态约定同 read 的图片分支"""
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
    name="write_memory",
    description="Write a new keyword memory file (global or project scope)",
    usage="write_memory <keyword> <content> <type>",
    schema={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Memory keyword, a short phrase used as the file name"},
            "content": {"type": "string", "description": "Memory body in markdown"},
            "type": {
                "type": "string",
                "enum": ["global", "project"],
                "description": "global = applies to all projects, body injected into every turn's context; project = current project only, only the keyword enters the index and the body must be read via read_memory",
            },
        },
        "required": ["keyword", "content", "type"],
    },
)
def write_memory(keyword: str, content: str, type: str = "project") -> str:
    """新建关键词记忆文件；同名关键词已存在时报错引导用 update_memory"""
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    if type not in ("global", "project"):
        return "参数错误: type 必须为 global 或 project"
    status, key = session.write_memory_file(type, keyword, content)
    if status == "exists":
        return f"写入失败: 关键词「{key}」的记忆已存在，请改用 update_memory 更新内容"
    if status == "invalid":
        return "写入失败: keyword 清洗后为空（含非法字符或过长），请换一个简短关键词"
    if status == "error":
        return "写入失败: 文件系统错误，详见日志"
    scope_name = "全局" if type == "global" else "项目"
    tip = "该记忆正文将随每轮上下文常驻注入。" if type == "global" else "关键词已入索引，正文需 read_memory 按需读取。"
    return f"已写入{scope_name}记忆「{key}」。{tip}"


@register.register(
    name="update_memory",
    description="Update an existing keyword memory (content rewrite, optional rename)",
    usage="update_memory <keyword> <content> <type> [new_keyword]",
    schema={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Keyword of the memory to update"},
            "content": {"type": "string", "description": "New memory body, full replacement"},
            "type": {
                "type": "string",
                "enum": ["global", "project"],
                "description": "Memory scope, global or project",
            },
            "new_keyword": {"type": "string", "description": "Optional new keyword; renaming moves the file"},
        },
        "required": ["keyword", "content", "type"],
    },
)
def update_memory(keyword: str, content: str, type: str = "project", new_keyword: str = "") -> str:
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    if type not in ("global", "project"):
        return "参数错误: type 必须为 global 或 project"
    status, key = session.update_memory_file(type, keyword, content, new_keyword)
    if status == "missing":
        return f"更新失败: 关键词「{key}」的记忆不存在，可先用 write_memory 新建"
    if status == "conflict":
        return f"更新失败: 新关键词「{key}」已存在，请换一个或先删除"
    if status == "invalid":
        return "更新失败: 关键词清洗后为空，请检查 keyword/new_keyword"
    if status == "error":
        return "更新失败: 文件系统错误，详见日志"
    return f"已更新记忆「{key}」内容" if key == keyword else f"已更新并改名: 「{keyword}」->「{key}」"


@register.register(
    name="read_memory",
    description="Read one or more project memory files by keyword (results are kept across turns, not stripped)",
    usage="read_memory <keywords...>",
    schema={
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Project memory keywords to read, see the [项目记忆索引] section; global memory bodies are already injected into context and need no read",
            },
        },
        "required": ["keywords"],
    },
)
def read_memory(keywords: Optional[List[str]] = None) -> str:
    """按关键词读取项目记忆正文；结果入工具白名单，跨回合保留不被剥离"""
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    keys = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not keys:
        return "参数错误: keywords 至少提供一个关键词"
    found, missing = [], []
    for k in keys:
        text = session.read_memory_file("project", k)
        if text is None:
            missing.append(k)
        else:
            key = session.sanitize_keyword(k)
            found.append(f"## {key}\n{text.strip()}")
    parts = []
    if found:
        parts.append("\n\n".join(found))
    if missing:
        parts.append("未找到的关键词: " + "、".join(missing) + "（当前项目记忆索引见 [项目记忆索引]）")
    return "\n\n".join(parts)


@register.register(
    name="delete_memory",
    description="Delete a keyword memory file (global or project scope)",
    usage="delete_memory <keyword> <type>",
    schema={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Keyword of the memory to delete"},
            "type": {
                "type": "string",
                "enum": ["global", "project"],
                "description": "Memory scope, global or project",
            },
        },
        "required": ["keyword", "type"],
    },
)
def delete_memory(keyword: str, type: str = "project") -> str:
    session = _session()
    if session is None:
        return "记忆会话未初始化"
    if type not in ("global", "project"):
        return "参数错误: type 必须为 global 或 project"
    status, key = session.delete_memory_file(type, keyword)
    if status == "missing":
        return f"删除失败: 关键词「{key}」的记忆不存在"
    if status == "invalid":
        return "删除失败: 关键词清洗后为空"
    if status == "error":
        return "删除失败: 文件系统错误，详见日志"
    return f"已删除{('全局' if type == 'global' else '项目')}记忆「{key}」"


@register.register(
    name="load_skill",
    description="Load the full SKILL.md guide of a skill listed in [可用技能]; call it when the task matches a listed skill, then follow its instructions",
    usage="load_skill <skill>",
    schema={
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "Skill name, see the [可用技能] list"},
        },
        "required": ["skill"],
    },
)
def load_skill(skill: str) -> str:
    """技能正文按需加载，渐进式披露第二层；超限由 add_tool_result 行内限额统一外置"""
    from core import skills as skills_mod

    session = _session()
    loader = skills_mod.get_loader(session.config if session is not None else None)
    if loader is None:
        return "技能系统未启用（config.skills.enabled 或缺少 pyyaml）"
    loaded = loader.load(skill)
    if loaded is None:
        names = ", ".join(loader.list_names()) or "（无）"
        return f"技能不存在: {skill}。可用技能: {names}"
    parts = [f"[技能: {loaded.name}] 来源 {loaded.path}", "", loaded.body]
    extras = loaded.scripts + loaded.references
    if extras:
        lines = "\n".join(f"- {p}" for p in extras)
        parts.append(f"\n[配套资源（按需用 read 工具查看；脚本经 run_command 执行）]\n{lines}")
    return "\n".join(parts)
