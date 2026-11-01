# 工具访问策略：四级权限（手动确认 / 自动接收编辑 / 自动模式 / 完全访问）
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from core import register
from core.log import get_logger

log = get_logger("policy")

_ROOT = Path(__file__).resolve().parent.parent

# 模式常量：从逐次确认到完全放行，权限逐级递增
MODE_CONFIRM = "confirm"
MODE_EDIT = "edit"
MODE_AUTO = "auto"
MODE_FULL = "full"
MODE_MANUAL = MODE_CONFIRM  # 兼容既有内部引用
MODES = (MODE_CONFIRM, MODE_EDIT, MODE_AUTO, MODE_FULL)
MODE_LABELS = {
    MODE_CONFIRM: "手动确认",
    MODE_EDIT: "自动接收编辑",
    MODE_AUTO: "自动模式",
    MODE_FULL: "完全访问",
}

# 判定结果
ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"
LLM_APPROVE = "llm_approve"

# 路径类工具：文件系统副作用，通配放行需限定项目边界
PATH_TOOLS = ("write", "edit_file", "delete_file")

# 只读安全工具：所有访问模式自动放行（手动确认也只读放行）
SAFE_TOOLS = {
    "read",
    "search",
    "glob",
    "list_directory",
    "webfetch",
    # RAG：检索/重建只读（索引仅存内存与插件数据目录）；set_rag 只写插件
    # 项目状态文件（.bawcode/plugin-data/rag/state.json），无文件系统副作用
    "rag_search",
    "rag_index",
    "set_rag",
    "load_skill",
    # 计划管理四件套：仅改会话内计划/步骤状态，无文件系统与系统副作用；
    # 参数随轮次变化导致通用指纹"始终允许"失效，逐次确认纯属打扰
    "write_plan",
    "update_plan",
    "generate_steps",
    "update_step_status",
    # 询问用户：纯交互无副作用，问之前还要先确认"能问"纯属打扰
    "ask_user",
    # 关键词记忆四件套：仅读写 data/memory 下 md 文件，无系统副作用
    "write_memory",
    "update_memory",
    "read_memory",
    "delete_memory",
    # 子代理编排：自身无副作用（子代理内每次调用各自过门禁），并发派发依赖放行判定
    "task",
    "wait_subagent",
    "cancel_subagent",
    "query_subagent",
    # 申请切换模式：纯交互无副作用，实际切换仍由用户批准（模式面板裁决）
    "request_mode_switch",
}

# 仅放行明确的只读参数，未识别的选项交由用户确认
SAFE_OPTIONS = {
    "ls": {"-a", "-l", "-h", "-al", "-la", "-lh", "-lah", "-R", "--all", "--long"},
    "dir": {"/a", "/b", "/s", "/w", "/p", "/o", "/n"},
    "cat": {"-n", "-b", "-s", "--number"},
    "type": set(), "where": {"/r", "/q", "/f", "/t"},
    "which": {"-a", "--all"}, "tree": {"/f", "/a", "-a", "-d"},
}


def has_shell_operator(cmd: str) -> bool:
    """双引号外出现管道/链式/重定向/换行即视为复合命令（cmd 下单引号非引用，一并按操作符处理）"""
    in_dq = False
    for ch in cmd:
        if ch == '"':
            in_dq = not in_dq
        elif not in_dq and ch in "|;&<>\n":
            return True
    return False


