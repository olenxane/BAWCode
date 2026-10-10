# 插件系统：把 hooks/commands/tools/skills/上下文补充 打包为可分发的目录插件。
#
# 目录（双层，与 skills 同范式）：
#   全局  data/plugins/<id>/          —— config.plugins.dir
#   项目  {workspace}/.bawcode/plugins/<id>/   —— 同名插件覆盖全局
# 插件结构：
#   plugin.json   清单（id 必填；entry 缺省 main.py；enabled 缺省 true）
#   main.py       入口模块，定义 setup(ctx)；ctx 提供 hooks/commands/tools/补充注册 API
#   skills/<n>/SKILL.md   可选，自动并入技能系统（项目级技能仍最高优先）
#   requirements.txt      可选，插件独有依赖；装载时缺失项经 pip 自动补齐，不随主程序安装
#
# 生命周期：启动 discover→逐个装载（错误隔离，失败不影响其他插件）；启用的插件
# 先按 requirements.txt 补齐依赖，再 import+setup；auto_install_deps 可关；
# /plugin reload 卸载重载（按 owner 注销 hooks/commands/tools）；
# 禁用持久化到 config.plugins.disable。
#
# 信任模型：插件在本进程内以完整权限运行 Python 代码，只安装可信来源的插件。
import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:  # packaging 随 pip 生态普遍存在；缺失时依赖检查退化为仅按发行名判断有无
    from packaging.requirements import Requirement as _Requirement
except Exception:  # pragma: no cover
    _Requirement = None  # type: ignore[assignment]

from core import commands
from core import hooks
from core import register
from core import skills
from core.log import get_logger

log = get_logger("plugins")

_ROOT = Path(__file__).resolve().parent.parent

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 插件配置声明支持的类型（plugin.json 的 config 数组，见 docs/plugin-development.md §9）
CONFIG_TYPES = ("str", "list", "int", "float", "bool")


def _coerce_decl_value(ctype: str, value: Any) -> Any:
    """按声明类型规整值；不可转换返回 None"""
    try:
        if value is None:
            return None
        if ctype == "str":
            return str(value)
        if ctype == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "on", "开")
            return bool(value)
        if ctype == "int":
            return int(float(value))
        if ctype == "float":
            return float(value)
    except (TypeError, ValueError):
        return None
    return value


def _parse_config_decls(manifest: dict, pid: str) -> List[dict]:
    """解析 plugin.json 的 config 声明数组；非法项 warn 后丢弃（不影响装载）"""
    raw = manifest.get("config")
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.warn("插件 %s 的 config 声明必须是数组，已忽略", pid)
        return []
    decls: List[dict] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            log.warn("插件 %s config 声明项必须是对象: %r", pid, item)
            continue
        key = str(item.get("key") or "").strip()
        ctype = str(item.get("type") or "").strip().lower()
        if not _KEY_RE.match(key):
            log.warn("插件 %s config 声明 key 非法（字母开头，字母数字下划线）: %r", pid, key)
            continue
        if ctype not in CONFIG_TYPES:
            log.warn("插件 %s config 声明 %s 类型非法（支持 %s）: %r", pid, key, "/".join(CONFIG_TYPES), ctype)
            continue
        if key in seen:
            log.warn("插件 %s config 声明 key 重复，已跳过: %s", pid, key)
            continue
        seen.add(key)
        decl = {
            "key": key,
            "type": ctype,
            "label": str(item.get("label") or key),
            "hint": str(item.get("hint") or ""),
            "default": _coerce_decl_value(ctype, item.get("default")),
        }
        if ctype == "list":
            options = item.get("options")
            if not isinstance(options, list) or not options:
                log.warn("插件 %s config 声明 %s 为 list 类型但缺非空 options，已跳过", pid, key)
                continue
            decl["options"] = [str(o) for o in options]
            if decl["default"] is not None and decl["default"] not in decl["options"]:
                decl["default"] = decl["options"][0]
        if ctype in ("int", "float"):
            lo = _coerce_decl_value(ctype, item.get("min"))
            hi = _coerce_decl_value(ctype, item.get("max"))
            if lo is not None:
                decl["min"] = lo
            if hi is not None:
                decl["max"] = hi
        decls.append(decl)
    return decls

# id -> {"manifest", "dir", "source", "status", "error", "context", "module"}
_loaded: Dict[str, dict] = {}
_runtime_paths: List[str] = []  # 装载期间加入 sys.path 的插件目录（卸载时移除）
_last_workspace: Optional[Path] = None  # 最近一次 load 的 workspace（reload 缺省复用）
# 运行时载体（main 在创建 _AgentRunner 后经 bind_runtime 注入）：
#   app    —— UI 桥与渲染（ctx.ui 交互面板、notify 刷新）
#   runner —— 回合调度（_AgentRunner：start/submit，submit_turn 使用）
_runtime: Dict[str, Any] = {"app": None, "runner": None}


