"""隔离插件加载器"""
import importlib
import importlib.util
import importlib.abc
import importlib.machinery
import sys
from pathlib import Path
from types import ModuleType

_legacy_paths = {}


class _SourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        return self.source_to_code(self.get_data(self.path), self.path)


class _PluginFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("bawcode_plugin_") or "." not in fullname:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = _SourceLoader(fullname, spec.origin)
        return spec


_finder = _PluginFinder()


def module_name(pid: str) -> str:
    return "bawcode_plugin_" + pid.encode("utf-8").hex()


def unload_entry(pid: str) -> None:
    prefix = module_name(pid)
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            sys.modules.pop(name, None)
    path = _legacy_paths.pop(pid, None)
    if path is not None and path in sys.path:
        sys.path.remove(path)


def import_entry(pid: str, dir_path: Path, manifest: dict):
    if _finder not in sys.meta_path:
        sys.meta_path.insert(0, _finder)
    root = dir_path.resolve()
    entry = str(manifest.get("entry") or "main.py")
    target = (root / entry).resolve()
    if not target.is_relative_to(root):
        raise ValueError("插件入口必须位于插件目录内")
    if not target.is_file():
        raise FileNotFoundError(f"入口文件不存在: {entry}")
    unload_entry(pid)
    prefix = module_name(pid)
    package = ModuleType(prefix)
    package.__path__ = [str(root)]
    package.__package__ = prefix
    package.__spec__ = importlib.util.spec_from_loader(prefix, loader=None, is_package=True)
    sys.modules[prefix] = package
    name = prefix + ".entry"
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        unload_entry(pid)
        raise ImportError(f"无法构造模块 spec: {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # sdk版本检查
    if "sdk_version" not in manifest and str(root) not in sys.path:
        sys.path.append(str(root))
        _legacy_paths[pid] = str(root)
    importlib.invalidate_caches()
    try:
        # 编译并执行插件入口模块，异常时卸载已加载的模块
        exec(compile(target.read_bytes(), str(target), "exec"), module.__dict__)
    except BaseException:
        unload_entry(pid)
        raise
    return module, name