def readonly_simple_cmd(cmd: str) -> bool:
    """按完整参数白名单判定只读命令，未知选项不自动放行"""
    if any(ch in cmd for ch in "'`$%!^\r") or cmd.count('"') % 2:
        return False
    tokens = re.findall(r'"[^"\n]*"|[^\s"]+', cmd.strip())
    if not tokens:
        return False
    first = tokens[0].lower()
    args = [t[1:-1] if t.startswith('"') else t for t in tokens[1:]]
    if first in ("python", "python3"):
        return args in (["-V"], ["--version"])
    if first in ("pwd", "ver", "version", "env", "set"):
        return not args
    if first == "cd":
        return not args or len(args) == 1 and not args[0].startswith(("-", "/"))
    if first == "echo":
        return not any(arg.startswith("-") for arg in args)
    if first == "pip":
        return len(args) > 1 and args[0] == "show" and all(re.fullmatch(r"[\w.-]+", a) and not a.startswith("-") for a in args[1:])
    if first == "git":
        if not args:
            return False
        command, rest = args[0], args[1:]
        if command == "branch":
            return all(a in {"--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--no-color"} for a in rest)
        if command == "status":
            return all(a in {"--short", "-s", "--branch", "-b", "--porcelain", "--porcelain=v1", "--porcelain=v2", "--untracked-files", "--untracked-files=all", "--untracked-files=no", "--untracked-files=normal"} for a in rest)
        if command == "log":
            index = 0
            while index < len(rest):
                arg = rest[index]
                if arg in ("-n", "--max-count"):
                    index += 1
                    if index >= len(rest) or not rest[index].isdigit():
                        return False
                elif arg not in {"--oneline", "--graph", "--all", "--decorate", "--no-decorate", "--stat", "--name-only", "--name-status", "--no-color"} and not re.fullmatch(r"-\d+|-n\d+|--max-count=\d+", arg):
                    return False
                index += 1
            return True
        return False
    if first in ("head", "tail"):
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("-n", "-c"):
                index += 1
                if index >= len(args) or not args[index].isdigit():
                    return False
            elif arg.startswith("-") and not re.fullmatch(r"-\d+", arg):
                return False
            index += 1
        return True
    if first in SAFE_OPTIONS:
        options = SAFE_OPTIONS[first]
        return all(not a.startswith("-") and not (sys.platform == "win32" and a.startswith("/")) or a in options for a in args)
    return False


def _split_cmd_segments(cmd: str, shell: str = "", keep_redirect: bool = False) -> Optional[List[str]]:
    """按执行器的引号/转义规则在 |;& 与换行处切分复合命令；含重定向时返回 None

    keep_redirect 为真时保留重定向并在操作符前补空格，便于分词时剔除。
    各执行器的转义不同：bash 用 \\、单引号也是引号；cmd 用 ^；PowerShell 用反引号。"""
    from core import shells

    name = shells.resolve_name(shell)
    segs: List[str] = []
    cur: List[str] = []
    in_dq = False
    in_sq = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if name == shells.GIT_BASH and ch == "\\" and not in_sq and i + 1 < len(cmd):
            cur.append(ch)
            cur.append(cmd[i + 1])
            i += 2
            continue
        if name == shells.CMD and ch == "^" and i + 1 < len(cmd):
            cur.append(ch)
            cur.append(cmd[i + 1])
            i += 2
            continue
        if name == shells.POWERSHELL and ch == "`" and in_dq and i + 1 < len(cmd):
            cur.append(ch)
            cur.append(cmd[i + 1])
            i += 2
            continue
        if name == shells.GIT_BASH and ch == "'" and not in_dq:
            in_sq = not in_sq
            cur.append(ch)
        elif ch == '"':
            in_dq = not in_dq
            cur.append(ch)
        elif in_dq or in_sq:
            cur.append(ch)
        elif ch in "<>":
            if not keep_redirect:
                return None
            j = len(cur)
            while j > 0 and cur[j - 1].isdigit():
                j -= 1
            if j == len(cur) or (j > 0 and not cur[j - 1].isspace()):
                cur.append(" ")
            cur.append(ch)
            if i + 1 < len(cmd) and cmd[i + 1] == ch:
                cur.append(ch)
                i += 1
            if i + 1 < len(cmd) and cmd[i + 1] == "&":
                cur.append("&")
                i += 1
        elif ch in "|;&\n":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s for s in segs if s.strip()]


def readonly_compound_cmd(cmd: str, shell: str = "") -> bool:
    """复合命令逐段判定：引号外按 |;& 与换行切分，重定向直接否，每段须为只读简单命令"""
    segs = _split_cmd_segments(cmd, shell)
    if segs is None:
        return False
    return bool(segs) and all(readonly_simple_cmd(s) for s in segs)