def bind_runtime(app=None, runner=None) -> None:
    """main 注入运行时载体（app/runner）；不传的项保留原值。

    插件的类用户操作 API（ctx.submit_turn / ctx.notify / ctx.ui）依赖此注入；
    注入前调用这些 API 会得到失败提示/None，不会抛异常。"""
    if app is not None:
        _runtime["app"] = app
    if runner is not None:
        _runtime["runner"] = runner
    log.debug("插件运行时已注入: app=%s runner=%s", "有" if app else "-", "有" if runner else "-")


def runtime() -> Dict[str, Any]:
    """只读访问运行时载体 {app, runner}（bind_runtime 注入的对象引用）。

    插件可读取 app 的实时状态（busy / streaming_msg / status / phase_hint）与
    runner（busy 判定、llm.cancel() 中断在途回合）；未注入时对应值为 None。
    约定只读：请勿替换其中对象或改动 runner 的调度状态。"""
    return dict(_runtime)


# ---------------------------------------------------------------------------
# 清单与发现


def _plugins_dirs(config, workspace: Optional[Path]) -> List[Path]:
    data = getattr(config, "data", None) or {}
    cfg = data.get("plugins") or {}
    rel = cfg.get("dir") or "data/plugins"
    p = Path(rel)
    global_dir = p if p.is_absolute() else _ROOT / rel
    ws = Path(workspace or Path.cwd())
    return [global_dir, ws / ".bawcode" / "plugins"]


def _read_manifest(dir_path: Path) -> tuple:
    """读取 plugin.json；缺失时用目录名 + 缺省 entry 兜底。返回 (manifest, error)"""
    manifest_path = dir_path / "plugin.json"
    manifest: Dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {}, f"plugin.json 解析失败: {e}"
        if not isinstance(manifest, dict):
            return {}, "plugin.json 必须是 JSON 对象"
    pid = str(manifest.get("id") or dir_path.name).strip()
    if not _ID_RE.match(pid):
        return {}, f"插件 id 非法（允许字母数字-_）: {pid}"
    manifest["id"] = pid
    manifest.setdefault("name", pid)
    manifest.setdefault("version", "0.0.0")
    manifest.setdefault("description", "")
    manifest.setdefault("author", "")
    manifest.setdefault("entry", "main.py")
    manifest.setdefault("enabled", True)
    return manifest, ""


def discover(config, workspace: Optional[Path] = None) -> List[dict]:
    """扫描双层插件目录，返回按装载顺序的 [{id, dir, manifest, source}]；
    项目级同名插件覆盖全局（后者不再装载）"""
    found: Dict[str, dict] = {}
    order: List[str] = []
    for source, base in zip(("global", "project"), _plugins_dirs(config, workspace)):
        if not base.is_dir():
            continue
        for dir_path in sorted(base.iterdir()):
            if not dir_path.is_dir() or dir_path.name.startswith(("_", ".")):
                continue
            manifest, err = _read_manifest(dir_path)
            pid = manifest.get("id") or dir_path.name
            if err:
                found[pid] = {
                    "id": pid,
                    "dir": dir_path,
                    "manifest": {"id": pid, "name": pid, "enabled": False},
                    "source": source,
                    "error": err,
                }
                if pid not in order:
                    order.append(pid)
                continue
            if pid in found and found[pid]["source"] == "global" and source == "project":
                log.info("插件 %s 被项目级版本覆盖: %s", pid, dir_path)
            elif pid in found:
                log.warn("同层插件 id 重复，后扫描目录生效: %s（%s 覆盖 %s）", pid, dir_path, found[pid]["dir"])
            else:
                order.append(pid)
            found[pid] = {"id": pid, "dir": dir_path, "manifest": manifest, "source": source, "error": ""}
    return [found[pid] for pid in order]


def _enabled_of(manifest: dict, config) -> tuple:
    """判定插件启用状态：返回 (enabled, reason)"""
    data = getattr(config, "data", None) or {}
    cfg = data.get("plugins") or {}
    if not cfg.get("enabled", True):
        return False, "插件系统全局关闭（config.plugins.enabled=false）"
    pid = manifest.get("id") or ""
    if pid in set(cfg.get("disable") or []):
        return False, "已被禁用（config.plugins.disable）"
    if not manifest.get("enabled", True):
        return False, "清单声明 enabled=false"
    return True, ""


# ---------------------------------------------------------------------------
# 类用户操作接口：交互面板桥（agent 线程安全）


