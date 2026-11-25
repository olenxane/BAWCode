#回合级文件快照与回滚（/undo）+ 项目回收站（delete_file / /clear-trash）
#快照管线行为：
#- 回合开始做轻量清单（路径+mtime+size，不含内容），回合末对账——execute_command 等
#  绕过写入工具的改动至少能在 /undo 里明确列出"无法自动还原"，不装作没发生
#- 两个文件写入工具（write 全量/位置、edit_file）+ delete_file 在改动前
#  capture_before 留底原始字节，tool 名记入索引——
#  这就是"成功的文件修改工具调用历史"（上下文回合末剥离会丢参数，必须在捕获时落索引）
#- 回退由程序确定性执行：逆操作重放对 edit 顺序/多处匹配敏感，write 覆盖
#  的参数里没有旧内容；还原前校验当前磁盘 md5 == 回合末 md5，用户事后改过的文件跳过；
#  还原后 ledger_reset() 强制模型重新 read（台账"安全优先"哲学）
#- 回收站：delete_file 把文件移入项目回收站（data/trash/{project_id}/，可配置），
#  /clear-trash 真正不可逆删除；shell 删除命令由 execute_command 工具拦截，
#  目标静默移入项目回收站而非真正删除
#- 不覆盖：execute_command 副作用（仅清单对账提示）、MCP 工具写文件、redo
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import toolstore
from core.log import get_logger

log = get_logger("snapshot")

_ROOT = Path(__file__).resolve().parent.parent

_SNAPSHOT_DEFAULTS = {
    "enabled": True,
    "dir": "data/snapshots",
    "trash_dir": "data/trash",
    "max_turns": 10,
    "max_file_bytes": 20 * 1024 * 1024,
    "inventory_max_files": 50000,
    "inventory_max_seconds": 32.0,
    "gc_days": 30,
}

# 单回合字节留底总量上限（与单文件上限并存，防大量小文件把磁盘写爆）
_TURN_BYTES_TOTAL_MAX = 256 * 1024 * 1024

# 清单遍历跳过的目录名（构建产物/VCS/工具自身数据，量大且无对账价值）
_INVENTORY_SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".bawcode", "data", "_recycle", "develop",
}
_EXTERNAL_LIST_CAP = 50  # 对账清单最多列出的外部改动条数

# 当前回合缓冲（线程本地）：begin_turn 置入、end_turn 取走，capture_before 写入工具落盘前留底
_CONTEXT = threading.local()
# 回收站路径的兜底上下文：线程未绑定 bound 时使用
_BOUND: Dict[str, Any] = {}
_LOCK = threading.Lock()


def _turn() -> Optional[dict]:
    return getattr(_CONTEXT, "turn", None)


def _bound() -> Dict[str, Any]:
    return getattr(_CONTEXT, "bound", None) or _BOUND


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def snapshot_cfg(config) -> dict:
    """配置解析：默认值 + 类型钳制集中一处"""
    data = dict(_SNAPSHOT_DEFAULTS)
    data.update((getattr(config, "data", None) or {}).get("snapshot") or {})
    data["enabled"] = bool(data.get("enabled", True))
    data["max_turns"] = max(1, _int(data.get("max_turns"), 10))
    data["max_file_bytes"] = max(1024, _int(data.get("max_file_bytes"), 20 * 1024 * 1024))
    data["inventory_max_files"] = max(0, _int(data.get("inventory_max_files"), 50000))
    try:
        data["inventory_max_seconds"] = max(0.0, float(data.get("inventory_max_seconds", 32.0)))
    except (TypeError, ValueError):
        data["inventory_max_seconds"] = 32.0
    data["gc_days"] = max(0, _int(data.get("gc_days"), 30))
    return data


def _base_dir(cfg: dict, key: str) -> Path:
    p = Path(str(cfg.get(key) or ""))
    return p if p.is_absolute() else _ROOT / p


def _snapshots_dir(cfg: dict, project_id: str, session_id: str) -> Path:
    return _base_dir(cfg, "dir") / (project_id or "_default") / (session_id or "_default")


def clear_session(config, project_id: str, session_id: str) -> dict:
    """清理当前项目/会话的回合快照，不触碰项目回收站。"""
    directory = _snapshots_dir(snapshot_cfg(config), project_id, session_id)
    if directory.is_symlink():
        try:
            directory.unlink()
            return {"ok": True, "removed": 1, "errors": []}
        except OSError as exc:
            return {"ok": False, "removed": 0, "errors": [str(exc)]}
    if not directory.exists():
        return {"ok": True, "removed": 0, "errors": []}
    removed = 0
    errors = []
    for path in list(directory.iterdir()):
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    try:
        directory.rmdir()
    except OSError as exc:
        if directory.exists():
            errors.append(f"目录: {exc}")
    return {"ok": not errors, "removed": removed, "errors": errors}


