"""Compatibility facade for host callers; plugins should import bawcode_sdk."""
from bawcode_sdk import PluginContext as SDKPluginContext, PluginUI
from bawcode_sdk.configuration import CONFIG_TYPES
from .plugin_system.adapter import BAWCodeHost
from .plugin_system import runtime as _runtime_mod
from .plugin_system.runtime import bind_runtime, runtime
from .plugin_system.discovery import discover
from .plugin_system.manager import (
    load, unload_all, reload, loaded_ids, summary, statuses, status_listing,
    set_enabled, declared_configs, config_values, get_setting, set_setting, reset,
)

# Historical module-level binding retained for callers which used the old private API.
_runtime = _runtime_mod._runtime


class _LegacyHost(BAWCodeHost):
    def invoke(self, service, operation, *args, **kwargs):
        if service == "runtime" and operation == "snapshot":
            return dict(_runtime)
        if service == "runtime" and operation == "notify":
            app = _runtime.get("app")
            from core.memory import get_session
            session = get_session()
            if session is not None:
                session.add_message("system", args[0], type="help")
                if app is not None:
                    app.refresh_from_session(session, render=False)
            return None
        if service == "runtime" and operation == "interrupt":
            runner = _runtime.get("runner")
            fn = getattr(runner, "interrupt_turn", None)
            return bool(fn()) if callable(fn) else False
        return super().invoke(service, operation, *args, **kwargs)


class PluginContext(SDKPluginContext):
    """Legacy host-side constructor; the public SDK requires explicit host injection."""

    def __init__(self, pid, dir_path, manifest, source, config, workspace, *, host=None):
        super().__init__(pid, dir_path, manifest, source, config, workspace,
                         host=host if host is not None else _LegacyHost())

    def request_llm_retry(self):
        app = self.runtime.get("app") if self is not None else _runtime.get("app")
        fn = getattr(app, "request_llm_retry", None) if app is not None else None
        return bool(fn()) if callable(fn) else False

__all__ = [
    "PluginContext", "PluginUI", "CONFIG_TYPES", "discover", "bind_runtime", "runtime",
    "load", "unload_all", "reload", "loaded_ids", "summary", "statuses", "status_listing",
    "set_enabled", "declared_configs", "config_values", "get_setting", "set_setting", "reset",
]
