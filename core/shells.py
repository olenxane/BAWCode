#命令执行器适配层：解析默认/单次执行器，构造 argv，调用解释器自身做语法预检（只解析不执行）
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Tuple

from core.log import get_logger

log = get_logger("shells")

GIT_BASH = "git_bash"        # Git for Windows 自带的 Bash
POWERSHELL = "powershell"    # Windows PowerShell 5.1（powershell.exe）
CMD = "cmd"                  # cmd.exe

# 设置面板/工具参数可选值；内置默认 cmd.exe（系统自带、零依赖；Git Bash 需自行安装）
SHELL_NAMES = (GIT_BASH, POWERSHELL, CMD)
DEFAULT_SHELL = CMD
LABELS = {GIT_BASH: "Git Bash", POWERSHELL: "Windows PowerShell", CMD: "cmd.exe"}

# 执行器身份说明：写进提示词，让模型按实际执行器的语法书写命令
NOTES = {
    GIT_BASH: "Git for Windows 的 bash；命令按 POSIX shell 语法书写，可用 '&&'、'|'、多行与 heredoc",
    POWERSHELL: "Windows PowerShell 5.1（powershell.exe）；命令按 PowerShell 语法书写，顺序执行用 ';'（该版本不支持 '&&'）",
    CMD: "Windows 命令提示符（cmd.exe）；顺序执行用 '&&'（';' 只是普通字符），命令必须是单行",
}

_LABEL_TO_NAME = {v.lower(): k for k, v in LABELS.items()}
_LABEL_TO_NAME["bash"] = GIT_BASH
_LABEL_TO_NAME["powershell.exe"] = POWERSHELL
_LABEL_TO_NAME["cmd.exe"] = CMD

_process_lock = threading.Lock()
_process_cache = {}   # name -> 可执行文件路径（str）或 None（不可用）
_version_cache = {}   # name -> 版本字符串


def resolve_name(name: Optional[str]) -> str:
    """执行器名归一化：空值取默认，兼容标签/别名写法，未知名称原样返回交由校验报错"""
    text = str(name or "").strip()
    if not text:
        return DEFAULT_SHELL
    low = text.lower()
    if low in SHELL_NAMES:
        return low
    return _LABEL_TO_NAME.get(low, low)


def is_valid(name: str) -> bool:
    return name in SHELL_NAMES


def default_name() -> str:
    """默认执行器：读配置 tools.default_shell，读取失败回退内置默认"""
    try:
        from core import memory as memory_mod

        session = memory_mod.get_session()
        data = getattr(getattr(session, "config", None), "data", None) or {}
        value = resolve_name((data.get("tools") or {}).get("default_shell"))
        if is_valid(value):
            return value
    except Exception:
        pass
    return DEFAULT_SHELL


def code_pages() -> set:
    """启动参数类环境键：切执行器时清理，避免上一执行器的设置污染本次子进程"""
    if sys.platform != "win32":
        return set()
    return {"CHCP", "PYTHONIOENCODING"}


def _git_install_roots() -> list:
    """Git for Windows 安装根目录：注册表 InstallPath、PATH 上 git.exe 反推

    Git 允许装到任意目录，只按 Program Files 之类的固定位置猜不到；注册表 InstallPath
    与 git.exe 的上溯目录是可靠旁证（git.exe 位于 <root>\\cmd、<root>\\bin 或
    <root>\\mingw64\\bin 三种层级）。"""
    roots = []
    if sys.platform == "win32":
        import winreg

        for hive, path in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GitForWindows"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\GitForWindows"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GitForWindows"),
        ):
            try:
                with winreg.OpenKey(hive, path) as key:
                    root = winreg.QueryValueEx(key, "InstallPath")[0]
            except OSError:
                continue
            if root:
                roots.append(str(root))
    found = shutil.which("git")
    if found:
        root = Path(found).resolve().parent.parent
        if root.name.lower() in ("mingw32", "mingw64"):
            root = root.parent
        roots.append(str(root))
    return roots


