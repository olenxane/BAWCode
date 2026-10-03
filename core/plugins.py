# 插件系统：把 hooks/commands/tools/skills/上下文补充 打包为可分发的目录插件。
#
# 目录（双层，与 skills 同范式）：
#   全局  data/plugins/<id>/          —— config.plugins.dir
#   项目  {workspace}/.bawcode/plugins/<id>/   —— 同名插件覆盖全局
# 插件结构：
#   plugin.json   清单（id 必填；entry 缺省 main.py；enabled 缺省 true）
#   main.py       入口模块，定义 setup(ctx)；ctx 提供 hooks/commands/tools/补充注册 API
#   skills/<n>/SKILL.md   可选，自动并入技能系统（项目级技能仍最高优先）
#
# 生命周期：启动 discover→逐个装载（错误隔离，失败不影响其他插件）；
# /plugin reload 卸载重载（按 owner 注销 hooks/commands/tools）；
# 禁用持久化到 config.plugins.disable。
#
# 信任模型：插件在本进程内以完整权限运行 Python 代码，只安装可信来源的插件。
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core import commands
from core import hooks
from core import register
from core import skills
from core.log import get_logger

log = get_logger("plugins")

_ROOT = Path(__file__).resolve().parent.parent

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

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
        data = (getattr(config, "data", None) or {})
        self.settings = dict((data.get("plugins_config") or {}).get(pid) or {})
        self.log = get_logger(f"plugins.{pid}")
        self.ui = PluginUI(self)

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

    def submit_turn(self, text: str) -> str:
        """以用户语义提交一条消息开启回合（等价主输入框发送）。

        idle 直接开新回合；busy 按设置 busy_send_mode 排队或中断在途回合
        （_AgentRunner.start/submit 原语义）。返回给用户看的状态说明；
        调度器未注入（启动早期）返回提示且不提交。"""
        text = (text or "").strip()
        if not text:
            return "[空消息，未提交]"
        runner = _runtime.get("runner")
        if runner is None:
            self.log.warn("submit_turn 失败：runner 未注入（启动早期/测试环境不可用）")
            return "[回合调度器未就绪，消息未提交]"
        if runner.start(text):
            return "[已提交，新回合开始]"
        return runner.submit(text)

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
                    try:
                        app.refresh_from_session(session, render=False)
                    except Exception:
                        pass
        except Exception as e:
            self.log.warn("notify 失败: %s", e)

    # ---- 杂项 ----

    def storage_dir(self) -> Path:
        """插件专属持久化目录 {workspace}/.bawcode/plugin-data/<id>/（自动创建）"""
        d = self.workspace / ".bawcode" / "plugin-data" / self.plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def __repr__(self) -> str:  # pragma: no cover
        return f"PluginContext({self.plugin_id}, dir={self.dir.name})"


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
    sys.path.insert(0, str(dir_path))
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
        }
        log.debug("插件跳过（%s）: %s", reason, pid)
        return
    ctx = PluginContext(pid, item["dir"], manifest, item["source"], config, workspace)
    record = {
        "manifest": manifest,
        "dir": item["dir"],
        "source": item["source"],
        "status": "loaded",
        "error": "",
        "context": ctx,
        "module": None,
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
        log.info(
            "插件已装载: %s v%s（hooks=%s tools=%s commands=%s）",
            pid,
            manifest.get("version"),
            record["hooks"],
            record["tools"],
            record["commands"],
        )
    except Exception as e:
        # 失败即回滚该插件已注册的一切，错误隔离
        _unload_registrations(pid)
        record["status"] = "failed"
        record["error"] = f"{type(e).__name__}: {e}"
        log.warn("插件装载失败 %s: %s", pid, record["error"])


def _unload_registrations(pid: str) -> None:
    """按 owner 注销插件在宿主各注册表的登记（hooks/commands/tools/skills）"""
    hooks.remove_owner(pid)
    commands.remove_source(f"plugin:{pid}")
    register.unregister_owner(pid)
    module_name = f"bawcode_plugin_{re.sub(r'[^A-Za-z0-9_]', '_', pid)}"
    sys.modules.pop(module_name, None)


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


def statuses() -> List[dict]:
    """诊断视图：id/名称/版本/来源/状态/错误/注册量"""
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
        if r["description"]:
            line += f" · {r['description'][:40]}"
        lines.append(line)
        if r["error"]:
            lines.append(f"    └ {r['error']}")
    hooks_view = hooks.list_hooks()
    hook_lines = [f"{ev}: {len(entries)}" for ev, entries in sorted(hooks_view.items()) if entries]
    if hook_lines:
        lines.append("扩展点: " + " · ".join(hook_lines))
    return "\n".join(lines)


def set_enabled(pid: str, enabled: bool, config) -> bool:
    """启停插件并持久化到 config.plugins.disable（写盘）；返回插件是否存在"""
    if pid not in _loaded and not any(r["id"] == pid for r in statuses()):
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


def reset() -> None:
    """测试辅助：清空运行态（含运行时载体绑定）"""
    global _last_workspace
    unload_all()
    _last_workspace = None
    _runtime.update({"app": None, "runner": None})