def _trash_base(cfg: dict, project_id: str) -> Path:
    return _base_dir(cfg, "trash_dir") / (project_id or "_default")


def _key(path: Path) -> str:
    # 与 _walk_inventory 的键保持同一口径：绝对路径规范化，但不逐文件 resolve()
    # （逐文件 resolve 是原扫描的主要开销，且符号链接场景下并不需要）。
    return os.path.normpath(os.path.abspath(str(path)))


def _file_md5(path: Path) -> Optional[str]:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _walk_inventory(
    root: Path, cap: int, max_seconds: float = 0.0
) -> Optional[Dict[str, Tuple[int, int]]]:
    """轻量清单：路径 → (mtime_ns, size)，不含内容；超文件数或时间预算返回 None。

    使用 os.scandir + entry.stat() 避免逐文件 Path.resolve()；这是回合末扫描
    与初始化后台扫描共用的实现。max_seconds=0 表示不限时。
    """
    if cap <= 0:
        return None
    root_abs = os.path.normpath(os.path.abspath(str(root)))
    inventory: Dict[str, Tuple[int, int]] = {}
    started = time.monotonic()
    checked = 0
    stack = [root_abs]
    try:
        while stack:
            if max_seconds > 0 and time.monotonic() - started >= max_seconds:
                return None
            cur = stack.pop()
            try:
                with os.scandir(cur) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in _INVENTORY_SKIP_DIRS:
                                    stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        inventory[os.path.normpath(entry.path)] = (st.st_mtime_ns, st.st_size)
                        if len(inventory) > cap:
                            return None
                        checked += 1
                        if checked % 256 == 0 and max_seconds > 0 and time.monotonic() - started >= max_seconds:
                            return None
            except OSError:
                continue
    except OSError:
        return None
    return inventory


# 清单缓存：会话初始化时后台扫描一次，回合末扫描后更新；begin_turn 只取缓存基线，
# 避免每回合开始/结束各扫一次。root 变化（切换工作区）时重新建立基线。
_INVENTORY_LOCK = threading.Lock()
_INVENTORY_CACHE: Dict[str, Any] = {
    "root": "",
    "inventory": None,
    "scanned_at": 0.0,
    "elapsed": 0.0,
    "running": False,
    "last_attempt": 0.0,
    "token": 0,
}
_INVENTORY_RETRY_SECONDS = 30.0


def _inventory_root() -> str:
    return os.path.normpath(os.path.abspath(os.getcwd()))


def _cached_inventory(root: str) -> Optional[Dict[str, Tuple[int, int]]]:
    with _INVENTORY_LOCK:
        if _INVENTORY_CACHE.get("root") == root:
            inventory = _INVENTORY_CACHE.get("inventory")
            if isinstance(inventory, dict):
                return inventory
    return None


def _update_inventory_cache(
    root: str,
    inventory: Optional[Dict[str, Tuple[int, int]]],
    elapsed: float,
    token: Optional[int] = None,
) -> None:
    with _INVENTORY_LOCK:
        current = int(_INVENTORY_CACHE.get("token") or 0)
        if token is not None and token != current:
            # 回合末同步扫描已经更新过缓存，迟到的后台扫描结果直接丢弃
            return
        _INVENTORY_CACHE.update({
            "root": root,
            "inventory": inventory,
            "scanned_at": time.time(),
            "elapsed": max(0.0, float(elapsed or 0.0)),
            "running": False,
            "last_attempt": time.monotonic(),
            # 同步扫描（token=None）递增 token，使仍在飞的后台结果失效
            "token": current + 1 if token is None else current,
        })


def _inventory_worker(root: str, cap: int, max_seconds: float, token: int) -> None:
    started = time.monotonic()
    inventory = _walk_inventory(Path(root), cap, max_seconds)
    elapsed = time.monotonic() - started
    _update_inventory_cache(root, inventory, elapsed, token=token)
    if inventory is None:
        log.info("快照清单异步扫描超时/超限，本会话外部改动对账将按回合重试（用时 %.2fs）", elapsed)
    else:
        log.info("快照清单异步扫描完成: %d 文件，用时 %.2fs", len(inventory), elapsed)