#匹配拦截命令
_DANGEROUS_CMD_RE = re.compile(
    r"""(?xi)
    (?:
        (?<![a-z0-9_])rm\s+-[rf]*[rf]|
        (?<![a-z0-9_])rm\s+--recursive|
        (?<![a-z0-9_])del\s+/|
        (?<![a-z0-9_])(?:rd|rmdir)\s+/|
        (?<![a-z0-9_])format\s+[a-z]:|
        (?<![a-z0-9_])mkfs(?![a-z0-9_])|
        (?<![a-z0-9_])(?:shutdown|reboot|halt)(?![a-z0-9_])|
        (?<![a-z0-9_])dd\s+if=|
        (?<![a-z0-9_])chmod\s+.*777|
        (?<![a-z0-9_])reg\s+add|
        (?<![a-z0-9_])schtasks(?![a-z0-9_])|
        (?<![a-z0-9_])powershell.*-enc|
        curl.+\|\s*(?:sh|bash|powershell)|
        wget.+\|\s*(?:sh|bash|powershell)|
        (?<![a-z0-9_])net\s+user|
        (?<![a-z0-9_])taskkill(?![a-z0-9_])
    )
    """
)

# 删除类命令经工具层移入项目回收站；仍按访问模式审批，不因回收站处理而自动授权。
# 只匹配命令首词或分隔符（|;& 与换行，shell 下换行即命令分隔）之后的删除词，
# 避免 "python app.py del" 这类参数误伤
_DELETE_WORDS = ("rmdir", "rimraf", "deltree", "remove-item", "erase", "rm", "rd", "del", "unlink", "ri")
_DELETE_CMD_RE = re.compile(
    r"(?ix)(?:^|[|;&\r\n(@{])\s*(?:\\|(?:sudo|command|builtin|exec|nohup|doas)\s+)?(?:[a-z]:\S+\s+)?(?:"
    + "|".join(_DELETE_WORDS) + r")(?:\s|$|\))"
)
# PowerShell 预筛只决定是否启动 AST 解析，宁宽勿漏，准确判定交给 AST
_PS_DELETE_HINT_RE = re.compile(
    r"(?i)(?<![\w-])(?:remove-item|ri|rm|del|erase|rd|rmdir|rimraf|deltree|unlink|iex|invoke-expression|delete)(?![\w-])"
)
_WRAPPER_WORDS = ("sudo", "command", "builtin", "exec", "nohup", "time", "doas")
_INTERPRETERS = ("python", "python3", "py", "node", "perl", "ruby", "deno", "bun")
_INTERP_DELETE_RE = re.compile(
    r"(?i)(?:shutil\.rmtree|os\.(?:remove|unlink|rmdir|removedirs)|\.unlink\(|\.rmdir\(|"
    r"fs\.(?:rm|rmdir|unlink)(?:sync)?|rm_rf|fileutils\.rm|file\.delete|directory\.delete|::delete)"
)
_REDIRECT_TOKEN_RE = re.compile(r"^\d*(?:>>?|<)&?")
_ANY_DELETE_WORD_RE = re.compile(r"(?i)(?<![\w-])(?:" + "|".join(_DELETE_WORDS) + r")(?![\w-])")

DELETE_UNRELIABLE = "删除命令已被安全策略拦截：目标不是确定的字面路径（含变量、通配符、表达式或命令内切换目录），未执行也未移入回收站。请改用 delete_file 工具逐个删除确定路径。"


def is_delete_command(cmd: str) -> bool:
    """命令是否命中删除类命令；按执行器无关的保守口径先行筛出候选"""
    return bool(_DELETE_CMD_RE.search(str(cmd or "")))


def _strip_redirects(toks: List[str]) -> List[str]:
    """剔除重定向 token；操作符单独成 token 时连同其后的文件名一起剔除"""
    out: List[str] = []
    skip = False
    for tok in toks:
        if skip:
            skip = False
            continue
        m = _REDIRECT_TOKEN_RE.match(tok)
        if m:
            skip = m.end() == len(tok)
            continue
        out.append(tok)
    return out


def _strict_block_reason(word: str, toks: List[str], seg: str) -> str:
    """严格护栏：识别会删除文件但无法转为移入回收站的命令形态，返回拦截原因"""
    low = [t.lower().strip("()") for t in toks]
    rest = low[1:]
    if word == "git" and "clean" in rest and not any(t in ("-n", "--dry-run") for t in rest):
        return "git clean 会删除未跟踪文件"
    if word == "find" and ("-delete" in rest or (
        any(t in ("-exec", "-execdir", "-ok") for t in rest) and any(_strip_exe(t) in _DELETE_WORDS for t in rest)
    )):
        return "find 带删除动作"
    if word == "xargs" and any(_strip_exe(t) in _DELETE_WORDS for t in rest):
        return "xargs 批量删除"
    if word in ("move", "mv") and rest and rest[-1] in ("nul", "/dev/null"):
        return "移动到空设备等同删除"
    if word == "robocopy" and any(t in ("/mir", "/purge") for t in rest):
        return "robocopy /mir 或 /purge 会删除目标目录中的文件"
    if word in _INTERPRETERS and any(t in ("-c", "-e", "--eval") for t in rest) and _INTERP_DELETE_RE.search(seg):
        return "解释器一行命令中包含删除调用"
    return ""