class PluginUI:
    """app.request_ui/wait_ui 交互桥的插件包装：confirm/choose/line 三个用户动作原语。

    仅可在 agent 线程（hook / 工具执行内）调用——桥的应答由主线程帧循环完成，
    主线程命令 handler 里调用会阻塞帧循环造成死锁；主线程请直接用 app 的阻塞
    表单（app.read_line / app.show_sessions_form 等，见 /run /resume 的做法）。
    用户取消或运行时未注入（bind_runtime 之前）返回 None，不抛异常。
    """

    _YES = ("y", "yes", "是", "1")

    def __init__(self, ctx: "PluginContext"):
        self._ctx = ctx

    def _bridge(self, kind: str, payload: dict, cancelled) -> Any:
        from core.llm import CANCELLED

        app = _runtime.get("app")
        if app is None:
            self._ctx.log.warn("ctx.ui.%s 失败：app 未注入（运行时未就绪）", kind)
            return None
        req = app.request_ui(kind, payload)
        result = app.wait_ui(req, cancelled)
        return None if result == CANCELLED else result

    def confirm(self, prompt: str = "确认？", cancelled=None) -> Optional[bool]:
        """是/否确认面板。True/False；用户取消返回 None"""
        result = self.choose(["是", "否"], prompt=prompt, cancelled=cancelled)
        if result is None:
            return None
        return str(result).strip().lower() in self._YES

    def choose(self, options, prompt: str = "请选择", cancelled=None) -> Optional[str]:
        """选择菜单：返回所选项文本（按序号/文本匹配）；用户输入自由文本时原样返回；
        取消/未注入返回 None"""
        labels = [str(o) for o in (options or [])]
        if not labels:
            return None
        pairs = [(str(i + 1), label) for i, label in enumerate(labels)]
        result = self._bridge("choose", {"options": pairs, "prompt": prompt}, cancelled)
        if result is None:
            return None
        text = str(result).strip()
        if text.isdigit() and 1 <= int(text) <= len(labels):
            return labels[int(text) - 1]
        for label in labels:
            if text.lower() == label.lower():
                return label
        return text

    def line(self, prompt: str = "请输入", default: str = "", cancelled=None) -> Optional[str]:
        """单行输入面板：返回输入文本（空输入返回 default）；取消/未注入返回 None"""
        result = self._bridge("line", {"prompt": prompt, "config": self._ctx.config}, cancelled)
        if result is None:
            return None
        text = str(result).strip()
        return text or default


# ---------------------------------------------------------------------------
# 插件上下文（setup(ctx) 收到的 API 面）


