"""Host-neutral service boundary. No BAWCode implementation modules are imported here."""
from typing import Any, Protocol


class HostAPI(Protocol):
    def invoke(self, service: str, operation: str, *args: Any, **kwargs: Any) -> Any: ...
    def value(self, service: str, name: str) -> Any: ...


# Explicit contracts: adapters may change internally without exposing entire modules.
SERVICE_METHODS = {
    "hooks": ("register_hook", "unregister_hook", "call_hook", "collect_hook", "list_hooks", "http_handler"),
    "commands": ("register", "register_arg_completer", "list_commands", "complete", "execute", "resolve"),
    "tools": ("register", "list_tools", "get_tool", "failure_hint"),
    "sessions": ("get_session", "content_text", "count_image_parts", "sessions_dir", "load_session_data", "list_sessions", "clear_session_artifacts"),
    "files": ("scan_candidates", "quote_reference", "read_persisted_content"),
    "plugins": ("statuses", "status_listing", "declared_configs", "get_setting", "set_setting", "set_enabled", "reload"),
    "workflows": ("active_name", "list_workflows", "load_workflow", "save_workflow", "delete_workflow",
                  "validate_workflow_full", "builtin_agents", "workflow_dir"),
    "snapshots": ("list_turns", "snapshot_cfg", "trash_count", "undo"),
    "agents": ("list_records", "status_label", "roles_listing", "load_specs", "get_specs",
               "save_role", "delete_role", "agents_dir"),
    "skills": ("get_loader",),
    "mcp": ("status_listing",),
    "models": ("make_model_name", "normalize_base_url"),
    "settings": ("fields", "read_field", "write_field"),
    "runtime": ("snapshot", "notify", "interrupt", "tree_node", "cancelled", "scope"),
    "logging": ("get_logger",),
    "ui": ("request_wait",),
}
SERVICE_VALUES = {
    "policy": ("MODE_LABELS", "MODE_CONFIRM", "MODES"),
    "files": ("MAX_SCAN_DEPTH",),
    "runtime": ("CANCELLED", "HOST_ROOT"),
}


class Service:
    """Only declared operations are reachable; private host attributes are not forwarded."""

    def __init__(self, host: HostAPI, name: str, owner: str = ""):
        self._host = host
        self._name = name
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        if name in SERVICE_METHODS.get(self._name, ()):
            def invoke(*args, **kwargs):
                return self._host.invoke(self._name, name, *args, **kwargs)
            return invoke
        if name in SERVICE_VALUES.get(self._name, ()):
            return self._host.value(self._name, name)
        raise AttributeError(f"Unknown SDK operation: {self._name}.{name}")

    def invoke_raw(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """绑定本服务一次的直通调用（供 HostServices 包装层使用）"""
        return self._host.invoke(self._name, operation, *args, **kwargs)


class _HooksService:
    """hooks 服务的 owner 感知包装：注册/注销缺省归属调用方插件。

    防两类事故：register_hook 忘传 owner → 卸载时残留；unregister_hook 只按
    handler/name 的宿主默认 OR 匹配 → 误注销别家插件的订阅。"""

    def __init__(self, service: Service, owner: str):
        self._service = service
        self._owner = owner

    def register_hook(self, event, handler=None, priority: int = 100, owner: str = "", name: str = ""):
        return self._service.invoke_raw(
            "register_hook", event, handler, priority=priority, owner=owner or self._owner, name=name
        )

    def unregister_hook(self, event, handler=None, owner: str = "", name: str = "", match_all: bool = False):
        # 缺省只注销本插件的订阅；显式传 owner 才允许跨插件（管理插件权责自负）
        return self._service.invoke_raw(
            "unregister_hook", event, handler, owner=owner or self._owner, name=name, match_all=True
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


class HostServices:
    """Advanced host services for management plugins, injected per context.

    服务名与服务集合一一对应：新增服务须同步在 SERVICE_METHODS/SERVICE_VALUES
    声明并在 __init__ 挂载，静态检查才能看到属性。"""

    hooks: Service
    commands: Service
    tools: Service
    sessions: Service
    files: Service
    plugins: Service
    workflows: Service
    snapshots: Service
    agents: Service
    skills: Service
    mcp: Service
    models: Service
    settings: Service
    runtime: Service
    logging: Service
    policy: Service
    ui: Service

    def __init__(self, host: HostAPI, owner: str = ""):
        for name in SERVICE_METHODS.keys() | SERVICE_VALUES.keys():
            service = Service(host, name, owner)
            if name == "hooks" and owner:
                service = _HooksService(service, owner)  # type: ignore[assignment]
            setattr(self, name, service)