def _nested_command(word: str, toks: List[str]) -> Tuple[str, str]:
    """识别 cmd /c、bash -c、powershell -Command 这类嵌套执行，返回 (内层命令, 内层执行器)"""
    from core import shells

    rest = toks[1:]
    lowered = [t.lower() for t in rest]
    if word == "cmd":
        for i, t in enumerate(lowered):
            if t in ("/c", "/k", "/r"):
                return _join_tokens(rest[i + 1:]), shells.CMD
    if word in ("bash", "sh", "zsh", "wsl"):
        for i, t in enumerate(lowered):
            if t == "-c" and i + 1 < len(rest):
                return rest[i + 1], shells.GIT_BASH
    if word in ("powershell", "pwsh"):
        for i, t in enumerate(lowered):
            if t in ("-c", "-command") and i + 1 < len(rest):
                return _join_tokens(rest[i + 1:]), shells.POWERSHELL
    return "", ""


def _join_tokens(toks: List[str]) -> str:
    return " ".join(f'"{t}"' if any(c.isspace() for c in t) else t for t in toks)


def _strip_exe(token: str) -> str:
    """取命令词的可执行名：去路径前缀与 .exe，转小写"""
    name = token.strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _tokenize(seg: str) -> List[str]:
    """按空白切分并剥离成对引号"""
    out = []
    for tok in re.findall(r'"[^"]*"|\'[^\']*\'|[^\s"\']+', seg):
        if len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
            tok = tok[1:-1]
        out.append(tok)
    return out


def _cd_dir(toks: List[str]) -> Optional[str]:
    """识别 cd 段的目录参数，非 cd 段返回 None"""
    if not toks or _strip_exe(toks[0]) not in ("cd", "chdir"):
        return None
    args = [t for t in toks[1:] if not t.startswith("-") and not re.fullmatch(r"/[A-Za-z]", t)]
    return args[0] if len(args) == 1 else None


def _with_cwd(parts: List[str], target: str) -> str:
    """并入命令内 cd 累积的目录前缀；绝对路径不受影响"""
    if not parts:
        return target
    return str(Path(*parts) / target)


class DeleteCheck(NamedTuple):
    """删除命令分析结果：block 非空表示必须拦截，否则 targets 为待移入回收站的字面路径"""

    targets: List[str]
    block: str = ""


_PS_DOTNET_DELETE_RE = re.compile(r"(?i)::delete\s*\(|\.delete\(\s*\)")
_LOOP_WORDS = ("if", "for", "forfiles", "while", "until", "foreach", "foreach-object", "%")
_GROUP_STRIP = "({@"


def _strict_block(reason: str) -> DeleteCheck:
    return DeleteCheck([], (
        f"删除命令已被安全策略拦截：{reason}，无法转为移入回收站，原命令未执行。"
        "请改用 delete_file 工具删除确定路径；确需执行可在设置中关闭严格删除护栏。"
    ))


def _segment_tokens(seg: str) -> List[str]:
    """分词并剥掉分组括号、重定向与 sudo/then 这类前缀词"""
    text = seg.strip().lstrip(_GROUP_STRIP).rstrip(")}").strip()
    toks = _strip_redirects(_tokenize(text))
    while toks and _strip_exe(toks[0]) in _WRAPPER_WORDS + ("then", "do", "else"):
        toks = toks[1:]
        while toks and toks[0].startswith("-"):
            toks = toks[1:]
    return toks


