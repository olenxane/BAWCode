#该部分是工具的具体实现，需要补齐常用工具，包含：基础的文件编辑、computer-use相关、终端命令调用、程序调用、计划编写、步骤生成、步骤更新、计划更新
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
    description=(
        "Read the content of a text file with line-number prefixes "
        "(supports line-based paging via offset/limit; use the numbers to anchor edit_file)"
    ),
    usage="read <file_path> [offset] [limit]",
    schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path of the file to read",
            },
            "offset": {
                "type": "integer",
                "description": "1-based start line; omit to read from the beginning",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of lines to return; omit to read to the end",
            },
        },
        "required": ["file_path"],
    },
)
def read(file_path: str, offset: int = 0, limit: int = 0) -> str:
    """读取指定文件内容（带行号前缀，供 edit_file 锚定）；offset/limit 可选，按行分页"""
    path = Path(file_path)
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
    text, _enc, _ok = _decode_best_effort(raw)
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return "（空文件）"
    start = max(int(offset or 0), 1) - 1
    if start >= total:
        return f"offset 超出范围：文件共 {total} 行"
    end = start + int(limit) if int(limit or 0) > 0 else total
    page = "\n".join(f"{n:>5}| {t}" for n, t in enumerate(lines[start:end], start=start + 1))
    if start > 0 or end < total:
        return f"[第 {start + 1}-{min(end, total)} 行 / 共 {total} 行]\n{page}"
    return page


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
    description=(
        "Edit a file by replacing old_str with new_str. old_str must be copied "
        "verbatim from the file (exact indentation and whitespace) and be unique "
        "unless replace_all is true; include surrounding lines for context when needed."
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
    if not old_str:
        return "old_str 不能为空"
    try:
        content, is_crlf, enc, err = _load_editable(path)
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
    count = content.count(old_str)
    if count == 0:
        log.warn("编辑失败，未找到待替换内容: %s", path)
        return _edit_miss_feedback(old_str, content)
    if count > 1 and not replace_all:
        spots = []
        start = 0
        while True:
            i = content.find(old_str, start)
            if i < 0:
                break
            spots.append(content.count("\n", 0, i) + 1)
            start = i + len(old_str)
        lines = content.splitlines()
        ctx = []
        for n in spots[:5]:
            window = lines[max(0, n - 2) : min(len(lines), n + 1)]
            ctx.append(f"  L{n}: {' | '.join(x.strip() for x in window)[:200]}")
        log.warn("编辑中止，匹配到 %d 处: %s", count, path)
        return (
            f"匹配到 {count} 处（行号: {'、'.join(str(n) for n in spots)}），"
            "请为 old_str 扩展上下文精确锚定，或设置 replace_all=true\n"
            "各匹配处上下文：\n" + "\n".join(ctx)
        )
    new_content = content.replace(old_str, new_str) if replace_all else content.replace(old_str, new_str, 1)
    note = _changed_note(content, new_content)
    written = _save_editable(path, new_content, is_crlf, enc)
    log.debug("编辑完成: 替换 %d 处，写入 %d 字节", count, written)
    suffix = f"（原编码 {enc} 已保留）" if enc != "utf-8" else ""
    return f"编辑成功：替换 {count} 处{suffix}\n{note}"


def _load_editable(path: Path) -> tuple:
    """读取待编辑文件：解码探测（utf-8→gbk→有损拒改）+ 行尾归一。

    返回 (\n 归一文本, 是否 CRLF 主导, 编码名, 错误消息)；错误消息非 None 时其余值无意义。
    """
    raw = path.read_bytes()
    text, enc, decodable = _decode_best_effort(raw)
    if not decodable:
        return "", False, "utf-8", "文件含非文本字节（UTF-8/GBK 均无法解码），拒绝编辑以免损坏"
    crlf = text.count("\r\n")
    is_crlf = crlf > text.count("\n") - crlf
    content = text.replace("\r\n", "\n") if is_crlf else text
    return content, is_crlf, enc, None


def _save_editable(path: Path, content: str, is_crlf: bool, enc: str) -> int:
    """按原文件行尾风格与编码字节写回，返回写入字节数"""
    if is_crlf:
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    encoded = content.encode(enc)
    path.write_bytes(encoded)
    return len(encoded)


@register.register(
    name="multi_edit",
    description=(
        "Apply multiple find-and-replace edits to one file atomically: "
        "all edits are validated and applied in order, file is written once at the end; "
        "any failure leaves the file untouched"
    ),
    usage="multi_edit <file_path> <edits>",
    schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path of the file to edit",
            },
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_str": {"type": "string", "description": "Exact string to replace"},
                        "new_str": {"type": "string", "description": "Replacement string"},
                        "replace_all": {"type": "boolean", "description": "Replace all occurrences of this item, default false"},
                    },
                    "required": ["old_str", "new_str"],
                },
                "description": "Ordered edit list, applied in array order",
            },
        },
        "required": ["file_path", "edits"],
    },
)
def multi_edit(file_path: str, edits: List[dict]) -> str:
    """批量编辑：按数组顺序校验并应用，全部通过后一次性写回（原子），失败不留任何修改"""
    path = Path(file_path)
    log.info("批量编辑: %s（%d 项）", path, len(edits or []))
    if not path.exists():
        log.warn("编辑失败，文件不存在: %s", path)
        return "找不到文件"
    if not edits or not isinstance(edits, list):
        return "edits 不能为空"
    items = []
    for idx, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            return f"第 {idx} 项须为对象（含 old_str/new_str）"
        old, new = e.get("old_str"), e.get("new_str")
        if not isinstance(old, str) or not isinstance(new, str):
            return f"第 {idx} 项 old_str/new_str 须为字符串"
        if not old:
            return f"第 {idx} 项 old_str 不能为空"
        if old == new:
            return f"第 {idx} 项 old_str 与 new_str 相同"
        items.append((old, new, bool(e.get("replace_all", False))))
    try:
        content, is_crlf, enc, err = _load_editable(path)
    except OSError as e:
        return f"读取失败，发生错误: {e}"
    if err:
        log.warn("编辑中止: %s（%s）", path, err)
        return err
    original = content
    total = 0
    for idx, (old, new, replace_all) in enumerate(items, 1):
        count = content.count(old)
        if count == 0:
            return f"第 {idx} 项编辑失败，未做任何修改:\n{_edit_miss_feedback(old, content)}"
        if count > 1 and not replace_all:
            spots = []
            start = 0
            while True:
                i = content.find(old, start)
                if i < 0:
                    break
                spots.append(content.count("\n", 0, i) + 1)
                start = i + len(old)
            return (
                f"第 {idx} 项匹配到 {count} 处（行号: {'、'.join(str(n) for n in spots)}），"
                "未做任何修改；请为该项 old_str 扩展上下文，或设置其 replace_all=true"
            )
        total += count
        content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    note = _changed_note(original, content)
    written = _save_editable(path, content, is_crlf, enc)
    log.debug("批量编辑完成: %d 项替换 %d 处，写入 %d 字节", len(items), total, written)
    suffix = f"（原编码 {enc} 已保留）" if enc != "utf-8" else ""
    return f"multi_edit 成功：{len(items)} 项编辑，共替换 {total} 处{suffix}\n{note}"


