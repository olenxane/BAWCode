#该脚本负责历史会话的磁盘存储：按项目隔离目录、原子写入、扫描列表、加载与删除。
#文件布局：data/sessions/{project_id}/{YYYYMMDD-HHMMSS-hexx}.json（与长期记忆 Projects/ 同范式）
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core.log import get_logger

log = get_logger("session_store")

_ROOT = Path(__file__).resolve().parent.parent

SESSION_VERSION = 1
save_lock = threading.Lock()


def new_session_id() -> str:
    """生成会话 ID：20260926-143022-ab12cd34（时间可排序 + 随机后缀降低同秒碰撞概率）"""
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{now}-{uuid.uuid4().hex[:8]}"


def sessions_dir(config, project_id: str) -> Path:
    """会话存储根目录：data/sessions/{project_id}，project_id 为空回落 _default"""
    memory_cfg = (getattr(config, "data", None) or {}).get("memory", {})
    raw = memory_cfg.get("sessions_dir") or "data/sessions"
    base = Path(raw) if Path(raw).is_absolute() else _ROOT / raw
    return base / (project_id or "_default")


def session_path(directory: Path, session_id: str) -> Optional[Path]:
    """解析目录内的会话文件路径。

    会话标识只接受文件名字符，不接受路径。
    """
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    path = directory / f"{session_id}.json"
    if path.resolve().parent != directory.resolve():
        return None
    return path


def save_session_data(directory: Path, data: dict) -> Path:
    """使用独占临时文件原子保存会话。

    返回最终会话文件路径。
    """
    path = session_path(directory, data.get("id"))
    if path is None:
        raise ValueError("会话标识非法")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    directory.mkdir(parents=True, exist_ok=True)
    with save_lock:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def list_sessions(directory: Path) -> List[dict]:
    """扫描目录返回会话元信息，按 updated_at 倒序（损坏文件跳过，不阻塞面板）"""
    items: List[dict] = []
    if not directory.exists():
        return items
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warn("会话文件解析失败，跳过 %s: %s", path.name, e)
            continue
        if not isinstance(data, dict):
            continue
        items.append(
            {
                "id": data.get("id") or path.stem,
                "title": data.get("title") or "（无标题会话）",
                "created_at": data.get("created_at") or "",
                "updated_at": data.get("updated_at") or "",
                "message_count": data.get("message_count") or len(data.get("messages") or []),
                "path": path,
            }
        )
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


def heal_orphan_tool_calls(messages: list) -> int:
    """补齐孤儿 tool_calls：assistant 带 tool_calls 但缺配对 tool 结果时（历史中断所致）
    在该 assistant 的 tool 结果段末尾合成占位结果，否则后续每次请求会被 API 以
    配对不全拒绝。返回补齐条数"""
    result_ids = {
        str(m.get("tool_call_id"))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    extras: dict = {}
    for i, m in enumerate(messages):
        patched = []
        for c in m.get("tool_calls") or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "")
            if not cid or cid in result_ids:
                continue
            result_ids.add(cid)
            name = str(c.get("name") or "")
            patched.append({
                "role": "tool",
                "type": "tool",
                "tool_call_id": cid,
                "tool_name": name,
                "description": name,
                "content": "[会话修复：该调用无结果记录（历史中断所致）]",
            })
        if patched:
            extras[i] = patched
    if not extras:
        return 0
    # 重组：带孤儿的 assistant 先接上紧随其后的既有 tool 结果段，再补孤儿
    out = []
    i = 0
    while i < len(messages):
        out.append(messages[i])
        if i in extras:
            j = i + 1
            while j < len(messages) and str(messages[j].get("role")) == "tool":
                out.append(messages[j])
                j += 1
            out.extend(extras[i])
            i = j
            continue
        i += 1
    messages[:] = out
    return sum(len(v) for v in extras.values())


def load_session_data(directory: Path, session_id: str) -> Optional[dict]:
    """按 id 加载完整会话数据（messages/plan/steps 原样，孤儿 tool_calls 顺带修复）"""
    path = session_path(directory, session_id)
    if path is None:
        log.warn("会话标识非法")
        return None
    if not path.exists():
        log.warn("会话文件不存在: %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("会话文件解析失败 %s: %s", path, e)
        return None
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        healed = heal_orphan_tool_calls(data["messages"])
        if healed:
            log.warn("会话 %s 修复 %d 条孤儿工具结果", session_id, healed)
    return data if isinstance(data, dict) else None


def delete_session_data(directory: Path, session_id: str) -> bool:
    """删除会话文件；文件不存在返回 False"""
    path = session_path(directory, session_id)
    if path is None:
        log.warn("会话标识非法")
        return False
    try:
        path.unlink()
        log.info("会话已删除: %s", path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.error("会话删除失败 %s: %s", path, e)
        return False
