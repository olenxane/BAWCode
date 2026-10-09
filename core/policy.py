#工具访问策略：自动/手动/完全访问
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core import register
from core.log import get_logger

log = get_logger("policy")

_ROOT = Path(__file__).resolve().parent.parent

# 模式常量
MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODE_FULL = "full"
MODES = (MODE_AUTO, MODE_MANUAL, MODE_FULL)
MODE_LABELS = {
    MODE_AUTO: "自动模式",
    MODE_MANUAL: "手动模式",
    MODE_FULL: "完全访问",
}

# 判定结果
ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"

# 路径类工具：文件系统副作用，通配放行需限定项目边界
PATH_TOOLS = ("write", "edit_file", "delete_file")

# 只读安全工具（manual 也放行）
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


def readonly_compound_cmd(cmd: str) -> bool:
    """复合命令逐段判定：引号外按 |;& 与换行切分，重定向直接否，每段须为只读简单命令"""
    segs, cur, in_dq = [], [], False
    for ch in cmd:
        if ch == '"':
            in_dq = not in_dq
            cur.append(ch)
        elif in_dq:
            cur.append(ch)
        elif ch in "<>":
            return False
        elif ch in "|;&\n":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    segs.append("".join(cur))
    segs = [s for s in segs if s.strip()]
    return bool(segs) and all(readonly_simple_cmd(s) for s in segs)
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

# 删除类命令改为"移入回收站"执行：不再直接拒绝，命令放行到 execute_command，
# 由工具层把目标静默移入项目回收站，真正删除只由用户 /clear-trash 触发。
# 只匹配命令首词或分隔符（|;& 与换行，shell 下换行即命令分隔）之后的删除词，
# 避免 "python app.py del" 这类参数误伤
_DELETE_WORDS = ("rmdir", "rimraf", "deltree", "remove-item", "erase", "rm", "rd", "del")
_DELETE_CMD_RE = re.compile(
    r"(?ix)(?:^|[|;&\r\n])\s*(?:[a-z]:\S+\s+)?(?:" + "|".join(_DELETE_WORDS) + r")(?:\s|$)"
)
_DELETE_TRASH_REASON = "删除类命令改为移入项目回收站执行"


def is_delete_command(cmd: str) -> bool:
    """命令是否命中删除类命令"""
    return bool(_DELETE_CMD_RE.search(str(cmd or "")))


def _strip_exe(token: str) -> str:
    """取命令词的可执行名：去路径前缀与 .exe，转小写"""
    name = token.strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _split_command_segments(cmd: str) -> List[str]:
    """引号外按 |;& 与换行切分复合命令"""
    segs: List[str] = []
    cur: List[str] = []
    in_dq = False
    for ch in cmd:
        if ch == '"':
            in_dq = not in_dq
            cur.append(ch)
        elif in_dq:
            cur.append(ch)
        elif ch in "|;&\r\n":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    segs.append("".join(cur))
    return [s for s in segs if s.strip()]


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


def delete_targets(cmd: str) -> Optional[List[str]]:
    """解析删除类命令的目标路径；非删除类命令返回 None，命中但目标不可靠时返回空表"""
    text = str(cmd or "")
    if not is_delete_command(text):
        return None
    # 重定向会混入与删除无关的 token，目标不可靠时返回空表交由调用方拒绝执行
    in_dq = False
    for ch in text:
        if ch == '"':
            in_dq = not in_dq
        elif not in_dq and ch in "<>":
            return []
    targets: List[str] = []
    cwd_parts: List[str] = []
    for seg in _split_command_segments(text):
        toks = _tokenize(seg)
        if not toks:
            continue
        cd_dir = _cd_dir(toks)
        if cd_dir is not None:
            cwd_parts.append(cd_dir)
            continue
        index, word = 0, _strip_exe(toks[0])
        if word not in _DELETE_WORDS:
            # 允许盘符路径前缀，如 C:\Tools\rm.exe
            if len(toks) < 2 or _strip_exe(toks[1]) not in _DELETE_WORDS:
                continue
            index, word = 1, _strip_exe(toks[1])
        rest = toks[index + 1:]
        i = 0
        literal = False
        while i < len(rest):
            arg = rest[i]
            if not literal and arg == "--":
                literal = True
                i += 1
                continue
            if not literal and word == "remove-item" and arg.lower() in ("-path", "-literalpath"):
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
        return f"execute_command|{key}|{rest}"
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
        return f"execute_command|{(parts[0].lower() if parts else '')}"
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


def _command_text(tool_name: str, args: dict) -> str:
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
        cmd = _command_text(tool_name, args)
        if not cmd.strip():
            return False
        # 危险词全串扫描为独立第二层；复合命令逐段只读判定——"| powershell"、"| nc"
        # 这类外发链因段不只读落回确认，重定向（写文件）同样不放行
        if _DANGEROUS_CMD_RE.search(cmd):
            return False
        return readonly_compound_cmd(cmd)
    # 未知工具：保守
    return False


def evaluate(tool_name: str, args: Optional[dict], mode: str, root: Optional[Path] = None) -> Tuple[str, str]:
    """返回 (action, reason)：allow / confirm / deny"""
    action, reason = _evaluate(tool_name, args, mode, root)
    log.debug("策略判定 %s mode=%s -> %s(%s)", tool_name or "-", mode, action, reason)
    return action, reason


def _evaluate(tool_name: str, args: Optional[dict], mode: str, root: Optional[Path] = None) -> Tuple[str, str]:
    root = root or project_root()
    # 删除类命令不再拒绝：放行到工具层静默移入项目回收站，execute_command 与 run_program 同口径
    if tool_name in ("execute_command", "run_program") and is_delete_command(_command_text(tool_name, args or {})):
        return ALLOW, _DELETE_TRASH_REASON
    if mode == MODE_FULL:
        return ALLOW, "full"
    if not mode:
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

    if mode == MODE_MANUAL:
        if is_safe_call(tool_name, args):
            return ALLOW, "manual_safe"
        return CONFIRM, "manual_unsafe"

    # 自动模式：安全放行，否则确认
    if is_safe_call(tool_name, args):
        return ALLOW, "auto_safe"
    return CONFIRM, "auto_unsafe"


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
