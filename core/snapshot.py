#回合级文件快照与回滚（/undo）+ 项目回收站（delete_file / /clear-trash）
#快照管线行为：
#- 回合开始做轻量清单（路径+mtime+size，不含内容），回合末对账——execute_command 等
#  绕过写入工具的改动至少能在 /undo 里明确列出"无法自动还原"，不装作没发生
#- 两个文件写入工具（write 全量/位置、edit_file）+ delete_file 在改动前
#  capture_before 留底原始字节（字节级还原保编码/CRLF 保真），tool 名记入索引——
#  这就是"成功的文件修改工具调用历史"（上下文回合末剥离会丢参数，必须在捕获时落索引）
#- 回退由程序确定性执行（不走模型）：逆操作重放对 edit 顺序/多处匹配敏感，write 覆盖
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
}

# 单回合字节留底总量上限（与单文件上限并存，防大量小文件把磁盘写爆）
_TURN_BYTES_TOTAL_MAX = 256 * 1024 * 1024

# 清单遍历跳过的目录名（构建产物/VCS/工具自身数据，量大且无对账价值）
_INVENTORY_SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".bawcode", "data", "_recycle", "develop",
}
_EXTERNAL_LIST_CAP = 50  # 对账清单最多列出的外部改动条数

# 当前回合缓冲：begin_turn 置入、end_turn 取走；capture_before 从各写入工具调用
_TURN: Optional[dict] = None
# 绑定上下文（config/project_id）：与 enabled 无关，delete_file 的回收站定位用
_BOUND: Dict[str, Any] = {}
_LOCK = threading.Lock()


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
    return data


def _base_dir(cfg: dict, key: str) -> Path:
    p = Path(str(cfg.get(key) or ""))
    return p if p.is_absolute() else _ROOT / p


def _snapshots_dir(cfg: dict, project_id: str, session_id: str) -> Path:
    return _base_dir(cfg, "dir") / (project_id or "_default") / (session_id or "_default")


def _trash_base(cfg: dict, project_id: str) -> Path:
    return _base_dir(cfg, "trash_dir") / (project_id or "_default")


