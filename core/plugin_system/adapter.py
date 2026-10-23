"""插件系统适配器"""
import importlib
from bawcode_sdk.host import SERVICE_METHODS, SERVICE_VALUES

_MODULES = {
    "hooks": "hooks", "commands": "commands", "tools": "register",
    "sessions": "session_store", "files": "file", "plugins": "plugins",
    "workflows": "workflow", "snapshots": "snapshot", "agents": "subagent",
    "skills": "skills", "mcp": "mcp", "models": "config", "policy": "policy",
    "logging": "log",
}
_OVERRIDES = {
    ("hooks", "http_handler"): ("hooks", "_make_http_handler"),
    ("tools", "failure_hint"): ("tools", "tool_failure_hint"),
    ("sessions", "get_session"): ("memory", "get_session"),
    ("sessions", "content_text"): ("memory", "content_text"),
    ("sessions", "count_image_parts"): ("memory", "count_image_parts"),
    ("files", "read_persisted_content"): ("toolstore", "read_persisted_content"),
    ("runtime", "CANCELLED"): ("llm", "CANCELLED"),
    ("runtime", "tree_node"): ("render", "TreeNode"),
}


class BAWCodeHost:
    def _resolve(self, service, operation):
        module, name = _OVERRIDES.get((service, operation), (_MODULES.get(service), operation))
        if module is None:
            raise AttributeError(f"Unknown host service: {service}.{operation}")
        return getattr(importlib.import_module(f"core.{module}"), name)

    def invoke(self, service, operation, *args, **kwargs):
        if operation not in SERVICE_METHODS.get(service, ()):
            raise AttributeError(f"Unknown SDK operation: {service}.{operation}")
        if service == "runtime" and operation != "tree_node":
            from .runtime import runtime, notify, interrupt
            return {"snapshot": runtime, "notify": notify, "interrupt": interrupt}[operation](*args, **kwargs)
        return self._resolve(service, operation)(*args, **kwargs)

    def value(self, service, name):
        if name not in SERVICE_VALUES.get(service, ()):
            raise AttributeError(f"Unknown SDK value: {service}.{name}")
        return self._resolve(service, name)