def _parse_targets(word: str, rest: List[str], cwd_parts: List[str]) -> List[str]:
    targets: List[str] = []
    i = 0
    literal = False
    while i < len(rest):
        arg = rest[i]
        if not literal and arg == "--":
            literal = True
            i += 1
            continue
        if not literal and word in ("remove-item", "ri") and arg.lower() in ("-path", "-literalpath"):
            i += 1
            if i < len(rest):
                targets.append(_with_cwd(cwd_parts, rest[i]))
            i += 1
            continue
        if not literal and arg.startswith("-"):
            i += 1
            continue
        if not literal and sys.platform == "win32" and word in ("del", "erase", "rd", "rmdir") and re.fullmatch(r"/[A-Za-z]{1,3}", arg):
            i += 1
            continue
        targets.append(_with_cwd(cwd_parts, arg))
        i += 1
    return targets


def _walk_segments(text: str, name: str, strict: bool, depth: int, direct: bool) -> Optional[DeleteCheck]:
    """逐段分析复合命令；direct 为假时只看嵌套命令与严格护栏，用于已由 AST 处理过的 PowerShell"""
    segs = _split_cmd_segments(text, name, keep_redirect=True) or []
    targets: List[str] = []
    cwd_parts: List[str] = []
    found = False
    for seg in segs:
        toks = _segment_tokens(seg)
        if not toks:
            continue
        cd_dir = _cd_dir(toks)
        if cd_dir is not None:
            cwd_parts.append(cd_dir)
            continue
        index, word = 0, _strip_exe(toks[0])
        # 允许盘符路径前缀，如 C:\Tools\rm.exe
        if word not in _DELETE_WORDS and len(toks) >= 2 and _strip_exe(toks[1]) in _DELETE_WORDS:
            index, word = 1, _strip_exe(toks[1])
        inner, inner_shell = _nested_command(word, toks)
        if inner:
            sub = analyze_delete(inner, inner_shell, strict, depth + 1)
            if sub is None:
                continue
            if sub.block:
                return sub
            if cwd_parts:
                return DeleteCheck([], DELETE_UNRELIABLE)
            found = True
            targets.extend(sub.targets)
            continue
        if strict:
            # PowerShell 的条件与循环已由 AST 覆盖，这里只处理 bash/cmd
            if direct and word in _LOOP_WORDS and _ANY_DELETE_WORD_RE.search(" ".join(toks[1:])):
                return _strict_block("条件或循环内的删除无法确定目标")
            reason = _strict_block_reason(word, toks, seg)
            if reason:
                return _strict_block(reason)
        if not direct or word not in _DELETE_WORDS or word == "ri":
            continue
        found = True
        targets.extend(_parse_targets(word, toks[index + 1:], cwd_parts))
    if not found:
        return None
    if not targets:
        return DeleteCheck([], DELETE_UNRELIABLE)
    return DeleteCheck(targets)


def analyze_delete(cmd: str, shell: str = "", strict: bool = True, _depth: int = 0) -> Optional[DeleteCheck]:
    """分析删除类命令；非删除命令返回 None

    PowerShell 交解释器 AST 绑定参数取字面路径，bash/cmd 走分词解析。
    strict 开启时额外拦截 git clean、find -delete、解释器一行删除等无法转为回收站的形态。"""
    from core import shells

    text = str(cmd or "")
    name = shells.resolve_name(shell)
    if _depth > 3:
        return _strict_block("嵌套执行层数过深")
    if name != shells.POWERSHELL:
        return _walk_segments(text, name, strict, _depth, direct=True)
    result: Optional[DeleteCheck] = None
    if strict and _PS_DOTNET_DELETE_RE.search(text):
        return _strict_block(".NET 文件删除调用")
    hint = _PS_DELETE_HINT_RE if strict else _ANY_DELETE_WORD_RE
    if hint.search(text):
        targets, state = shells.powershell_parse(text)
        if state == shells.PS_PARSE_ERROR:
            return DeleteCheck([], "删除命令已被安全策略拦截：PowerShell 解析失败，无法确认删除目标，原命令未执行。请改用 delete_file 工具。")
        if state == shells.PS_PARSE_DYNAMIC:
            return DeleteCheck([], DELETE_UNRELIABLE)
        if state == shells.PS_PARSE_OK:
            result = DeleteCheck(targets or []) if targets else DeleteCheck([], DELETE_UNRELIABLE)
    nested = _walk_segments(text, name, strict, _depth, direct=False)
    if nested is None:
        return result
    if nested.block or result is None:
        return nested
    return DeleteCheck(result.targets + nested.targets)


