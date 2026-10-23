"""插件发现与清单读取"""
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from core.log import get_logger
from bawcode_sdk import API_VERSION

log = get_logger("plugins.discovery")
_ROOT = Path(__file__).resolve().parents[2]
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

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
    version = manifest.get("sdk_version", API_VERSION)
    if type(version) is not int or version != API_VERSION:
        return manifest, f"不支持 sdk_version={version!r}，宿主支持 {API_VERSION}"
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
