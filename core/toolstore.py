#该脚本负责工具调用结果的外置存储：超限/被剥离的工具输出以调用id为文件名落盘，
#供模型后续用 read 工具分批回读；提供 token 计量切片、原子写入与会话清理。
#文件布局：data/toolcalls/{project_id}/{session_id}/{call_id}.txt（与 sessions/ 同范式）
import json
import os
import time
from pathlib import Path
from typing import Optional

from core import tokens as token_mod
from core.log import get_logger

log = get_logger("toolstore")

_ROOT = Path(__file__).resolve().parent.parent

_HEADER_TITLE = "[BAWCode 工具调用记录]"


def store_dir(config, project_id: str, session_id: str) -> Path:
    """外置存储目录：data/toolcalls/{project_id}/{session_id}，project_id 为空回落 _default"""
    ctx_cfg = (getattr(config, "data", None) or {}).get("context", {})
    raw = ctx_cfg.get("persist_dir") or "data/toolcalls"
    base = Path(raw) if Path(raw).is_absolute() else _ROOT / raw
    return base / (project_id or "_default") / (session_id or "_default")


def count_tokens_safe(text: str, model: str = "") -> int:
    """token 计数兜底：tiktoken 不可用时按字符数折半（与 memory 退化口径一致）"""
    if not text:
        return 0
    try:
        return token_mod.count_tokens(text, model)
    except Exception:
        return max(1, len(text) // 2)


def slice_to_tokens(text: str, cap: int, model: str = "") -> str:
    """截取不超过 cap tokens 的头部切片；tiktoken 缺失时退化为 cap*2 字符"""
    if cap <= 0 or not text:
        return ""
    try:
        enc = token_mod._encoding_for_model(model)
        if enc is None:
            raise RuntimeError("tiktoken 不可用")
        ids = enc.encode(text, disallowed_special=())
        if len(ids) <= cap:
            return text
        return enc.decode(ids[:cap])
    except Exception:
        keep = cap * 2
        return text if len(text) <= keep else text[:keep]


def persist(
    config,
    project_id: str,
    session_id: str,
    call_id: str,
    tool_name: str,
    arguments: dict,
    description: str,
    content: str,
) -> Optional[Path]:
    """以 call_id 为文件名原子写入完整输出；文件已存在则幂等跳过，返回路径"""
    if not call_id:
        return None
    directory = store_dir(config, project_id, session_id)
    path = directory / f"{call_id}.txt"
    if path.exists():
        return path
    content = content or ""
    header_lines = [
        _HEADER_TITLE,
        f"call_id: {call_id}",
        f"tool: {tool_name or '-'}",
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"arguments: {json.dumps(arguments or {}, ensure_ascii=False)}",
        f"description: {description or '-'}",
        f"total_chars: {len(content)}",
        f"total_lines: {len(content.splitlines())}",
        "",
        "--- content ---",
    ]
    directory.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text("\n".join(header_lines) + "\n" + content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.error("工具输出外置失败 %s: %s", path, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    log.debug("工具输出已外置: %s (%d字)", path, len(content))
    return path


def header_line_count() -> int:
    """外置文件元数据头行数（正文自该行+1 起，供指针提示分页起点）"""
    return 10


def pointer_line(path: Optional[Path], total_lines: int) -> str:
    """行内消息中的落盘指针（模型据此用 read offset/limit 分批回读）"""
    if path is None:
        return "完整输出外置失败，仅保留行内部分"
    start = header_line_count() + 1
    return (
        f"完整输出共{total_lines}行已保存: {path}"
        f"（文件前{header_line_count()}行为元数据，正文自第{start}行起，"
        f"可用 read 工具传 offset/limit 分批读取）"
    )


def clear_session(config, project_id: str, session_id: str) -> int:
    """清理会话对应的全部外置记录（/clear 用），返回删除文件数"""
    directory = store_dir(config, project_id, session_id)
    if not directory.exists():
        return 0
    removed = 0
    for path in directory.glob("*.txt"):
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            log.warn("外置记录删除失败 %s: %s", path, e)
    try:
        directory.rmdir()
    except OSError:
        pass
    try:
        directory.parent.rmdir()  # 项目目录空了也顺手清掉
    except OSError:
        pass
    if removed:
        log.info("已清理会话外置记录: %s (%d个文件)", directory, removed)
    return removed
