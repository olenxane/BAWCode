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


# ----- 文件状态台账（edit/位置写入门禁）-----
# read/write/edit 成功后登记磁盘基线（md5+mtime+size+行数）与内存快照（LRU 上限）；
# edit/位置 write 前校验三态：无记录拒绝（要求先 read）、新鲜放行（哪怕 read 内容
# 已被剥离/压缩——锚点是台账不是对话历史）、内容漂移拒绝并附变更区间行号与窄读建议。
# 台账是会话级 RAM 状态，随会话生命周期重置（memory.__init__/switch_to/start_new_session/clear）；
# 快照仅存内存（工具函数无 session 上下文，落盘版待台账持久化时一并考虑），LRU 淘汰后
# 漂移检测退化为"已变化但无法给出区间"。
import difflib
import hashlib
import re as _re

_LEDGER_MAX_ENTRIES = 32
_LEDGER_SNAPSHOT_MAX_CHARS = 2_000_000
_LEDGER_DIFF_MAX_LINES = 20000
_ledger: dict = {}


def _ledger_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _file_md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def ledger_reset() -> None:
    """清空台账（会话初始化/切换/清空时调用，防跨会话串状态）"""
    _ledger.clear()


def ledger_register(path: Path, text: str = "", source: str = "") -> None:
    """登记/更新文件基线：对磁盘现状取 md5 指纹，保存行数与文本快照（供漂移时算 diff）"""
    key = _ledger_key(path)
    try:
        st = path.stat()
    except OSError as e:
        log.warn("台账登记失败（文件不可 stat）: %s (%s)", path, e)
        return
    _ledger.pop(key, None)  # 重插维持 LRU 新鲜度
    _ledger[key] = {
        "md5": _file_md5(path),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "total_lines": len((text or "").splitlines()),
        "source": source or "-",
        "snapshot": (text or "")[:_LEDGER_SNAPSHOT_MAX_CHARS] if text else "",
    }
    while len(_ledger) > _LEDGER_MAX_ENTRIES:
        _ledger.pop(next(iter(_ledger)))
    log.debug("台账登记: %s（来源=%s，%d行）", path, source or "-", _ledger[key]["total_lines"])


def ledger_fresh_hint(path: Path) -> str:
    """剥离 read 记录时的新鲜度提示（仅比 mtime/size，不做全文哈希）"""
    entry = _ledger.get(_ledger_key(path))
    if entry is None:
        return "文件不在台账中，edit_file 前请先 read"
    try:
        st = path.stat()
    except OSError:
        return "文件已不存在，请先确认路径"
    if st.st_mtime_ns == entry["mtime_ns"] and st.st_size == entry["size"]:
        return "文件自上次读取未变化（仍新鲜），可直接 edit_file"
    return "文件 mtime/size 与上次读取不符（可能已变化），edit_file 前请重新 read"


def ledger_check(path: Path) -> Optional[str]:
    """edit/位置写入门禁：返回 None=放行；否则为拒绝消息（含原因、变更区间与窄读建议）"""
    entry = _ledger.get(_ledger_key(path))
    if entry is None:
        return (
            f"编辑被拒绝：{path} 本次会话尚未 read 过。"
            "请先用 read 工具读取该文件（可用 offset/limit 分段），再执行编辑。"
        )
    try:
        st = path.stat()
    except OSError as e:
        return f"编辑被拒绝：无法获取文件状态（{e}），请重新 read 确认。"
    if st.st_mtime_ns == entry["mtime_ns"] and st.st_size == entry["size"]:
        return None
    current_md5 = _file_md5(path)
    if current_md5 == entry["md5"]:
        # 触碰未变内容（保存但无改动）：刷新缓存口径，避免下次重复全文哈希
        entry["mtime_ns"], entry["size"] = st.st_mtime_ns, st.st_size
        return None
    regions, changed = ledger_diff_regions(path)
    if regions:
        first = _re.match(r"L(\d+)", regions[0])
        start_line = int(first.group(1)) if first else 1
        suggest = f"建议 read offset={max(1, start_line - 3)} limit=40 核对后重试"
        return (
            f"编辑被拒绝：文件自上次读取后已变化"
            f"（{'、'.join(regions)}，共变更约{changed}行）。{suggest}"
        )
    return (
        f"编辑被拒绝：文件自上次读取后已变化"
        f"（md5 {entry['md5'][:8]}→{current_md5[:8]}，内容快照不可用，无法给出变更区间）。"
        "请重新 read 后重试。"
    )


def ledger_diff_regions(path: Path, max_regions: int = 5) -> tuple:
    """当前磁盘内容 vs 上次快照的变更区间（新文件行号口径）与变更行数；快照不可用返回空"""
    entry = _ledger.get(_ledger_key(path))
    old_text = (entry or {}).get("snapshot") or ""
    if not old_text:
        return [], 0
    try:
        raw = path.read_bytes()
    except OSError:
        return [], 0
    from core.tools import _decode_best_effort  # 惰性导入避免模块循环

    new_text, _enc, _ok = _decode_best_effort(raw)
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    if max(len(old_lines), len(new_lines)) > _LEDGER_DIFF_MAX_LINES:
        return [], 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    regions, changed = [], 0
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += max(a2 - a1, b2 - b1)
        if len(regions) < max_regions:
            # 以新文件行号报告（模型重读的是新文件）；纯删除区间为空时报告插入位置
            regions.append(f"L{b1 + 1}-L{b2}" if b2 > b1 else f"L{b1 + 1}前")
    if len(regions) == max_regions and changed and _re.search(r"L\d+", "、".join(regions)):
        pass  # 区间已截断到 max_regions，错误消息里以"共变更约N行"兜底
    return regions, changed