class PluginContext:
    """传给插件 setup() 的能力面：注册 hooks/commands/tools/上下文补充/外部接口，
    以及类用户操作（submit_turn/notify/ctx.ui）。

    所有注册自动带 owner=插件 id，插件卸载时整体注销。
    """

    def __init__(self, pid: str, dir_path: Path, manifest: dict, source: str, config, workspace: Path):
        self.plugin_id = pid
        self.dir = dir_path
        self.manifest = manifest
        self.source = source  # global | project
        self.config = config  # 宿主 Config（只读约定；改配置走 /settings）
        self.workspace = workspace
        # plugin.json 的 config 声明（设置面板插件页与 ctx.settings 缺省值的依据）
        self.config_decls: List[dict] = _parse_config_decls(manifest, pid)
        self.log = get_logger(f"plugins.{pid}")
        self.ui = PluginUI(self)
        self._teardowns: List[Callable] = []  # register_teardown 登记，卸载时按序执行

    @property
    def settings(self) -> dict:
        """插件配置实时视图：声明缺省 + config.plugins_config[<id>] 持久化值（后者覆盖）。

        每次访问都重读宿主配置——设置面板里的改动无需重载即可被插件读到；
        需要动态行为的插件请在处理函数内读取本属性，而非在 setup 时缓存。"""
        data = (getattr(self.config, "data", None) or {})
        persisted = dict((data.get("plugins_config") or {}).get(self.plugin_id) or {})
        vals = {d["key"]: d["default"] for d in self.config_decls if d.get("default") is not None}
        vals.update(persisted)
        return vals

    # ---- hooks ----

    def register_hook(
        self, event: str, handler: Optional[Callable] = None, priority: int = 100, name: str = ""
    ):
        """注册 hook 处理函数（装饰器或直接传 fn）；语义见 core/hooks.EVENTS"""
        return hooks.register_hook(event, handler, priority=priority, owner=self.plugin_id, name=name)

    def register_context_supplement(self, fn: Callable[[dict], Any], priority: int = 100):
        """注册回合上下文补充：fn(payload) -> str|None；非 None 文本以 [插件补充] 注入"""
        return hooks.register_hook(
            "context_supplement", fn, priority=priority, owner=self.plugin_id, name="supplement"
        )

    def register_ui_tree(self, fn: Callable[[dict], Any], priority: int = 100):
        """注册每帧会话树节点回调；返回 TreeNode 或 TreeNode 列表。"""
        return hooks.register_hook("ui_tree_nodes", fn, priority=priority, owner=self.plugin_id, name="ui_tree")

    def register_ui_bottom(self, fn: Callable[[dict], Any], priority: int = 100):
        """注册每帧底部 ANSI 文本行回调；返回字符串或字符串列表。"""
        return hooks.register_hook("ui_bottom_rows", fn, priority=priority, owner=self.plugin_id, name="ui_bottom")

    def register_external_api(self, event: str, fn: Optional[Callable] = None, url: str = "", priority: int = 50):
        """插件侧实现 external_apis 同名扩展点（如 computer_use/memory_write）。
        传 fn 直接挂进程内处理函数；传 url 挂 HTTP POST 处理函数。"""
        if fn is not None:
            return hooks.register_hook(event, fn, priority=priority, owner=self.plugin_id, name="api")
        if url:
            return hooks.register_hook(
                event, hooks._make_http_handler(event, url), priority=priority, owner=self.plugin_id, name="api"
            )
        raise ValueError("register_external_api 需要 fn 或 url")

    def call(self, event: str, payload: Optional[dict] = None, default: Any = None) -> Any:
        """触发变换链事件（可与宿主/其他插件互通）"""
        return hooks.call_hook(event, payload, default=default)

    def collect(self, event: str, payload: Optional[dict] = None) -> List[Any]:
        """触发观察链事件，收集全部非 None 结果"""
        return hooks.collect_hook(event, payload)

    # ---- commands ----

    def register_command(
        self,
        name: str,
        hint: str = "",
        usage: str = "",
        aliases: Optional[List[str]] = None,
        handler: Optional[Callable] = None,
        completer: Optional[Callable] = None,
    ):
        """注册斜杠命令：handler(ctx, args)；completer(config, arg) -> [{name,hint}, ...]"""
        source = f"plugin:{self.plugin_id}"
        commands.register(name, hint=hint, usage=usage, aliases=aliases, handler=handler, source=source)
        if completer is not None:
            commands.register_arg_completer(name, completer, source=source)
        return handler

    def register_arg_completer(self, name: str, fn: Callable):
        """为任意命令（含内建）扩展参数补全；插件卸载时仅注销自己注册的补全器"""
        commands.register_arg_completer(name, fn, source=f"plugin:{self.plugin_id}")

    # ---- tools（模型可见）----

    def register_tool(
        self, name: Optional[str] = None, description: str = "", usage: str = "", schema: Optional[dict] = None
    ):
        """注册模型可见工具（进工具清单，走统一权限/确认/限额管线）；装饰器用法：
            @ctx.register_tool(name="my_tool", description="...", schema={...})
            def my_tool(**kwargs): ...
        """
        def decorator(func: Callable) -> Callable:
            register.register(name=name or func.__name__, description=description, usage=usage, schema=schema, owner=self.plugin_id)(func)
            return func

        return decorator

    # ---- 类用户操作 ----

    def submit_turn(self, text: str, images: Optional[List[str]] = None) -> str:
        """以用户语义提交消息及附件。

        空闲时开启回合，忙碌时整条消息排队。
        """
        text = (text or "").strip()
        if not text:
            return "[空消息，未提交]"
        runner = _runtime.get("runner")
        if runner is None:
            self.log.warn("submit_turn 失败：runner 未注入")
            return "[回合调度器未就绪，消息未提交]"
        submit = getattr(runner, "submit_message", None)
        if callable(submit):
            return submit(text, images)
        if images:
            return "[调度器不支持图片附件，消息未提交]"
        if runner.start(text):
            return "[已提交，新回合开始]"
        return runner.submit(text)

    def run_when_idle(self, callback: Callable) -> Any:
        """在调度器空闲锁内执行会话结构操作。"""
        runner = _runtime.get("runner")
        run = getattr(runner, "run_when_idle", None)
        if not callable(run):
            return {"ok": False, "message": "调度器不支持原子会话操作"}
        ok, result = run(callback)
        if not ok:
            return {"ok": False, "message": "回合进行中：请先停止后再操作"}
        return result

    def notify(self, text: str) -> None:
        """用户可见通知：写入会话系统消息（对话树可见，同命令反馈 _echo 的呈现），
        并刷新 UI 消息缓存（只改不渲染，帧循环统一上屏——agent 线程同款约定）"""
        text = (text or "").strip()
        if not text:
            return
        try:
            from core import memory as memory_mod

            session = memory_mod.get_session()
            if session is not None:
                session.add_message("system", text, type="help")
                app = _runtime.get("app")
                if app is not None:
                    app.refresh_from_session(session, render=False)
        except Exception as e:
            self.log.warn("notify 失败: %s", e)

    # ---- 杂项 ----

    def request_llm_retry(self) -> bool:
        """请求重试当前失败的 LLM 请求：仅当回合正处于 API 错误等待态（瞬态错误
        自动重试耗尽后，终端提示 Ctrl+Y 重试）时有效；返回是否成功触发。"""
        app = _runtime.get("app")
        if app is None:
            return False
        fn = getattr(app, "request_llm_retry", None)
        return bool(fn()) if callable(fn) else False

    def register_teardown(self, fn: Callable[[], Any]) -> Callable:
        """注册卸载回调：插件被卸载/重载/禁用回滚时按注册顺序执行（异常隔离）。

        用于停止 setup 期间启动的后台线程、释放端口/文件句柄等资源——没有它，
        /plugin reload 后旧线程会残留（模块级全局变量不跨重载保留，无法自行停止）。"""
        self._teardowns.append(fn)
        return fn

    def storage_dir(self) -> Path:
        """插件专属持久化目录 {workspace}/.bawcode/plugin-data/<id>/（自动创建）"""
        d = self.workspace / ".bawcode" / "plugin-data" / self.plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def __repr__(self) -> str:  # pragma: no cover
        return f"PluginContext({self.plugin_id}, dir={self.dir.name})"