# edit_file 失败反馈/成功回显参数
_SUGGEST_MAX_LINES = 20000
_SNIPPET_CONTEXT = 3
_SNIPPET_MAX_LINES = 30

# 工具结果失败模式（启发式）：供 UI ✅/❌ 展示与工作流轮次续期判定共用
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
    "命令被安全策略拒绝",
    "已拒绝",
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


def _decode_best_effort(raw: bytes) -> tuple:
    """文件解码探测：utf-8 → gbk → 有损兜底；返回 (文本, 编码名, 是否纯文本)"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc, True
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8", False


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
    parts.append("可先 read 该文件核对实际内容再试")
    return "\n".join(parts)


def _changed_note(old_content: str, new_content: str) -> str:
    """成功回显：新内容中首个变更区间，前后各扩 _SNIPPET_CONTEXT 行"""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    i = 0
    while i < min(len(old_lines), len(new_lines)) and old_lines[i] == new_lines[i]:
        i += 1
    j = 0
    while j < min(len(old_lines), len(new_lines)) - i and old_lines[len(old_lines) - 1 - j] == new_lines[len(new_lines) - 1 - j]:
        j += 1
    a, b = i, len(new_lines) - j  # 新内容中变更区间 [a, b)，0 基
    sa, sb = max(0, a - _SNIPPET_CONTEXT), min(len(new_lines), b + _SNIPPET_CONTEXT)
    body_lines = new_lines[sa:sb]
    omitted = ""
    if sb - sa > _SNIPPET_MAX_LINES:
        body_lines = body_lines[:_SNIPPET_MAX_LINES]
        omitted = f"（片段超过 {_SNIPPET_MAX_LINES} 行已截断）"
    body = "\n".join(f"{n:>5}| {t}" for n, t in enumerate(body_lines, start=sa + 1))
    shown_end = sa + len(body_lines)
    return f"变更片段（第 {sa + 1}-{shown_end} 行 / 共 {len(new_lines)} 行）{omitted}:\n{body}"


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


# search/glob 目录遍历统一跳过的目录名（小写比较），rg 与纯 Python 兜底共用
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", ".idea", ".vscode", "dist", "build", "_recycle",
}
# 纯 Python 路径单文件扫描上限（rg 路径不设，自行流式截断）
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
    # rg 快路径（rust 正则不兼容 python 语法时退出码 2 → 落到纯 Python 兜底）
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
    """glob → 正则：** 跨目录（含零层），* 单层，? 单字符；不区分大小写"""
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

    返回 (绝对路径, 行号, 行文本) 列表；None 表示 rg 不可用/正则不兼容，走纯 Python 兜底。
    rg 默认按 UTF-8 解码，GBK 等编码文件会被当作二进制跳过（与 read 工具 utf-8 口径一致）。
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
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    # 退出码 2 = 执行出错（典型为 rust 正则不兼容），交兜底路径处理
    if proc.returncode and proc.returncode not in (0, 1):
        return None
    return matches


def _python_search(
    root: Path,
    rx: "re.Pattern",
    glob: str,
    max_matches: int,
    single_file: bool,
) -> List[tuple]:
    """纯 Python 兜底：os.walk + 逐行扫描，支持 GBK（经 _decode_best_effort）"""
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
    """上下文块渲染用：读全文行；失败返回 None（该文件退化为仅匹配行）"""
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


@register.register(
    name="load_skill",
    description="Load the full SKILL.md guide of a skill listed in [可用技能]; call it when the task matches a listed skill, then follow its instructions",
    usage="load_skill <skill>",
    schema={
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "技能名（见 [可用技能] 清单）"},
        },
        "required": ["skill"],
    },
)
def load_skill(skill: str) -> str:
    """技能正文按需加载（渐进式披露第二层）；超限由 add_tool_result 行内限额统一外置"""
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