def _git_bash_candidates() -> list:
    """Git Bash 候选路径：环境变量 > 常见安装位置 > 注册表/PATH 反推的安装根 > PATH"""
    out = []
    for env_name in ("BAWCODE_GIT_BASH", "GIT_BASH"):
        value = os.environ.get(env_name)
        if value:
            out.append(value)
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
        os.environ.get("LocalAppData"),
    ]
    for root in roots:
        if root:
            out.append(str(Path(root) / "Git" / "bin" / "bash.exe"))
            out.append(str(Path(root) / "Git" / "usr" / "bin" / "bash.exe"))
    for root in _git_install_roots():
        out.append(str(Path(root) / "bin" / "bash.exe"))
        out.append(str(Path(root) / "usr" / "bin" / "bash.exe"))
    which = shutil.which("bash")
    if which:
        out.append(which)
    return out


def _is_not_wsl(path: str) -> bool:
    """排除 System32 下的 WSL bash：它的路径语义与 Git Bash 不同，不能当 Unix 命令执行器"""
    normalized = str(path).replace("/", "\\").lower()
    return "\\system32\\" not in normalized and "\\sysnative\\" not in normalized


def _resolve_process(name: str) -> Optional[str]:
    with _process_lock:
        if name in _process_cache:
            return _process_cache[name]
        found = None
        if name == GIT_BASH:
            for candidate in _git_bash_candidates():
                if candidate and Path(candidate).is_file() and _is_not_wsl(candidate):
                    found = str(Path(candidate))
                    break
        elif name == POWERSHELL:
            system_root = os.environ.get("SystemRoot") or r"C:\Windows"
            candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if candidate.is_file():
                found = str(candidate)
            else:
                found = shutil.which("powershell")
        elif name == CMD:
            system_root = os.environ.get("SystemRoot") or r"C:\Windows"
            candidate = Path(system_root) / "System32" / "cmd.exe"
            found = str(candidate) if candidate.is_file() else shutil.which("cmd")
        _process_cache[name] = found
        return found


def label(name: str) -> str:
    return LABELS.get(resolve_name(name), str(name or ""))


def unavailable_message(name: str) -> str:
    """执行器不可用时的结果文本：不静默改用其它执行器"""
    shown = label(name)
    if name == GIT_BASH:
        return (
            "命令未执行：未找到 Git Bash（bash.exe）。请安装 Git for Windows，"
            "或用 BAWCODE_GIT_BASH 指定 bash.exe 路径，或在设置中改用其它执行器。"
        )
    return f"命令未执行：执行器 {shown} 不可用；请在设置中选择其它执行器。"


