"""Public plugin context. All host calls go through an injected HostAPI."""
from pathlib import Path
from typing import Any, Callable, List, Optional
from .configuration import _parse_config_decls
from .host import HostAPI, HostServices
from .ui import PluginUI


class PluginContext:
    """传给插件 setup() 的能力面：注册 hooks/commands/tools/上下文补充/外部接口，
    以及类用户操作（submit_turn/notify/ctx.ui）。

    所有注册自动带 owner=插件 id，插件卸载时整体注销。
    """

    def __init__(self, pid: str, dir_path: Path, manifest: dict, source: str, config, workspace: Path, *, host: HostAPI):
        self.host = HostServices(host)
        self._closed = False
        self.plugin_id = pid
        self.dir = dir_path
        self.manifest = manifest
        self.source = source  # global | project因
        self.config = config  # 宿主 Config（只读约定；改配置走 /settings）
        self.workspace = workspace
        # plugin.json 的 config 声明（设置面板插件页与 ctx.settings 缺省值的依据）
        self.config_decls: List[dict] = _parse_config_decls(manifest, pid)
        self.log = self.host.logging.get_logger(f"plugins.{pid}")
        self.ui = PluginUI(self)
        self._teardowns: List[Callable] = []  # register_teardown 登记，卸载时按序执行

    @property
    def runtime(self) -> dict:
        """Advanced compatibility view of app/runner references; prefer higher-level methods."""
        return self.host.runtime.snapshot()

    @property
    def session(self):
        return self.host.sessions.get_session()

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_active(self) -> None:
        if self._closed:
            raise RuntimeError(f"Plugin {self.plugin_id} has been unloaded")

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> Any:
        self._ensure_active()
        return self.host.plugins.set_setting(self.plugin_id, key, value, self.config)

    def unregister_hook(self, event: str, handler: Optional[Callable] = None, name: str = "") -> int:
        return self.host.hooks.unregister_hook(event, handler, owner=self.plugin_id, name=name, match_all=True)

    def interrupt_turn(self) -> bool:
        return self.host.runtime.interrupt()

    def close(self) -> None:
        """Run cleanup exactly once, including after a failed setup."""
        if self._closed:
            return
        self._closed = True
        callbacks, self._teardowns = self._teardowns, []
        for fn in callbacks:
            try:
                fn()
            except (Exception, SystemExit) as exc:
                self.log.warning("Plugin cleanup failed: %s", exc)

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
        """注册 hook 处理函数（装饰器或直接传 fn）；语义见插件开发指南的事件目录"""
        def bind(fn):
            self._ensure_active()
            return self.host.hooks.register_hook(event, fn, priority=priority, owner=self.plugin_id, name=name)
        return bind(handler) if handler is not None else bind

    def register_context_supplement(self, fn: Callable[[dict], Any], priority: int = 100):
        """注册回合上下文补充：fn(payload) -> str|None；非 None 文本以 [插件补充] 注入"""
        self._ensure_active()
        return self.host.hooks.register_hook(
            "context_supplement", fn, priority=priority, owner=self.plugin_id, name="supplement"
        )

    def register_ui_tree(self, fn: Callable[[dict], Any], priority: int = 100):
        """注册每帧会话树节点回调；返回 TreeNode 或 TreeNode 列表。"""
        self._ensure_active()
        return self.host.hooks.register_hook("ui_tree_nodes", fn, priority=priority, owner=self.plugin_id, name="ui_tree")

    def register_ui_bottom(self, fn: Callable[[dict], Any], priority: int = 100):
        """注册每帧底部 ANSI 文本行回调；返回字符串或字符串列表。"""
        self._ensure_active()
        return self.host.hooks.register_hook("ui_bottom_rows", fn, priority=priority, owner=self.plugin_id, name="ui_bottom")

    def register_external_api(self, event: str, fn: Optional[Callable] = None, url: str = "", priority: int = 50):
        """插件侧实现 external_apis 同名扩展点（如 computer_use/memory_write）。
        传 fn 直接挂进程内处理函数；传 url 挂 HTTP POST 处理函数。"""
        self._ensure_active()
        if fn is not None:
            return self.host.hooks.register_hook(event, fn, priority=priority, owner=self.plugin_id, name="api")
        if url:
            return self.host.hooks.register_hook(
                event, self.host.hooks.http_handler(event, url), priority=priority, owner=self.plugin_id, name="api"
            )
        raise ValueError("register_external_api 需要 fn 或 url")

    def call(self, event: str, payload: Optional[dict] = None, default: Any = None) -> Any:
        """触发变换链事件（可与宿主/其他插件互通）"""
        return self.host.hooks.call_hook(event, payload, default=default)

    def collect(self, event: str, payload: Optional[dict] = None) -> List[Any]:
        """触发观察链事件，收集全部非 None 结果"""
        return self.host.hooks.collect_hook(event, payload)

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
        def bind(fn):
            self._ensure_active()
            source = f"plugin:{self.plugin_id}"
            result = self.host.commands.register(name, hint=hint, usage=usage, aliases=aliases, handler=fn, source=source)
            if completer is not None:
                self.host.commands.register_arg_completer(name, completer, source=source)
            return result
        return bind(handler) if handler is not None else bind

    def register_arg_completer(self, name: str, fn: Callable):
        """为任意命令（含内建）扩展参数补全；插件卸载时仅注销自己注册的补全器"""
        self._ensure_active()
        self.host.commands.register_arg_completer(name, fn, source=f"plugin:{self.plugin_id}")

    # ---- tools（模型可见）----

    def register_tool(
        self, name: Optional[str] = None, description: str = "", usage: str = "", schema: Optional[dict] = None
    ):
        """注册模型可见工具（进工具清单，走统一权限/确认/限额管线）；装饰器用法：
            @ctx.register_tool(name="my_tool", description="...", schema={...})
            def my_tool(**kwargs): ...
        """
        def decorator(func: Callable) -> Callable:
            self._ensure_active()
            self.host.tools.register(name=name or func.__name__, description=description, usage=usage, schema=schema, owner=self.plugin_id)(func)
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
        runner = self.runtime.get("runner")
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
        runner = self.runtime.get("runner")
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
        self.host.runtime.notify(text)

    # ---- 杂项 ----

    def request_llm_retry(self) -> bool:
        """请求重试当前失败的 LLM 请求：仅当回合正处于 API 错误等待态（瞬态错误
        自动重试耗尽后，终端提示 Ctrl+Y 重试）时有效；返回是否成功触发。"""
        app = self.runtime.get("app") if self is not None else None
        if app is None:
            return False
        fn = getattr(app, "request_llm_retry", None)
        return bool(fn()) if callable(fn) else False

    def register_teardown(self, fn: Callable[[], Any]) -> Callable:
        """注册卸载回调：插件被卸载/重载/禁用回滚时按注册顺序执行（异常隔离）。

        用于停止 setup 期间启动的后台线程、释放端口/文件句柄等资源——没有它，
        /plugin reload 后旧线程会残留（模块级全局变量不跨重载保留，无法自行停止）。"""
        self._ensure_active()
        self._teardowns.append(fn)
        return fn

    def storage_dir(self) -> Path:
        """插件专属持久化目录 {workspace}/.bawcode/plugin-data/<id>/（自动创建）"""
        d = self.workspace / ".bawcode" / "plugin-data" / self.plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def __repr__(self) -> str:  # pragma: no cover
        return f"PluginContext({self.plugin_id}, dir={self.dir.name})"