def prime_inventory(config) -> bool:
    """会话初始化时异步建立清单基线；已在扫描或已有基线时返回 False。"""
    cfg = snapshot_cfg(config)
    if not cfg.get("enabled") or not cfg.get("inventory_max_files"):
        return False
    root = _inventory_root()
    now = time.monotonic()
    with _INVENTORY_LOCK:
        if _INVENTORY_CACHE.get("root") == root:
            if isinstance(_INVENTORY_CACHE.get("inventory"), dict):
                return False
            if _INVENTORY_CACHE.get("running"):
                return False
            if now - float(_INVENTORY_CACHE.get("last_attempt") or 0.0) < _INVENTORY_RETRY_SECONDS:
                return False
        token = int(_INVENTORY_CACHE.get("token") or 0) + 1
        _INVENTORY_CACHE.update({
            "root": root,
            "inventory": None,
            "running": True,
            "last_attempt": now,
            "token": token,
        })
    threading.Thread(
        target=_inventory_worker,
        args=(root, int(cfg["inventory_max_files"]), float(cfg["inventory_max_seconds"]), token),
        name="baw-snapshot-inventory",
        daemon=True,
    ).start()
    return True


# ---------------------------------------------------------------------------
# 回合生命周期


def begin_turn(
    config, project_id: str, session_id: str, task: str = "", msg_index: int = -1,
    task_id: str = "", turn_id: str = "", revision: int = 0,
) -> None:
    """回合开始：设置线程本地捕获缓冲；子代理可传 task_id 得到独立快照序列。"""
    cfg = snapshot_cfg(config)
    bound = {"config": config, "project_id": str(project_id or "")}
    with _LOCK:
        _BOUND.update(bound)
    _CONTEXT.bound = bound
    _CONTEXT.turn = {
        "cfg": cfg,
        "project_id": str(project_id or ""),
        "session_id": str(session_id or ""),
        "turn_id": str(turn_id or ""),
        "revision": _int(revision, 0),
        "task": " ".join(str(task or "").split())[:120],
        "task_messages": [{"source": "initial", "text": str(task or "")}],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "msg_index": _int(msg_index, -1),
        "task_id": str(task_id or ""),
        "files": {},
        "inventory": None,
        "bytes_total": 0,
    }
    if cfg["enabled"] and not task_id:
        root = _inventory_root()
        baseline = _cached_inventory(root)
        if baseline is None:
            started = prime_inventory(config)
            log.info(
                "快照清单基线未就绪，本回合跳过外部改动对账%s",
                "（后台扫描中）" if started else "",
            )
        else:
            _CONTEXT.turn["inventory"] = baseline
            log.debug("快照清单基线就绪: %d 文件", len(baseline))
    elif cfg["enabled"] and task_id:
        log.debug("子代理回合跳过外部改动对账")


def add_task_message(text: str, source: str = "inject") -> None:
    """记录实际并入当前回合的用户消息，不改写快照归属。"""
    turn = _turn()
    if turn is not None:
        turn["task_messages"].append({"source": str(source or "inject"), "text": str(text or "")})


def _capture_checkpoint(turn: dict, rec: dict, path: Path, tool: str) -> None:
    existed = path.exists()
    data = None
    size = 0
    skip_reason = ""
    if existed:
        try:
            stat = path.stat()
            size = stat.st_size
        except OSError as exc:
            rec["checkpoints"].append({"skip_reason": f"读取失败: {exc}"})
            return
        limit = turn["cfg"]["max_file_bytes"]
        if size > limit or turn["bytes_total"] + size > _TURN_BYTES_TOTAL_MAX:
            signature = (True, f"oversize:{stat.st_mtime_ns}:{size}")
            skip_reason = f"检查点文件 {size} 字节超限未留底"
        else:
            try:
                data = path.read_bytes()
            except OSError as exc:
                rec["checkpoints"].append({"skip_reason": f"读取失败: {exc}"})
                return
            signature = (True, hashlib.md5(data).hexdigest())
    else:
        signature = (False, "")
    if signature == rec["checkpoint_signature"]:
        return
    before_signature = rec["checkpoint_signature"]
    checkpoint = {
        "existed": existed,
        "before_existed": before_signature[0],
        "before_md5": before_signature[1] if len(before_signature[1]) == 32 else "",
        "after_md5": signature[1] if len(signature[1]) == 32 else "",
        "bytes": data,
        "md5": signature[1] if len(signature[1]) == 32 else "",
        "size": size,
        "tool": str(tool or ""),
        "time": time.strftime("%H:%M:%S"),
        "skip_reason": skip_reason,
    }
    if data is not None:
        turn["bytes_total"] += size
    rec["checkpoints"].append(checkpoint)
    rec["checkpoint_signature"] = signature


