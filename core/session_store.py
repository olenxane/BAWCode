#该脚本负责历史会话的磁盘存储：按项目隔离目录、原子写入、扫描列表、加载与删除。
#文件布局：data/sessions/{project_id}/{YYYYMMDD-HHMMSS-hexx}.json（与长期记忆 Projects/ 同范式）
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core.log import get_logger

log = get_logger("session_store")

_ROOT = Path(__file__).resolve().parent.parent

SESSION_VERSION = 1


def new_session_id() -> str:
    """生成会话 ID：20260926-143022-ab12（时间可排序 + 短随机后缀防同秒碰撞）"""
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{now}-{uuid.uuid4().hex[:4]}"


def sessions_dir(config, project_id: str) -> Path:
    """会话存储根目录：data/sessions/{project_id}，project_id 为空回落 _default"""
    memory_cfg = (getattr(config, "data", None) or {}).get("memory", {})
    raw = memory_cfg.get("sessions_dir") or "data/sessions"
    base = Path(raw) if Path(raw).is_absolute() else _ROOT / raw
    return base / (project_id or "_default")


def save_session_data(directory: Path, data: dict) -> Path:
    """原子写入会话文件（.tmp + os.replace，防 Ctrl+C 写一半损坏），返回最终路径"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{data.get('id')}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
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


def load_session_data(directory: Path, session_id: str) -> Optional[dict]:
    """按 id 加载完整会话数据（messages/plan/steps 原样）"""
    path = directory / f"{session_id}.json"
    if not path.exists():
        log.warn("会话文件不存在: %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("会话文件解析失败 %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def delete_session_data(directory: Path, session_id: str) -> bool:
    """删除会话文件；文件不存在返回 False"""
    path = directory / f"{session_id}.json"
    try:
        path.unlink()
        log.info("会话已删除: %s", path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.error("会话删除失败 %s: %s", path, e)
        return False