# ---------------------------------------------------------------------------
# 插件依赖：首次装载时读 requirements.txt，缺失项经 pip 静默补齐


_REQ_FILE = "requirements.txt"
_PIP_TIMEOUT = 900  # pip 安装整体超时秒数，大型包留足下载时间
_NO_DEPS: Dict[str, Any] = {"status": "none", "missing": [], "error": ""}


def _auto_install_enabled(config) -> bool:
    """config.plugins.auto_install_deps，缺省 true；是否允许自动安装插件依赖"""
    cfg = (getattr(config, "data", None) or {}).get("plugins") or {}
    return bool(cfg.get("auto_install_deps", True))


def _read_requirements(dir_path: Path) -> List[str]:
    """读取 requirements.txt 的有效依赖行；跳过空行与注释，无文件返回 []"""
    req_file = dir_path / _REQ_FILE
    if not req_file.is_file():
        return []
    try:
        text = req_file.read_text(encoding="utf-8-sig")
    except OSError as e:
        log.warn("插件依赖文件读取失败 %s: %s", req_file, e)
        return []
    return [line for line in (raw.strip() for raw in text.splitlines()) if line and not line.startswith("#")]


def _split_requirement(line: str) -> tuple:
    """解析依赖行为 发行名与版本约束；非 PEP 508 行返回 None"""
    if _Requirement is not None:
        try:
            req = _Requirement(line)
        except Exception:
            return None, None
        if req.marker is not None:
            try:
                if not req.marker.evaluate():
                    return None, None  # 环境标记不适用，如 python_version<"3.8"
            except Exception:
                return None, None
        return req.name, req.specifier
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)  # 退化：仅取名字，不做版本判断
    return (m.group(1), None) if m else (None, None)


def _missing_requirements(reqs: List[str]) -> List[str]:
    """返回当前环境未满足的依赖行，含未安装与版本不符"""
    missing: List[str] = []
    for line in reqs:
        name, spec = _split_requirement(line)
        if not name:
            continue  # 非标准依赖行不参与检查，交由 pip 安装时处理
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(line)
            continue
        except Exception:
            missing.append(line)
            continue
        if spec is not None and not spec.contains(dist.version, prereleases=True):
            missing.append(line)
    return missing


def _pip_install(pid: str, req_file: Path) -> tuple:
    """经当前解释器的 pip 安装插件依赖文件；返回成功标志与输出尾部摘要"""
    cmd = [
        sys.executable, "-m", "pip", "install", "-r", str(req_file),
        "--disable-pip-version-check", "--no-input",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=_PIP_TIMEOUT)
    except FileNotFoundError as e:
        return False, f"无法启动 pip: {e}"
    except subprocess.TimeoutExpired:
        return False, f"pip 安装超时（>{_PIP_TIMEOUT}s）"
    except OSError as e:
        return False, f"pip 执行失败: {e}"
    out = "\n".join(x for x in ((proc.stdout or "") + (proc.stderr or "")).splitlines() if x.strip())
    log.debug("插件 %s pip 输出:\n%s", pid, out or "(空)")
    tail = "\n".join(out.splitlines()[-15:])
    if proc.returncode == 0:
        return True, tail
    return False, tail or f"pip 退出码 {proc.returncode}"


def _ensure_requirements(pid: str, dir_path: Path, config) -> dict:
    """检查并静默补齐插件目录 requirements.txt 声明的依赖。

    已满足则零开销跳过，只在缺失时调用一次 pip。返回记录：
      {"status": "none"|"ok"|"installed"|"manual"|"failed", "missing": [...], "error": str}
      none=无 requirements.txt；ok=依赖齐备；installed=已补装成功；
      manual=缺依赖但已关闭自动安装；failed=补装失败，不阻断装载，插件自行降级
    """
    none = {"status": "none", "missing": [], "error": ""}
    if not (dir_path / _REQ_FILE).is_file():
        return none
    reqs = _read_requirements(dir_path)
    if not reqs:
        return {"status": "ok", "missing": [], "error": ""}
    missing = _missing_requirements(reqs)
    if not missing:
        log.debug("插件 %s 依赖已满足（%d 项）", pid, len(reqs))
        return {"status": "ok", "missing": [], "error": ""}
    if not _auto_install_enabled(config):
        log.info("插件 %s 缺依赖 %d 项（已关闭自动安装）: %s", pid, len(missing), ", ".join(missing))
        return {"status": "manual", "missing": missing, "error": ""}
    log.info("插件 %s 缺依赖 %d 项，静默安装: %s", pid, len(missing), ", ".join(missing))
    ok, detail = _pip_install(pid, dir_path / _REQ_FILE)
    if not ok:
        log.warn("插件 %s 依赖安装失败: %s", pid, detail or "未知错误")
        return {"status": "failed", "missing": missing, "error": detail}
    importlib.invalidate_caches()  # 让本轮新装的发行版可见，复核是否真的补齐
    still = _missing_requirements(reqs)
    if still:
        log.warn("插件 %s 依赖安装后仍未满足: %s", pid, ", ".join(still))
        return {"status": "failed", "missing": still, "error": f"安装后仍未满足: {', '.join(still)}"}
    log.info("插件 %s 依赖已补齐（%d 项）", pid, len(missing))
    return {"status": "installed", "missing": missing, "error": ""}