def capture_before(path: Path, tool: str = "") -> None:
    """写入工具落盘前调用：首份原始字节作为回合起点，后续版本形成有界检查点。"""
    turn = _turn()
    if turn is None or not turn["cfg"]["enabled"]:
        return
    key = _key(path)
    with _LOCK:
        existing = turn["files"].get(key)
        if existing is not None:
            _capture_checkpoint(turn, existing, path, tool)
            return
        rec: Dict[str, Any] = {
            "path": key,
            "existed_before": False,
            "bytes": None,
            "tool": str(tool or ""),
            "time": time.strftime("%H:%M:%S"),
            "skip_reason": "",
            "checkpoints": [],
            "checkpoint_signature": (False, ""),
        }
        if path.exists():
            rec["existed_before"] = True
            mtime_ns = 0
            try:
                stat = path.stat()
                size = stat.st_size
                mtime_ns = stat.st_mtime_ns
            except OSError as exc:
                size = 0
                rec["skip_reason"] = f"读取失败: {exc}"
            limit = turn["cfg"]["max_file_bytes"]
            if rec["skip_reason"]:
                rec["checkpoint_signature"] = (True, f"unreadable:{size}")
            elif size > limit or turn["bytes_total"] + size > _TURN_BYTES_TOTAL_MAX:
                rec["skip_reason"] = f"文件 {size} 字节超限未留底"
                rec["checkpoint_signature"] = (True, f"oversize:{mtime_ns}:{size}")
            else:
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    data = None
                    rec["skip_reason"] = f"读取失败: {exc}"
                if data is not None:
                    rec["checkpoint_signature"] = (True, hashlib.md5(data).hexdigest())
                    rec["bytes"] = data
                    turn["bytes_total"] += size
        turn["files"][key] = rec


@contextmanager
def bind_context(config, project_id: str, session_id: str, task_id: str = ""):
    """在工具线程绑定回合快照上下文；子代理任务用独立序列避免混入主回合 /undo。"""
    previous_bound = getattr(_CONTEXT, "bound", None)
    _CONTEXT.bound = {"config": config, "project_id": str(project_id or "")}
    begin_turn(config, project_id, session_id, task=f"后台子代理 {task_id}", task_id=task_id)
    try:
        yield
    finally:
        end_turn()
        _CONTEXT.bound = previous_bound


