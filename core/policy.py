#工具访问策略：自动/手动/完全访问
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

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
    # 只读图片文件回注多模态消息，无写入副作用
    "read_image",
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

# 只读动词表：SAFE 判定要求整条命令无管道/链式/重定向（复合命令一律不走 SAFE），
# 且首词（取文件名部分）在此表内——关键词子串匹配会让 "dir | powershell ..." 误判安全
SAFE_VERBS = {
    "ls", "dir", "pwd", "cd", "echo", "cat", "head", "tail", "tree",
    "where", "which", "env", "set", "ver", "version", "type",
}
# 两段式只读命令（首词 + 子命令/标志）
SAFE_VERB_PAIRS = {
    "git status", "git log", "git branch", "pip show", "python -v", "python3 -v",
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
    """单条无操作符命令，首词属于只读动词表（含两段式组合）"""
    tokens = cmd.strip().split()
    if not tokens:
        return False
    first = tokens[0].lower().replace("\\", "/").rsplit("/", 1)[-1]
    if first in SAFE_VERBS:
        return True
    return " ".join([first] + [t.lower() for t in tokens[1:2]]) in SAFE_VERB_PAIRS
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

# 删除类命令硬拒绝（完全访问模式也不例外）：shell 直删不可恢复，文件删除一律走
# delete_file 工具（移入项目回收站，可 /undo 回滚、/clear-trash 真正清空）。
# 只匹配命令首词或分隔符（|;& 与换行，shell 下换行即命令分隔）之后的删除词，
# 避免 "python app.py del" 这类参数误伤
_DELETE_CMD_RE = re.compile(
    r"(?ix)(?:^|[|;&\r\n])\s*(?:[a-z]:\S+\s+)?(?:rm|rmdir|rd|del|erase|deltree|rimraf)(?:\s|$)|"
    r"(?:^|[|;&\r\n])\s*remove-item(?:\s|$)"
)
_DELETE_DENY_REASON = (
    "删除类命令被拦截（不可恢复）：请改用 delete_file 工具"
    "（文件移入回收站，可 /undo 回滚、/clear-trash 真正清空）"
)


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
        # 危险词全串扫描为独立第二层；复合命令（管道/链式/重定向）一律不 SAFE，
        # 落回确认路径——"| powershell"、"| nc" 这类外发链由此拦下
        if _DANGEROUS_CMD_RE.search(cmd):
            return False
        if has_shell_operator(cmd):
            return False
        return readonly_simple_cmd(cmd)
    # 未知工具：保守
    return False


def evaluate(tool_name: str, args: Optional[dict], mode: str, root: Optional[Path] = None) -> Tuple[str, str]:
    """返回 (action, reason)：allow / confirm / deny"""
    action, reason = _evaluate(tool_name, args, mode, root)
    log.debug("策略判定 %s mode=%s -> %s(%s)", tool_name or "-", mode, action, reason)
    return action, reason


def _evaluate(tool_name: str, args: Optional[dict], mode: str, root: Optional[Path] = None) -> Tuple[str, str]:
    root = root or project_root()
    # 删除类命令硬安全栏：先于一切模式判定（full 也不例外）
    if tool_name == "execute_command" and _DELETE_CMD_RE.search(str((args or {}).get("command") or "")):
        return DENY, _DELETE_DENY_REASON
    if mode == MODE_FULL:
        return ALLOW, "full"
    if not mode:
        # 失效关闭：未知模式不放假放行
        return CONFIRM, "unknown_mode"
    if not tool_name:
        return CONFIRM, "unknown_tool"

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