def project_root() -> Path:
    # allowlist 按项目键控：项目即进程启动目录（与 ensure_project_identity(Path.cwd()) 同源）
    return Path.cwd()


def allowlist_path(root: Optional[Path] = None) -> Path:
    base = root or project_root()
    digest = hashlib.sha1(str(base.resolve()).encode("utf-8")).hexdigest()[:12]
    return _ROOT / "data" / "allowlist" / f"{digest}.json"


def load_allowlist(root: Optional[Path] = None) -> Dict[str, list]:
    path = allowlist_path(root)
    if not path.exists():
        return {"rules": []}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {"rules": []}
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            # 存量清洗：路径类工具的工具级通配（如 write|*）放行范围无界，加载即剔除
            data["rules"] = [
                r for r in data["rules"]
                if not (r.get("tool") in PATH_TOOLS and (r.get("fingerprint") or "") == f"{r.get('tool')}|*")
            ]
            return data
        log.warn("allowlist 格式异常，忽略: %s", path)
    except (json.JSONDecodeError, OSError) as e:
        log.warn("allowlist 读取失败 %s: %s", path, e)
    return {"rules": []}


def save_allowlist(data: dict, root: Optional[Path] = None) -> Path:
    path = allowlist_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def fingerprint(tool_name: str, args: Optional[dict]) -> str:
    """指令指纹：工具名 + 归一化关键参数"""
    args = args or {}
    if tool_name.startswith("mcp__"):
        # MCP 工具指纹不含参数：语义为「放行该工具」；mcp|<server>|* 前缀通配
        # 借 similar_fingerprint 的 |* 机制即可按服务器粒度放行
        server, _, tool = tool_name[len("mcp__"):].partition("__")
        return f"mcp|{server}|{tool}"
    if tool_name == "execute_command":
        cmd = str(args.get("command") or "")
        parts = cmd.strip().split()
        key = parts[0].lower() if parts else ""
        rest = " ".join(parts[1:3]).lower()
        # 执行器进指纹：同一命令在不同执行器下语义不同，不能跨执行器复用授权
        from core import shells

        shell_name = shells.resolve_name(args.get("shell"))
        return f"execute_command|{shell_name}|{key}|{rest}"
    if tool_name == "run_program":
        prog = str(args.get("program") or "").lower()
        return f"run_program|{prog}"
    if tool_name in ("write", "edit_file", "delete_file"):
        path = str(args.get("file_path") or "").replace("\\", "/")
        if sys.platform == "win32":
            # Windows 路径大小写不敏感，指纹归一小写；其余平台保留大小写
            path = path.lower()
        return f"{tool_name}|{path}"
    # 其他：工具名 + 参数键排序摘要
    keys = sorted(args.keys())
    digest_src = tool_name + "|" + ",".join(f"{k}={args[k]}" for k in keys[:5])
    return hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:16]


def similar_fingerprint(tool_name: str, args: Optional[dict]) -> str:
    """较宽指纹：同名工具 + 命令首词/路径目录"""
    args = args or {}
    if tool_name.startswith("mcp__"):
        server, _, _tool = tool_name[len("mcp__"):].partition("__")
        return f"mcp|{server}|*"
    if tool_name == "execute_command":
        cmd = str(args.get("command") or "")
        parts = cmd.strip().split()
        from core import shells

        shell_name = shells.resolve_name(args.get("shell"))
        return f"execute_command|{shell_name}|{(parts[0].lower() if parts else '')}"
    return fingerprint(tool_name, args).split("|")[0] + "|*"


def path_in_scope(tool_name: str, fp: str, root: Path) -> bool:
    """路径类工具的通配放行仅限项目根内：allowlist 按项目键控，
    write|* 这类工具级通配若不限定边界，可免确认写 hosts/.ssh 等项目外文件"""
    if tool_name not in PATH_TOOLS:
        return True
    target = fp.split("|", 1)[1] if "|" in fp else ""
    if not target:
        return False
    try:
        Path(target).resolve().relative_to(root)
        return True
    except ValueError:
        return False


def command_text(tool_name: str, args: Optional[dict]) -> str:
    """命令类工具的可读命令文本（execute_command/run_program）；其余工具空串"""
    args = args or {}
    if tool_name == "execute_command":
        return str(args.get("command") or "")
    if tool_name == "run_program":
        prog = args.get("program") or ""
        rest = " ".join(args.get("args") or [])
        return f"{prog} {rest}"
    return ""