def end_turn() -> Optional[dict]:
    """结束当前线程快照：有捕获或外部改动则落盘，空回合无副作用。"""
    turn = _turn()
    _CONTEXT.turn = None
    if turn is None:
        return None
    cfg = turn["cfg"]
    session_dir = turn.get("task_id") or turn["session_id"]
    seq_dir = _next_seq_dir(cfg, turn["project_id"], session_dir)
    seq = int(seq_dir.name)

    entries: List[dict] = []
    for i, rec in enumerate(turn["files"].values()):
        path = Path(rec["path"])
        entry = {
            "path": rec["path"],
            "existed_before": rec["existed_before"],
            "captured": not rec["skip_reason"],
            "tool": rec["tool"],
            "time": rec["time"],
            "skip_reason": rec["skip_reason"],
            "blob": "",
            "md5_after": "",
            "size_after": 0,
            "existed_after": path.exists(),
            "checkpoints": [],
        }
        if rec["bytes"] is not None:
            entry["blob"] = f"{i:03d}.bin"
        if path.exists():
            entry["md5_after"] = _file_md5(path) or ""
            try:
                entry["size_after"] = path.stat().st_size
            except OSError:
                pass
        entries.append(entry)
        if entry["blob"]:
            _atomic_write_bytes(seq_dir / entry["blob"], rec["bytes"])
        for checkpoint_id, checkpoint in enumerate(rec["checkpoints"], start=1):
            checkpoint_entry = {
                "id": checkpoint_id,
                "existed": checkpoint.get("existed", False),
                "before_existed": checkpoint.get("before_existed", False),
                "before_md5": checkpoint.get("before_md5", ""),
                "after_md5": checkpoint.get("after_md5", checkpoint.get("md5", "")),
                "md5": checkpoint.get("md5", ""),
                "size": checkpoint.get("size", 0),
                "tool": checkpoint.get("tool", ""),
                "time": checkpoint.get("time", ""),
                "skip_reason": checkpoint.get("skip_reason", ""),
                "blob": "",
            }
            if checkpoint.get("bytes") is not None:
                checkpoint_entry["blob"] = f"checkpoints/{i:03d}-{checkpoint_id:03d}.bin"
                _atomic_write_bytes(seq_dir / checkpoint_entry["blob"], checkpoint["bytes"])
            entry["checkpoints"].append(checkpoint_entry)

    external: List[str] = []
    baseline = turn["inventory"]
    current = None
    if cfg["enabled"] and not turn.get("task_id"):
        root = _inventory_root()
        scan_started = time.monotonic()
        current = _walk_inventory(Path(root), cfg["inventory_max_files"], cfg["inventory_max_seconds"])
        elapsed = time.monotonic() - scan_started
        _update_inventory_cache(root, current, elapsed)
        if elapsed >= 2.0:
            log.info(
                "回合末清单扫描: %d 文件，用时 %.2fs%s",
                len(current or {}), elapsed, "（超时/超限）" if current is None else "",
            )
    if baseline is not None and current is not None:
        captured = set(turn["files"])
        # 自身产物不算外部改动：本回合刚写的 blob、移入回收站的文件
        # （前缀比对带分隔符边界，防 project_id 互为前缀时误判）
        snap_prefix = _key(_snapshots_dir(cfg, turn["project_id"], session_dir)) + os.sep
        trash_prefix = _key(_trash_base(cfg, turn["project_id"])) + os.sep
        for key, (mtime, size) in current.items():
            before = baseline.get(key)
            if key in captured or before == (mtime, size):
                continue
            if key.startswith(snap_prefix) or key.startswith(trash_prefix):
                continue
            external.append(key)
            if len(external) >= _EXTERNAL_LIST_CAP:
                external.append("…（其余略）")
                break
        # 已消失的文件也属于外部改动
        for key in baseline.keys() - current.keys():
            if len(external) >= _EXTERNAL_LIST_CAP:
                if external[-1] != "…（其余略）":
                    external.append("…（其余略）")
                break
            if key not in captured and not key.startswith((snap_prefix, trash_prefix)):
                external.append(key)

    if not entries and not external:
        return None  # 空回合不产生任何文件

    index = {
        "version": 2,
        "seq": seq,
        "time": turn["time"],
        "task": turn["task"],
        "task_messages": turn["task_messages"],
        "turn_id": turn["turn_id"],
        "session_id": turn["session_id"],
        "revision": turn["revision"],
        "msg_index": turn["msg_index"],
        "files": entries,
        "external_changes": external,
    }
    _atomic_write_bytes(seq_dir / "index.json", json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8"))
    _prune_old_turns(cfg, turn["project_id"], session_dir, cfg["max_turns"])
    log.info("回合快照已落盘: #%d（%d 个文件，外部改动 %d）", seq, len(entries), len(external))
    return {
        "seq": seq,
        "session_id": turn["session_id"],
        "turn_id": turn["turn_id"],
        "revision": turn["revision"],
        "task_messages": list(turn["task_messages"]),
        "files": len(entries),
        "external_changes": external,
    }


def _next_seq_dir(cfg: dict, project_id: str, session_id: str) -> Path:
    base = _snapshots_dir(cfg, project_id, session_id)
    seq = 1
    if base.is_dir():
        for child in base.iterdir():
            if child.is_dir() and child.name.isdigit():
                seq = max(seq, int(child.name) + 1)
    return base / f"{seq:04d}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(data)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _prune_old_turns(cfg: dict, project_id: str, session_id: str, keep: int) -> None:
    base = _snapshots_dir(cfg, project_id, session_id)
    if not base.is_dir():
        return
    seq_dirs = sorted((c for c in base.iterdir() if c.is_dir() and c.name.isdigit()), key=lambda c: int(c.name))
    for child in seq_dirs[:-keep] if len(seq_dirs) > keep else []:
        shutil.rmtree(child, ignore_errors=True)


# ---------------------------------------------------------------------------
# 启动回收：快照目录按最后活跃时间过期


def gc(config) -> dict:
    """启动时回收过期回合快照：每个会话目录取 max(会话json mtime, 快照目录mtime)
    为最后活跃时间，超 gc_days 天整目录删除；gc_days=0 禁用。异常不阻塞启动。

    双信号兜底孤儿目录：会话文件被删后无 json 可参照，回落到快照目录自身
    mtime（末次落盘时间）；反向场景（会话活跃但快照目录更旧）不会误删。
    """
    result = {"removed": 0, "freed_bytes": 0}
    try:
        cfg = snapshot_cfg(config)
        days = _int(cfg.get("gc_days"), 0)
        if days <= 0:
            return result
        from core import session_store

        root = _base_dir(cfg, "dir")
        if not root.is_dir():
            return result
        cutoff = time.time() - days * 86400
        for project_dir in list(root.iterdir()):
            if not project_dir.is_dir():
                continue
            sessions_root = session_store.sessions_dir(config, project_dir.name)
            for sid_dir in list(project_dir.iterdir()):
                try:
                    if sid_dir.is_symlink():
                        if sid_dir.lstat().st_mtime < cutoff:
                            sid_dir.unlink()
                            result["removed"] += 1
                        continue
                    if not sid_dir.is_dir():
                        continue
                    session_file = sessions_root / f"{sid_dir.name}.json"
                    last_active = sid_dir.stat().st_mtime
                    if session_file.is_file():
                        last_active = max(last_active, session_file.stat().st_mtime)
                    if last_active < cutoff:
                        shutil.rmtree(sid_dir, ignore_errors=True)
                        result["removed"] += 1
                        log.info("快照回收：会话 %s/%s 过期（最后活跃 %.0f 天前），目录已删除",
                                 project_dir.name, sid_dir.name,
                                 (time.time() - last_active) / 86400)
                except OSError as exc:
                    log.warn("快照回收失败 %s: %s", sid_dir, exc)
    except Exception as exc:
        log.warn("快照回收异常（忽略）: %r", exc)
    if result["removed"]:
        log.info("快照启动回收完成：%d 个过期会话目录已删除", result["removed"])
    return result


def ensure_resume_baseline(config, project_id: str, session_id: str) -> Optional[Path]:
    """恢复的会话无任何回合快照时合成 0001 基线索引（files 为空），
    使过期清理后恢复的会话仍有一个"恢复时刻"的 /undo 锚点。
    已有快照（含非数字目录如子代理任务目录）时不动作；异常吞掉返回 None。"""
    try:
        cfg = snapshot_cfg(config)
        if not cfg.get("enabled"):
            return None
        base = _snapshots_dir(cfg, project_id, session_id)
        if base.is_dir() and any(c.is_dir() and c.name.isdigit() for c in base.iterdir()):
            return None
        seq_dir = base / "0001"
        index = {
            "version": 2,
            "seq": 1,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task": "（恢复基线：过期快照已清理，以此为还原锚点）",
            "task_messages": [],
            "turn_id": "",
            "session_id": str(session_id or ""),
            "revision": 0,
            "msg_index": -1,
            "files": [],
            "external_changes": [],
        }
        _atomic_write_bytes(seq_dir / "index.json", json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8"))
        log.info("恢复基线快照已建立: %s", seq_dir)
        return seq_dir
    except OSError as exc:
        log.warn("恢复基线快照建立失败: %r", exc)
        return None


# ---------------------------------------------------------------------------
# /undo 回滚


def list_turns(config, project_id: str, session_id: str) -> List[dict]:
    """按序号升序列出本会话已落盘的回合快照（读 index.json 摘要）"""
    cfg = snapshot_cfg(config)
    base = _snapshots_dir(cfg, project_id, session_id)
    turns = []
    if base.is_dir():
        # 序号目录按数值排序：零填充 4 位，≥10000 后字典序不再等于数值序
        for child in sorted((c for c in base.iterdir() if c.name.isdigit()), key=lambda c: int(c.name)):
            index_path = child / "index.json"
            if not (child.is_dir() and index_path.exists()):
                continue
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            files = index.get("files") or []
            restorable = sum(
                1 for f in files
                if f.get("captured") or any(
                    not cp.get("skip_reason") and (not cp.get("existed") or cp.get("blob"))
                    for cp in f.get("checkpoints") or []
                )
            )
            checkpoint_details = [
                {
                    "id": cp.get("id"),
                    "path": str(file.get("path") or ""),
                    "tool": str(cp.get("tool") or ""),
                    "time": str(cp.get("time") or ""),
                    "restorable": not cp.get("skip_reason") and (not cp.get("existed") or bool(cp.get("blob"))),
                    "skip_reason": str(cp.get("skip_reason") or ""),
                }
                for file in files for cp in file.get("checkpoints") or []
            ]
            turns.append(
                {
                    "seq": int(child.name),
                    "time": str(index.get("time") or ""),
                    "task": str(index.get("task") or ""),
                    "turn_id": str(index.get("turn_id") or ""),
                    "session_id": str(index.get("session_id") or session_id),
                    "revision": _int(index.get("revision"), 0),
                    "task_messages": list(index.get("task_messages") or []),
                    "files": len(files),
                    "restorable": restorable,
                    "checkpoints": checkpoint_details,
                    "dir": str(child),
                }
            )
    return turns


def undo(
    config, project_id: str, session_id: str, which: Any = "last", checkpoint: Optional[int] = None
) -> Optional[dict]:
    """回滚指定回合；可选 checkpoint 恢复同回合中间版本，默认恢复回合起点。"""
    if which != "last":
        if isinstance(which, bool) or not (isinstance(which, int) or isinstance(which, str) and which.isdigit()):
            return None
        which = int(which)
        if which <= 0:
            return None
    if checkpoint is not None:
        if isinstance(checkpoint, bool) or not (isinstance(checkpoint, int) or isinstance(checkpoint, str) and checkpoint.isdigit()):
            return None
        checkpoint = int(checkpoint)
        if checkpoint <= 0:
            return None
    cfg = snapshot_cfg(config)
    base = _snapshots_dir(cfg, project_id, session_id)
    if which == "last":
        turns = list_turns(config, project_id, session_id)
        if not turns:
            return None
        turn_dir = base / f"{turns[-1]['seq']:04d}"
    else:
        turn_dir = base / f"{_int(which, 0):04d}"
        if not (turn_dir / "index.json").exists():
            return None
    try:
        index = json.loads((turn_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warn("快照索引读取失败 %s: %s", turn_dir, e)
        return None
    if checkpoint is not None and not any(
        _int(item.get("id"), 0) == checkpoint
        for entry in index.get("files") or []
        for item in entry.get("checkpoints") or []
    ):
        return None

    restored, deleted, skipped = _restore_entries(turn_dir, index, checkpoint=checkpoint)
    toolstore.ledger_reset()
    external_changes = list(index.get("external_changes") or [])
    result = {
        "seq": int(turn_dir.name),
        "task": str(index.get("task") or ""),
        "turn_id": str(index.get("turn_id") or ""),
        "session_id": str(index.get("session_id") or session_id),
        "revision": _int(index.get("revision"), 0),
        "task_messages": list(index.get("task_messages") or []),
        "checkpoint": checkpoint,
        "status": "partial" if skipped or external_changes else "complete",
        "external_changes_action": "not_rolled_back",
        "restored": restored,
        "deleted": deleted,
        "skipped": skipped,
        "external_changes": external_changes,
    }
    log.info(
        "回滚回合 #%d: 还原%d 删除%d 跳过%d", result["seq"], len(restored), len(deleted), len(skipped)
    )
    return result


def _restore_entries(
    turn_dir: Path, index: dict, checkpoint: Optional[int] = None
) -> Tuple[List[str], List[str], List[Tuple[str, str]]]:
    """按 index.json 还原单个回合；不做 ledger_reset（调用方统一）。"""
    restored: List[str] = []
    deleted: List[str] = []
    skipped: List[Tuple[str, str]] = []
    for entry in reversed(index.get("files") or []):
        path = Path(entry["path"])
        if checkpoint is not None:
            checkpoint_entry = next(
                (item for item in entry.get("checkpoints") or [] if _int(item.get("id"), 0) == checkpoint),
                None,
            )
            if checkpoint_entry is None:
                continue
            existed_after = entry.get("existed_after", bool(entry.get("md5_after")))
            if path.exists() != existed_after or path.exists() and _file_md5(path) != entry.get("md5_after"):
                skipped.append((entry["path"], "事后已修改或删除，未还原"))
                continue
            if checkpoint_entry.get("skip_reason"):
                skipped.append((entry["path"], checkpoint_entry["skip_reason"]))
                continue
            if not checkpoint_entry.get("existed"):
                if not path.exists():
                    skipped.append((entry["path"], "已处于该检查点状态"))
                else:
                    try:
                        path.unlink()
                        deleted.append(entry["path"])
                    except OSError as exc:
                        skipped.append((entry["path"], f"检查点删除失败: {exc}"))
                continue
            try:
                checkpoint_bytes = (turn_dir / checkpoint_entry["blob"]).read_bytes()
                _atomic_write_bytes(path, checkpoint_bytes)
                restored.append(entry["path"])
            except (OSError, KeyError) as exc:
                skipped.append((entry["path"], f"检查点读取或还原失败: {exc}"))
            continue
        if not entry.get("captured"):
            skipped.append((entry["path"], entry.get("skip_reason") or "未留底"))
            continue
        if not entry.get("existed_before"):
            # 回合新建的文件：当前内容与回合末一致才删（被事后改过则不动）
            if not path.exists():
                skipped.append((entry["path"], "已不存在"))
            elif _file_md5(path) == entry.get("md5_after"):
                try:
                    path.unlink()
                    deleted.append(entry["path"])
                except OSError as e:
                    skipped.append((entry["path"], f"删除失败: {e}"))
            else:
                skipped.append((entry["path"], "事后已修改，未删除"))
            continue
        if not entry.get("blob"):
            skipped.append((entry["path"], entry.get("skip_reason") or "未留底"))
            continue
        blob = turn_dir / entry["blob"]
        try:
            original = blob.read_bytes()
        except OSError as e:
            skipped.append((entry["path"], f"留底读取失败: {e}"))
            continue
        # 旧索引以回合末哈希判断文件是否存在
        existed_after = entry.get("existed_after", bool(entry.get("md5_after")))
        if path.exists() != existed_after or path.exists() and _file_md5(path) != entry.get("md5_after"):
            skipped.append((entry["path"], "事后已修改或删除，未还原"))
            continue
        try:
            _atomic_write_bytes(path, original)
            restored.append(entry["path"])
        except OSError as e:
            skipped.append((entry["path"], f"还原失败: {e}"))
    return restored, deleted, skipped


def turns_from_msg_index(config, project_id: str, session_id: str, threshold: int) -> List[dict]:
    """msg_index ≥ threshold 的已落盘回合（seq 降序）：树模式 Ctrl+Z 用它定位
    "被移除回合"对应的文件快照（msg_index=回合用户消息的绝对序号）"""
    cfg = snapshot_cfg(config)
    base = _snapshots_dir(cfg, project_id, session_id)
    out: List[dict] = []
    if not base.is_dir():
        return out
    for child in base.iterdir():
        index_path = child / "index.json"
        if not (child.is_dir() and child.name.isdigit() and index_path.exists()):
            continue
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _int(index.get("msg_index"), -1) >= threshold:
            out.append(
                {
                    "seq": int(child.name),
                    "dir": str(child),
                    "time": str(index.get("time") or ""),
                    "task": str(index.get("task") or ""),
                    "msg_index": _int(index.get("msg_index"), -1),
                    "files": len(index.get("files") or []),
                }
            )
    out.sort(key=lambda t: t["seq"], reverse=True)
    return out


def restore_turn(config, project_id: str, session_id: str, seq: int) -> Optional[dict]:
    """还原指定序号回合的文件快照（树模式 Ctrl+Z 联动用）；不做 ledger_reset——
    多回合批量还原后由调用方统一调一次。无该回合返回 None"""
    cfg = snapshot_cfg(config)
    turn_dir = _snapshots_dir(cfg, project_id, session_id) / f"{_int(seq, 0):04d}"
    try:
        index = json.loads((turn_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warn("快照索引读取失败 %s: %s", turn_dir, e)
        return None
    restored, deleted, skipped = _restore_entries(turn_dir, index)
    return {
        "seq": _int(seq, 0),
        "restored": restored,
        "deleted": deleted,
        "skipped": skipped,
        "external_changes": list(index.get("external_changes") or []),
    }


# ---------------------------------------------------------------------------
# 项目回收站


def _bound_trash_base() -> Path:
    bound = _bound()
    cfg = snapshot_cfg(bound.get("config")) if bound.get("config") is not None else dict(_SNAPSHOT_DEFAULTS)
    return _trash_base(cfg, str(bound.get("project_id") or ""))


def trash_base() -> Path:
    """当前绑定上下文对应的项目回收站目录"""
    return _bound_trash_base()


def move_to_trash(path: Path) -> Path:
    """文件或目录移入项目回收站（带时间戳前缀防撞名），返回落点"""
    base = _bound_trash_base()
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = base / f"{stamp}_{path.name}"
    i = 1
    while dest.exists():
        dest = base / f"{stamp}_{i}_{path.name}"
        i += 1
    shutil.move(str(path), str(dest))
    return dest


def _trash_dir_for(config, project_id: str) -> Path:
    cfg = snapshot_cfg(config)
    return _trash_base(cfg, project_id)


def trash_count(config, project_id: str) -> int:
    """回收站待处理文件数（退出时提示用）"""
    trash = _trash_dir_for(config, project_id)
    if not trash.is_dir():
        return 0
    return sum(1 for p in trash.iterdir() if p.is_file())


def clear_trash(config, project_id: str) -> Tuple[int, int]:
    """/clear-trash：真正不可逆删除回收站内容；返回 (文件数, 释放字节)"""
    trash = _trash_dir_for(config, project_id)
    count, total = 0, 0
    if trash.is_dir():
        for p in list(trash.iterdir()):
            try:
                if p.is_file():
                    total += p.stat().st_size
                    p.unlink()
                    count += 1
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    count += 1
            except OSError as e:
                log.warn("回收站清理失败 %s: %s", p, e)
    log.info("回收站已清空: %d 项，%d 字节", count, total)
    return count, total