def version(name: str) -> str:
    """解释器版本（惰性探测并缓存）：命令适配与提示词展示用，探测失败返回空串"""
    name = resolve_name(name)
    with _process_lock:
        if name in _version_cache:
            return _version_cache[name]
    binary = _resolve_process(name)
    text = ""
    if binary:
        try:
            if name == GIT_BASH:
                proc = subprocess.run(
                    [binary, "-c", "echo $BASH_VERSION"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    env=build_env(name),
                    errors="replace",
                )
                text = (proc.stdout or "").strip()
            elif name == POWERSHELL:
                script = "$PSVersionTable.PSVersion.ToString()"
                proc = subprocess.run(
                    [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                )
                text = (proc.stdout or b"").decode("ascii", errors="replace").strip()
            elif name == CMD:
                text = ""  # cmd.exe 无版本号可查，提示词不显示空括注
        except (OSError, subprocess.SubprocessError) as exc:
            log.warn("执行器版本探测失败 %s: %r", name, exc)
            text = ""
    with _process_lock:
        _version_cache[name] = text
    return text


def available(name: str) -> bool:
    return _resolve_process(resolve_name(name)) is not None


def build_env(name: Optional[str] = None) -> dict:
    """子进程环境：清理旧代码页设置并统一 Python 标准输出为 UTF-8"""
    env = dict(os.environ)
    for key in code_pages():
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    if name and resolve_name(name) == GIT_BASH:
        env["CHERE_INVOKING"] = "1"  # bash 从当前目录启动，不跳到用户主目录
        env["MSYS_NO_PATHCONV"] = "1"  # 不做 Windows/Unix 路径自动转换，路径原样传入
    return env


def build_command(command: str, name: str) -> Tuple[Optional[list], str, bool]:
    """构造启动方式：返回 (argv, stdin_text, use_shell)

    多行命令经 stdin 送入解释器（stderr=STDOUT 让语法/运行错误一并回收），避免命令行换行
    在 Windows 上被截断。Git Bash / PowerShell 直接启动解释器，少一层中间 shell 解析；
    cmd.exe 必须交 CPython 走 %COMSPEC%（shell=True）——把含引号整串放进参数数组会让
    cmd 重新解析引号，破坏嵌套引号命令。执行器不可用时返回 (None, 提示, False)。"""
    name = resolve_name(name)
    binary = _resolve_process(name)
    if binary is None:
        return None, unavailable_message(name), False
    text = str(command or "")
    if name == GIT_BASH:
        if "\n" in text or "\r" in text:
            return [binary, "-s"], text, False
        return [binary, "-c", text], "", False
    if name == POWERSHELL:
        if "\n" in text or "\r" in text:
            return [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"], text, False
        return [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", text], "", False
    return text, "", True


# 语法预检脚本：只解析不执行。内嵌脚本经 -EncodedCommand 传递，命令经环境变量送入，
# 双层都不做字符串拼接，避免引号/换行在 Windows 命令行上被重新解释

# 判定删除命令与目标：平铺枚举 AST（不做嵌套递归，避免在函数作用域内无限展开），
# 字面常量 path 作为目标；变量/展开串/子表达式标记为动态，交由调用方拒绝而非猜目标
_PS_VISIT = r"""
$names = @('remove-item', 'rm', 'del', 'erase', 'rd', 'rmdir', 'rimraf', 'deltree')
$targets = New-Object 'System.Collections.Generic.List[string]'
$dynamic = $false
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($cmd, [ref]$null, [ref]$errors)
if ($errors.Count -gt 0) {
    $first = $errors[0]
    Write-Output ("E|{0}|{1}|{2}" -f $first.Extent.StartLineNumber, $first.Extent.StartColumnNumber, $first.Message)
    exit 0
}
foreach ($cmdAst in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    $name = $cmdAst.GetCommandName()
    if (-not $name -or ($names -notcontains $name.ToLower())) { continue }
    $skipNext = $false
    $firstName = $true
    foreach ($el in $cmdAst.CommandElements) {
        if ($firstName) { $firstName = $false; continue }
        if ($el -is [System.Management.Automation.Language.CommandParameterAst]) {
            $skipNext = $true
        }
        elseif ($el -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
            if (-not $skipNext) { $targets.Add($el.Value) }
        }
        elseif ($el -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) { $dynamic = $true }
        elseif ($el -is [System.Management.Automation.Language.VariableExpressionAst]) { $dynamic = $true }
        elseif ($el -is [System.Management.Automation.Language.ArrayLiteralAst]) { $dynamic = $true }
        elseif ($el -is [System.Management.Automation.Language.ArrayExpressionAst]) { $dynamic = $true }
        elseif ($el -is [System.Management.Automation.Language.SubExpressionAst]) { $dynamic = $true }
        elseif ($el -isnot [System.Management.Automation.Language.CommandExpressionAst]) { $dynamic = $true }
        $skipNext = $false
    }
}
$flag = if ($dynamic) { 'D' } else { 'T' }
Write-Output ($flag + '|')
Write-Output ($targets -join "`n")
"""

_PS_DETECT_SCRIPT = "\n".join([
    # 统一 UTF-8：stdin 按 UTF-8 解码、stdout 按 UTF-8 输出，避免中文在代码页间乱码；关进度输出，避免 CLIXML 噪音混进 stderr
    "$ProgressPreference = 'SilentlyContinue'",
    "$ErrorActionPreference = 'Stop'",
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
    "$OutputEncoding = [System.Text.Encoding]::UTF8",
    "[Console]::InputEncoding = [System.Text.Encoding]::UTF8",
    "$cmd = [Console]::In.ReadToEnd()",
    _PS_VISIT,
])


def _encoded_command(script: str) -> str:
    import base64

    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _powershell_run(binary: str, script: str, stdin_text: str, timeout: int) -> Optional[str]:
    argv = [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded_command(script)]
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=build_env(POWERSHELL),
        )
    except OSError as exc:
        log.warn("PowerShell 探测启动失败: %r", exc)
        return None
    except subprocess.TimeoutExpired:
        log.warn("PowerShell 探测超时")
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    return out


def powershell_parse(command: str) -> Tuple[Optional[list], bool]:
    """PowerShell AST 解析：返回 (删除目标, 是否含动态表达式)

    未识别为删除命令返回 (None, False)；识别为删除但目标不可靠时返回 ([] , True)。
    语法错误由 syntax_issue 先行拦截，这里返回 (None, False)。"""
    binary = _resolve_process(POWERSHELL)
    if binary is None:
        return None, False
    out = _powershell_run(binary, _PS_DETECT_SCRIPT, str(command or ""), 30)
    if not out:
        return None, False
    head = out.split("|", 1)
    flag = head[0]
    if flag == "E":
        return None, False
    if flag not in ("T", "D"):
        return None, False
    body = head[1] if len(head) > 1 else ""
    targets = [part for part in body.replace("\r", "").split("\n") if part.strip()]
    if flag == "D":
        return [], True
    return targets, False


def syntax_issue(command: str, name: str) -> str:
    """执行器语法预检：只解析不执行，返回可操作提示；无问题返回空串

    覆盖“命令语法与当前执行器不匹配”与“执行器缺失”两类问题。
    注意这不能保证删除目标正确——删除安全由 delete_guard 负责。"""
    name = resolve_name(name)
    text = str(command or "")
    if not text.strip():
        return ""
    binary = _resolve_process(name)
    if binary is None:
        return unavailable_message(name)
    if name == GIT_BASH:
        return _bash_syntax_issue(binary, text)
    if name == POWERSHELL:
        return _powershell_syntax_issue(binary, text)
    return _cmd_syntax_issue(text)


def _bash_syntax_issue(binary: str, command: str) -> str:
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 30,
        "env": build_env(GIT_BASH),
    }
    if "\n" in command or "\r" in command:
        argv = [binary, "-n", "-s"]
        kwargs["input"] = command
    else:
        argv = [binary, "-n", "-c", command]
        kwargs["stdin"] = subprocess.DEVNULL
    try:
        proc = subprocess.run(argv, **kwargs)
    except OSError as exc:
        return f"命令未执行：Git Bash 启动失败（{exc}）"
    except subprocess.TimeoutExpired:
        return "命令未执行：Git Bash 语法检查超时"
    if proc.returncode == 0:
        return ""
    detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
    shown = f"Git Bash {version(GIT_BASH)}".strip()
    return (
        f"命令未执行：命令不是有效的 bash 语法（当前执行器 {shown}）。\n"
        f"{detail}\n"
        "请按 bash 语法改写；确需 PowerShell 的写法时，可在设置的默认执行器或本次调用的 shell 参数中明确指定。"
    )


def _powershell_syntax_issue(binary: str, command: str) -> str:
    out = _powershell_run(binary, _PS_DETECT_SCRIPT, command, 30)
    if not out or not out.startswith("E|"):
        return ""
    parts = out.split("|", 3)
    detail = parts[3] if len(parts) > 3 else ""
    line = parts[1] if len(parts) > 1 else "?"
    col = parts[2] if len(parts) > 2 else "?"
    shown = f"Windows PowerShell {version(POWERSHELL)}".strip()
    return (
        f"命令未执行：命令无法被 {shown} 解析（第 {line} 行第 {col} 列）。\n"
        f"解析错误: {detail}\n"
        "请使用该版本支持的写法（如 5.1 不支持 '&&'，顺序执行请用 ';'）；"
        "确需其它执行器时，可在设置的默认执行器或本次调用的 shell 参数中明确指定。"
    )


def _cmd_syntax_issue(command: str) -> str:
    """cmd.exe 没有可靠的完整语法预检接口，只拦可确定会截断命令的换行

    cmd /c 只执行首个换行之前的内容，其余行连同其报错被静默丢弃、退出码仍为 0；
    换行之后没有非空白内容时（纯尾随换行）执行结果不受影响，放行。"""
    if any(line.strip() for line in command.splitlines()[1:]):
        return (
            "命令未执行：cmd.exe 不支持多行命令——首个换行之后还有内容，"
            "只会执行第一行并静默丢弃其余行（退出码仍为 0）。"
            "请写成单行；多行脚本先写入临时 .py/.ps1 文件，再用单行命令运行该文件，"
            "或在设置中改用 Git Bash / PowerShell 执行器。"
        )
    return ""