# ---------------------------------------------------------------------------
# 装载 / 卸载


def _import_entry(pid: str, dir_path: Path, manifest: dict):
    entry = str(manifest.get("entry") or "main.py")
    entry_path = (dir_path / entry).resolve()
    if not entry_path.is_file():
        raise FileNotFoundError(f"入口文件不存在: {entry}")
    module_name = f"bawcode_plugin_{re.sub(r'[^A-Za-z0-9_]', '_', pid)}"
    if module_name in sys.modules:  # reload 场景残留
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法构造模块 spec: {entry_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # 追加而非插首：插件目录不得遮蔽标准库/宿主/其他插件的同名模块
    sys.path.append(str(dir_path))
    _runtime_paths.append(str(dir_path))
    try:
        spec.loader.exec_module(module)
    except Exception:
        # 失败插件的目录不得残留在 sys.path（半执行模块可能已改动过它，先判断再移除）
        sys.modules.pop(module_name, None)
        if str(dir_path) in sys.path:
            sys.path.remove(str(dir_path))
        _runtime_paths.remove(str(dir_path))
        raise
    return module, module_name


def _load_one(item: dict, config, workspace: Path) -> None:
    pid = item["id"]
    manifest = item["manifest"]
    # 配置声明先于启用判定解析：禁用/失败插件同样可在设置面板展开查看/编辑
    decls = _parse_config_decls(manifest, pid)
    if item.get("error"):
        # 清单读取/解析失败：以失败状态呈现真实原因，而非误报为"禁用"
        _loaded[pid] = {
            "manifest": manifest,
            "dir": item["dir"],
            "source": item["source"],
            "status": "failed",
            "error": item["error"],
            "context": None,
            "module": None,
            "config_decls": decls,
            "deps": dict(_NO_DEPS),
        }
        log.warn("插件清单异常 %s: %s", pid, item["error"])
        return
    enabled, reason = _enabled_of(manifest, config)
    if not enabled:
        _loaded[pid] = {
            "manifest": manifest,
            "dir": item["dir"],
            "source": item["source"],
            "status": "disabled",
            "error": reason,
            "context": None,
            "module": None,
            "config_decls": decls,
            "deps": dict(_NO_DEPS),
        }
        log.debug("插件跳过（%s）: %s", reason, pid)
        return
    # 依赖先于 import：requirements.txt 缺失项在此静默补齐，失败不阻断，插件可自行降级
    deps = _ensure_requirements(pid, item["dir"], config)
    ctx = PluginContext(pid, item["dir"], manifest, item["source"], config, workspace)
    record = {
        "manifest": manifest,
        "dir": item["dir"],
        "source": item["source"],
        "status": "loaded",
        "error": "",
        "context": ctx,
        "module": None,
        "deps": deps,
    }
    _loaded[pid] = record
    try:
        module, module_name = _import_entry(pid, item["dir"], manifest)
        record["module"] = module_name
        setup = getattr(module, "setup", None)
        if callable(setup):
            setup(ctx)
        else:
            # 无 setup 也允许（纯 skills/资源型插件），但提示一次
            log.debug("插件未定义 setup(ctx): %s", pid)
        # 记录注册量，供 /plugin 展示与诊断
        record["hooks"] = sum(1 for lst in hooks.list_hooks().values() for h in lst if h["owner"] == pid)
        record["tools"] = sum(1 for t in register.list_tools() if t.get("owner") == pid)
        record["commands"] = sum(1 for c in commands.list_commands() if c.get("source") == f"plugin:{pid}")
        record["configs"] = len(ctx.config_decls)
        log.info(
            "插件已装载: %s v%s（hooks=%s tools=%s commands=%s configs=%s）",
            pid,
            manifest.get("version"),
            record["hooks"],
            record["tools"],
            record["commands"],
            record["configs"],
        )
    except (Exception, SystemExit) as e:
        # SystemExit 不继承 Exception：插件 import 期 sys.exit() 不得击穿隔离
        # （否则后续插件与技能全部停装，且状态误标 loaded）；KeyboardInterrupt 保持穿透
        # 失败即回滚该插件已注册的一切与 sys.path 残留，错误隔离
        _unload_registrations(pid)
        record["status"] = "failed"
        record["error"] = f"{type(e).__name__}: {e}"
        log.warn("插件装载失败 %s: %s", pid, record["error"])


def _run_teardowns(pid: str) -> None:
    """执行插件登记的卸载回调（停止后台线程/释放资源；异常隔离不影响注销流程）"""
    ctx = (_loaded.get(pid) or {}).get("context")
    for fn in list(getattr(ctx, "_teardowns", []) or []):
        try:
            fn()
        except Exception as e:
            log.warn("插件卸载回调失败 %s: %s", pid, e)


def _unload_registrations(pid: str) -> None:
    """按 owner 注销插件在宿主各注册表的登记（hooks/commands/tools/skills/sys.path）"""
    _run_teardowns(pid)
    hooks.remove_owner(pid)
    commands.remove_source(f"plugin:{pid}")
    register.unregister_owner(pid)
    module_name = f"bawcode_plugin_{re.sub(r'[^A-Za-z0-9_]', '_', pid)}"
    sys.modules.pop(module_name, None)
    rec = _loaded.get(pid)
    if rec:
        dir_text = str(rec["dir"])
        if dir_text in sys.path:
            sys.path.remove(dir_text)
        if dir_text in _runtime_paths:
            _runtime_paths.remove(dir_text)


def _sync_plugin_skills(workspace: Path) -> None:
    """把已装载插件的 skills/ 目录并入技能系统（项目级同名仍最优先）"""
    dirs = [
        rec["dir"] / "skills"
        for rec in _loaded.values()
        if rec["status"] == "loaded" and (rec["dir"] / "skills").is_dir()
    ]
    skills.set_extra_dirs(dirs)
    skills.reset_loader()


def load(config, workspace: Optional[Path] = None) -> dict:
    """发现并装载全部插件（幂等：先卸载已有）。返回 {loaded, disabled, failed, total}"""
    global _last_workspace
    unload_all()
    ws = Path(workspace or Path.cwd())
    _last_workspace = ws
    items = discover(config, ws)
    for item in items:
        _load_one(item, config, ws)
    _sync_plugin_skills(ws)
    counts = summary()
    log.info(
        "插件装载完成: %d 装载 / %d 禁用 / %d 失败 / 共 %d", counts["loaded"], counts["disabled"], counts["failed"], counts["total"]
    )
    return counts


def unload_all() -> None:
    """卸载全部插件：按 owner 注销登记、清理 sys.modules/sys.path、清空插件技能目录"""
    for pid in list(_loaded.keys()):
        _unload_registrations(pid)
        record = _loaded.pop(pid, None)
        if record:
            log.debug("插件已卸载: %s", pid)
    for path in list(_runtime_paths):
        if path in sys.path:
            sys.path.remove(path)
    _runtime_paths.clear()
    skills.set_extra_dirs([])
    skills.reset_loader()


def reload(config, workspace: Optional[Path] = None) -> dict:
    """重载全部插件（重新发现 + 装载）；workspace 缺省复用最近一次 load 的值"""
    return load(config, workspace or _last_workspace)


def loaded_ids() -> List[str]:
    return [pid for pid, rec in _loaded.items() if rec["status"] == "loaded"]


def summary() -> dict:
    counts = {"loaded": 0, "disabled": 0, "failed": 0, "total": len(_loaded)}
    for rec in _loaded.values():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    return counts


def _deps_note(deps: Optional[dict]) -> str:
    """依赖状态在 /plugin 行内的简短标注；none 与 ok 不标注"""
    deps = deps or {}
    st = deps.get("status")
    missing = deps.get("missing") or []
    if st == "installed":
        return f" · 依赖已补装({len(missing)})"
    if st == "failed":
        return f" · 依赖安装失败: {', '.join(missing)}"
    if st == "manual":
        return f" · 依赖缺{len(missing)}项(未自动装)"
    return ""


def statuses() -> List[dict]:
    """诊断视图：id/名称/版本/来源/状态/错误/注册量/依赖"""
    rows = []
    for pid in sorted(_loaded.keys()):
        rec = _loaded[pid]
        manifest = rec["manifest"]
        rows.append(
            {
                "id": pid,
                "name": manifest.get("name") or pid,
                "version": manifest.get("version"),
                "description": manifest.get("description") or "",
                "source": rec["source"],
                "status": rec["status"],
                "error": rec.get("error") or "",
                "hooks": rec.get("hooks", 0),
                "tools": rec.get("tools", 0),
                "commands": rec.get("commands", 0),
                "configs": rec.get("configs", 0),
                "deps": rec.get("deps") or dict(_NO_DEPS),
            }
        )
    return rows


def status_listing() -> str:
    """/plugin 命令展示文本"""
    rows = statuses()
    if not rows:
        return "当前无插件。放置到 data/plugins/<id>/（含 plugin.json + main.py）后执行 /plugin reload。"
    label = {"loaded": "装载", "disabled": "禁用", "failed": "失败"}
    lines = ["插件:"]
    for r in rows:
        line = f"  {r['id']:20} v{r['version'] or '-':8} [{label.get(r['status'], r['status'])}] {r['source']}"
        if r["status"] == "loaded":
            line += f" · hooks={r['hooks']} tools={r['tools']} commands={r['commands']}"
            line += _deps_note(r.get("deps"))
        if r["description"]:
            line += f" · {r['description'][:40]}"
        lines.append(line)
        if r["error"]:
            lines.append(f"    └ {r['error']}")
        deps = r.get("deps") or {}
        if deps.get("status") == "failed" and deps.get("error"):
            last = (deps["error"] or "").splitlines()[-1]
            lines.append(f"    └ 依赖安装失败: {last}")
    hooks_view = hooks.list_hooks()
    hook_lines = [f"{ev}: {len(entries)}" for ev, entries in sorted(hooks_view.items()) if entries]
    if hook_lines:
        lines.append("扩展点: " + " · ".join(hook_lines))
    return "\n".join(lines)


def set_enabled(pid: str, enabled: bool, config) -> bool:
    """启停插件并持久化到 config.plugins.disable（写盘）；返回插件是否存在"""
    # statuses() 完全派生自 _loaded，存在性判断以此为准
    if pid not in _loaded:
        return False
    data = config.data
    plugins_cfg = data.setdefault("plugins", {})
    disable = list(plugins_cfg.get("disable") or [])
    if enabled and pid in disable:
        disable.remove(pid)
    elif not enabled and pid not in disable:
        disable.append(pid)
    plugins_cfg["disable"] = disable
    try:
        config.save()
    except Exception as e:
        log.warn("插件启停写配置失败: %s", e)
    return True


# ---------------------------------------------------------------------------
# 插件配置读写（设置面板"插件"标签页与 ctx.settings 的数据层）


def declared_configs(pid: str) -> List[dict]:
    """插件声明的配置项（已校验，含类型/缺省/选项/范围）；未装载返回 []。
    禁用/失败插件同样返回声明（记录级解析，供设置面板展开编辑）"""
    rec = _loaded.get(pid) or {}
    if rec.get("config_decls") is not None:
        return list(rec["config_decls"])
    ctx = rec.get("context")
    return list(getattr(ctx, "config_decls", []) or [])


def _persisted_settings(pid: str, config=None) -> dict:
    cfg_obj = config
    if cfg_obj is None:
        rec = _loaded.get(pid)
        cfg_obj = rec.get("context").config if rec and rec.get("context") else None
    data = (getattr(cfg_obj, "data", None) or {})
    return dict((data.get("plugins_config") or {}).get(pid) or {})


def config_values(pid: str, config=None) -> dict:
    """配置合并视图：声明缺省 < 持久化值（后者覆盖）"""
    vals = {d["key"]: d["default"] for d in declared_configs(pid) if d.get("default") is not None}
    vals.update(_persisted_settings(pid, config))
    return vals


def get_setting(pid: str, key: str, default: Any = None, config=None) -> Any:
    vals = config_values(pid, config)
    return vals[key] if key in vals else default


def set_setting(pid: str, key: str, value: Any, config) -> Any:
    """写入插件配置并持久化（config.plugins_config[<id>][<key>]，写盘）。

    有声明时按声明类型规整并钳制（list 必须命中 options，int/float 遵守
    min/max）；返回落盘后的实际值，无法规整时抛 ValueError。"""
    decl = next((d for d in declared_configs(pid) if d["key"] == key), None)
    if decl is not None:
        ctype = decl["type"]
        coerced = _coerce_decl_value(ctype, value)
        if coerced is None and value is not None:
            raise ValueError(f"插件 {pid} 配置 {key} 需要 {ctype} 类型，得到 {value!r}")
        value = coerced
        if ctype == "list" and value not in decl.get("options", []):
            raise ValueError(f"插件 {pid} 配置 {key} 必须是 {decl.get('options')} 之一，得到 {value!r}")
        if ctype in ("int", "float") and value is not None:
            if "min" in decl:
                value = max(decl["min"], value)
            if "max" in decl:
                value = min(decl["max"], value)
    pcfg = config.data.setdefault("plugins_config", {})
    pcfg.setdefault(pid, {})[key] = value
    try:
        config.save()
    except Exception as e:
        log.warn("插件配置写盘失败: %s", e)
    log.debug("插件配置已保存: %s.%s = %r", pid, key, value)
    return value


def reset() -> None:
    """测试辅助：清空运行态（含运行时载体绑定）"""
    global _last_workspace
    unload_all()
    _last_workspace = None
    _runtime.update({"app": None, "runner": None})