def _key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _file_md5(path: Path) -> Optional[str]:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _walk_inventory(root: Path, cap: int) -> Optional[Dict[str, Tuple[int, int]]]:
    """轻量清单：路径 → (mtime_ns, size)，不含内容；超 cap 返回 None（放弃对账，封顶成本）"""
    inventory: Dict[str, Tuple[int, int]] = {}
    try:
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _INVENTORY_SKIP_DIRS]
            for name in files:
                full = os.path.join(cur, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                inventory[str(Path(full).resolve())] = (st.st_mtime_ns, st.st_size)
                if len(inventory) > cap:
                    return None
    except OSError:
        return None
    return inventory


# ---------------------------------------------------------------------------
# 回合生命周期


def begin_turn(config, project_id: str, session_id: str, task: str = "", msg_index: int = -1) -> None:
    """回合开始：绑定上下文 + 开捕获缓冲 + 建轻量清单。每个回合（含中断/异常路径）必须配对 end_turn。
    msg_index 为本回合用户消息的绝对序号（树模式 Ctrl+Z 按它定位"哪些回合被移除"）"""
    global _TURN
    cfg = snapshot_cfg(config)
    with _LOCK:
        _BOUND["config"] = config
        _BOUND["project_id"] = str(project_id or "")
        _TURN = {
            "cfg": cfg,
            "project_id": str(project_id or ""),
            "session_id": str(session_id or ""),
            "task": " ".join(str(task or "").split())[:120],
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "msg_index": _int(msg_index, -1),
            "files": {},
            "inventory": _walk_inventory(Path.cwd(), cfg["inventory_max_files"]) if cfg["enabled"] else None,
            "bytes_total": 0,
        }
        if _TURN["inventory"] is None and cfg["enabled"] and cfg["inventory_max_files"]:
            log.debug("清单超限，本回合跳过外部改动对账")


def capture_before(path: Path, tool: str = "") -> None:
    """写入工具落盘前调用：留底原始字节（同回合同文件首次为准，保证还原到回合开始时）。
    无活动回合/未启用时 no-op；超限文件只记录不复制（undo 时列为无法还原）"""
    turn = _TURN
    if turn is None or not turn["cfg"]["enabled"]:
        return
    key = _key(path)
    with _LOCK:
        if key in turn["files"]:
            return
        rec: Dict[str, Any] = {
            "path": key,
            "existed_before": False,
            "bytes": None,
            "tool": str(tool or ""),
            "time": time.strftime("%H:%M:%S"),
            "skip_reason": "",
        }
        if path.exists():
            rec["existed_before"] = True
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            limit = turn["cfg"]["max_file_bytes"]
            if size > limit or turn["bytes_total"] + size > _TURN_BYTES_TOTAL_MAX:
                rec["skip_reason"] = f"文件 {size} 字节超限未留底"
            else:
                try:
                    rec["bytes"] = path.read_bytes()
                    turn["bytes_total"] += size
                except OSError as e:
                    rec["skip_reason"] = f"读取失败: {e}"
        turn["files"][key] = rec


def end_turn() -> Optional[dict]:
    """回合结束（finally 配对调用）：有捕获或外部改动则落盘快照目录，空回合无副作用。
    返回摘要 dict（seq/files/external_changes），空回合返回 None"""
    global _TURN
    with _LOCK:
        turn = _TURN
        _TURN = None
    if turn is None:
        return None
    cfg = turn["cfg"]
    seq_dir = _next_seq_dir(cfg, turn["project_id"], turn["session_id"])
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

    external: List[str] = []
    if turn["inventory"] is not None:
        current = _walk_inventory(Path.cwd(), cfg["inventory_max_files"])
        if current is not None:
            captured = set(turn["files"])
            # 自身产物不算外部改动：本回合刚写的 blob、移入回收站的文件
            # （前缀比对带分隔符边界，防 project_id 互为前缀时误判）
            snap_prefix = str(_snapshots_dir(cfg, turn["project_id"], turn["session_id"])) + os.sep
            trash_prefix = str(_trash_base(cfg, turn["project_id"])) + os.sep
            for key, (mtime, size) in current.items():
                before = turn["inventory"].get(key)
                if key in captured or before == (mtime, size):
                    continue
                if key.startswith(snap_prefix) or key.startswith(trash_prefix):
                    continue
                external.append(key)
                if len(external) >= _EXTERNAL_LIST_CAP:
                    external.append("…（其余略）")
                    break
            # 已消失的文件也属于外部改动
            for key in turn["inventory"].keys() - current.keys():
                if len(external) >= _EXTERNAL_LIST_CAP:
                    if external[-1] != "…（其余略）":
                        external.append("…（其余略）")
                    break
                if key not in captured and not key.startswith((snap_prefix, trash_prefix)):
                    external.append(key)

    if not entries and not external:
        return None  # 空回合不产生任何文件

    index = {
        "version": 1,
        "seq": seq,
        "time": turn["time"],
        "task": turn["task"],
        "msg_index": turn["msg_index"],
        "files": entries,
        "external_changes": external,
    }
    _atomic_write_bytes(seq_dir / "index.json", json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8"))
    _prune_old_turns(cfg, turn["project_id"], turn["session_id"], cfg["max_turns"])
    log.info("回合快照已落盘: #%d（%d 个文件，外部改动 %d）", seq, len(entries), len(external))
    return {"seq": seq, "files": len(entries), "external_changes": external}


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
            restorable = sum(1 for f in index.get("files") or [] if f.get("captured"))
            turns.append(
                {
                    "seq": int(child.name),
                    "time": str(index.get("time") or ""),
                    "task": str(index.get("task") or ""),
                    "files": len(index.get("files") or []),
                    "restorable": restorable,
                    "dir": str(child),
                }
            )
    return turns


def undo(config, project_id: str, session_id: str, which: Any = "last") -> Optional[dict]:
    """回滚指定回合（缺省最近一个）：新文件删除、旧文件字节还原；用户事后改过
    （md5 与回合末不符）的文件跳过。完成后 ledger_reset 强制模型重读。
    无可回滚内容返回 None"""
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

    restored, deleted, skipped = _restore_entries(turn_dir, index)
    toolstore.ledger_reset()
    result = {
        "seq": int(turn_dir.name),
        "task": str(index.get("task") or ""),
        "restored": restored,
        "deleted": deleted,
        "skipped": skipped,
        "external_changes": list(index.get("external_changes") or []),
    }
    log.info(
        "回滚回合 #%d: 还原%d 删除%d 跳过%d", result["seq"], len(restored), len(deleted), len(skipped)
    )
    return result


def _restore_entries(turn_dir: Path, index: dict) -> Tuple[List[str], List[str], List[Tuple[str, str]]]:
    """按 index.json 还原单个回合的文件改动（后写先回）；不做 ledger_reset（调用方统一）。
    md5 门禁：当前磁盘 md5 ≠ 回合末 md5（用户事后改过）的文件跳过"""
    restored: List[str] = []
    deleted: List[str] = []
    skipped: List[Tuple[str, str]] = []
    for entry in reversed(index.get("files") or []):
        path = Path(entry["path"])
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
    cfg = snapshot_cfg(_BOUND.get("config")) if _BOUND.get("config") is not None else dict(_SNAPSHOT_DEFAULTS)
    return _trash_base(cfg, str(_BOUND.get("project_id") or ""))


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
