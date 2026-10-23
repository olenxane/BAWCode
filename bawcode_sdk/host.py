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
    "workflows": ("active_name", "list_workflows", "set_active", "workflow_enabled"),
    "snapshots": ("list_turns", "snapshot_cfg", "trash_count", "undo"),
    "agents": ("list_records", "status_label", "roles_listing"),
    "skills": ("get_loader",),
    "mcp": ("status_listing",),
    "models": ("make_model_name", "normalize_base_url"),
    "runtime": ("snapshot", "notify", "interrupt", "tree_node"),
    "logging": ("get_logger",),
}
SERVICE_VALUES = {
    "policy": ("MODE_LABELS", "MODE_CONFIRM", "MODES"),
    "files": ("MAX_SCAN_DEPTH",),
    "runtime": ("CANCELLED",),
}


class Service:
    """Only declared operations are reachable; private host attributes are not forwarded."""

    def __init__(self, host: HostAPI, name: str):
        self._host = host
        self._name = name

    def __getattr__(self, name: str) -> Any:
        if name in SERVICE_METHODS.get(self._name, ()):
            def invoke(*args, **kwargs):
                return self._host.invoke(self._name, name, *args, **kwargs)
            return invoke
        if name in SERVICE_VALUES.get(self._name, ()):
            return self._host.value(self._name, name)
        raise AttributeError(f"Unknown SDK operation: {self._name}.{name}")


class HostServices:
    """Advanced host services for management plugins, injected per context."""

    def __init__(self, host: HostAPI):
        for name in SERVICE_METHODS.keys() | SERVICE_VALUES.keys():
            setattr(self, name, Service(host, name))