def is_safe_call(tool_name: str, args: Optional[dict]) -> bool:
    """是否属于可自动放行的安全调用"""
    args = args or {}
    if tool_name in SAFE_TOOLS:
        return True
    if tool_name in ("execute_command", "run_program"):
        cmd = command_text(tool_name, args)
        if not cmd.strip():
            return False
        # 危险词全串扫描为独立第二层；复合命令逐段只读判定——"| powershell"、"| nc"
        # 这类外发链因段不只读落回确认，重定向（写文件）同样不放行
        if _DANGEROUS_CMD_RE.search(cmd):
            return False
        from core import shells

        return readonly_compound_cmd(cmd, shells.resolve_name(args.get("shell")))
    # 未知工具：保守
    return False


def evaluate(tool_name: str, args: Optional[dict], mode: str, root: Optional[Path] = None) -> Tuple[str, str]:
    """返回 (action, reason)：allow / confirm / deny / llm_approve"""
    action, reason = _evaluate(tool_name, args, mode, root)
    log.debug("策略判定 %s mode=%s -> %s(%s)", tool_name or "-", mode, action, reason)
    return action, reason


def _evaluate(tool_name: str, args: Optional[dict], mode: str, root: Optional[Path] = None) -> Tuple[str, str]:
    root = root or project_root()
    if mode == MODE_FULL:
        return ALLOW, "full"
    if mode not in MODES:
        # 失效关闭：未知模式不放假放行
        return CONFIRM, "unknown_mode"
    if not tool_name:
        return CONFIRM, "unknown_tool"
    if not register.has_tool(tool_name):
        # 未注册工具（拼写错误、旧会话历史里的已删工具、已下线 MCP）：直接答复，不进确认面板
        return DENY, f"工具不存在: {tool_name}"

    # 项目始终允许规则（路径类工具的通配命中需通过项目边界校验）
    rules = load_allowlist(root).get("rules") or []
    fp = fingerprint(tool_name, args)
    sfp = similar_fingerprint(tool_name, args)
    for rule in rules:
        if rule.get("tool") != tool_name:
            continue
        pattern = rule.get("fingerprint") or ""
        if pattern in (fp, sfp) and not pattern.endswith("|*"):
            return ALLOW, f"allowlist:{pattern}"
        if (
            pattern.endswith("|*")
            and fp.startswith(pattern[:-1])
            and path_in_scope(tool_name, fp, root)
        ):
            return ALLOW, f"allowlist:{pattern}"

    if is_safe_call(tool_name, args):
        return ALLOW, "safe"
    if mode == MODE_CONFIRM:
        return CONFIRM, "manual_unsafe"
    if tool_name in ("write", "edit_file"):
        # 编辑类工具在自动接收编辑/自动模式下完全放行，不经确认也不经 LLM 裁决
        return ALLOW, "auto_edit"
    if mode == MODE_AUTO:
        # 自动模式：其余不确定的工具调用（含危险/删除类命令）一律交 LLM 裁决
        return LLM_APPROVE, "auto_approval"
    return CONFIRM, "mode_requires_confirmation"


def add_always_allow(tool_name: str, args: Optional[dict], root: Optional[Path] = None, similar: bool = True) -> dict:
    """写入本项目始终允许规则"""
    data = load_allowlist(root)
    rules = data.setdefault("rules", [])
    fps = {fingerprint(tool_name, args)}
    if similar:
        fps.add(similar_fingerprint(tool_name, args))
    added = []
    for item in fps:
        if not any(r.get("tool") == tool_name and r.get("fingerprint") == item for r in rules):
            rules.append({"tool": tool_name, "fingerprint": item})
            added.append(item)
    save_allowlist(data, root)
    if added:
        log.info("始终允许已写入: %s [%s]", tool_name, " | ".join(added))
    return data


def default_reject_message(reason: str = "") -> str:
    reason = (reason or "").strip()
    if reason:
        return f"用户拒绝执行该指令: {reason}"
    return "用户拒绝执行该指令"


def approval_reject_message(reason: str = "") -> str:
    reason = (reason or "").strip()
    if reason:
        return f"审批器拒绝执行该指令: {reason}"
    return "审批器拒绝执行该指令"
