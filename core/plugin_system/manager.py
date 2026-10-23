"""插件生命周期、状态与持久化配置"""
from pathlib import Path
from typing import Any, Dict, List, Optional
from core import commands, hooks, register, skills
from core.log import get_logger
from bawcode_sdk import PluginContext
from bawcode_sdk.configuration import CONFIG_TYPES, _parse_config_decls, _coerce_decl_value
from .adapter import BAWCodeHost
from .discovery import discover, _enabled_of
from .dependencies import _ensure_requirements, _NO_DEPS
from .loader import import_entry as _import_entry, unload_entry
from .runtime import reset_runtime

log = get_logger("plugins")
_loaded: Dict[str, dict] = {}
_last_workspace: Optional[Path] = None


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
    ctx = PluginContext(pid, item["dir"], manifest, item["source"], config, workspace, host=BAWCodeHost())
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
            cleanup = setup(ctx)
            if callable(cleanup):
                ctx.register_teardown(cleanup)
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
        _unload_registrations(pid)
        record["status"] = "failed"
        record["error"] = f"{type(e).__name__}: {e}"
        log.warn("插件装载失败 %s: %s", pid, record["error"])


def _unload_registrations(pid: str) -> None:
    rec = _loaded.get(pid) or {}
    ctx = rec.get("context")
    if ctx is not None:
        ctx.close()
    hooks.remove_owner(pid)
    commands.remove_source(f"plugin:{pid}")
    register.unregister_owner(pid)
    unload_entry(pid)


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
    for pid in reversed(list(_loaded.keys())):
        _unload_registrations(pid)
        record = _loaded.pop(pid, None)
        if record:
            log.debug("插件已卸载: %s", pid)
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


# 插件配置读写

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
            raise ValueError(f"插件 {pid} 配置 {key} 必须是 {decl.get('options')} 之一，不能是 {value!r}")
        if ctype in ("int", "float") and value is not None:
            if "min" in decl:
                value = max(decl["min"], value)
            if "max" in decl:
                value = min(decl["max"], value)
    pcfg = config.data.setdefault("plugins_config", {})
    values = pcfg.setdefault(pid, {})
    missing = object()
    previous = values.get(key, missing)
    values[key] = value
    try:
        config.save()
    except Exception:
        if previous is missing:
            values.pop(key, None)
        else:
            values[key] = previous
        raise
    log.debug("插件配置已保存: %s.%s = %r", pid, key, value)
    return value


def reset() -> None:
    """测试辅助：清空运行态（含运行时载体绑定）"""
    global _last_workspace
    unload_all()
    _last_workspace = None
    reset_runtime()
