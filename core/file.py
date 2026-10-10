"""工作区文件引用：扫描候选、解析 @ 引用并读取文本内容。"""
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


MAX_FILE_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_CANDIDATES = 100
MAX_SCAN_DEPTH = 32
SCAN_INTERVAL = 1.0
IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
_REFERENCE_RE = re.compile(r'@("[^"]+"|\'[^\']+\'|[^\s]+)')


def workspace_path(workspace) -> Path:
    return Path(workspace or Path.cwd()).resolve()


def relative_path(path: Path, workspace) -> Optional[str]:
    root = workspace_path(workspace)
    try:
        resolved = path.resolve()
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            return None
        return resolved.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def safe_path(name: str, workspace) -> Optional[Path]:
    """把用户提供的相对路径解析到工作区内。"""
    raw = str(name or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        return None
    root = workspace_path(workspace)
    path = (root / raw).resolve()
    try:
        if os.path.commonpath((str(root), str(path))) != str(root):
            return None
    except ValueError:
        return None
    return path


def quote_reference(path: str) -> str:
    """生成可直接粘贴到输入框的 @ 引用。"""
    value = str(path or "").replace("\\", "/")
    if any(ch.isspace() for ch in value) or '"' in value:
        return '@"' + value.replace('"', '\\"') + '"'
    return "@" + value


def _ignored(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in IGNORED_DIRS for part in parts)


def _scan_dir(folder: Path, root: Path, query: str, items: List[dict], limit: int) -> List[Path]:
    """扫描单层目录，返回需要继续展开的下一层目录"""
    following: List[Path] = []
    try:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name.lower())
    except OSError:
        return following
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in IGNORED_DIRS:
                    following.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if limit and len(items) >= limit:
                break
            if entry.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        path = Path(entry.path)
        rel = relative_path(path, root)
        if not rel:
            continue
        if query and query not in rel.lower() and query not in entry.name.lower():
            continue
        content, _error = read_text(path)
        if content is None:
            continue
        items.append({"name": quote_reference(rel), "path": rel, "hint": rel})
    return following


def scan_candidates(prefix: str = "", workspace=None, depth: int = 1, limit: int = MAX_CANDIDATES) -> dict:
    """按目录层级分级扫描候选：depth=1 只扫工作区根目录，逐级下钻。

    返回 {"items": 候选列表, "more": 是否还有更深层目录未展开}。"""
    root = workspace_path(workspace)
    query = str(prefix or "").replace("\\", "/").strip().lower()
    limit = max(0, int(limit))
    max_depth = None if depth is None else max(1, int(depth))
    items: List[dict] = []
    if not root.is_dir():
        return {"items": items, "more": False}
    pending = [root]
    level = 0
    while pending and (max_depth is None or level < max_depth) and (not limit or len(items) < limit):
        level += 1
        following: List[Path] = []
        for folder in pending:
            following.extend(_scan_dir(folder, root, query, items, limit))
        pending = following
    items.sort(key=lambda item: (item["path"].lower(), item["path"]))
    if limit:
        items = items[:limit]
    return {"items": items, "more": bool(pending)}


def list_candidates(prefix: str = "", workspace=None, limit: int = MAX_CANDIDATES, depth=None) -> List[dict]:
    """列出候选；depth 为 None 时遍历全部层级"""
    return scan_candidates(prefix, workspace, depth=depth, limit=limit)["items"]


class CandidateScanner:
    """后台分级扫描：输入停止 interval 后扫第一层，之后每 interval 下钻一层。

    线程单写 query/items，调用方经 snapshot() 只读取回；interval 内的连续
    submit 合并为一次扫描（其余为最新查询）。"""

    def __init__(self, interval: float = SCAN_INTERVAL, limit: int = MAX_CANDIDATES, max_depth: int = MAX_SCAN_DEPTH):
        self.interval = float(interval)
        self.limit = int(limit)
        self.max_depth = int(max_depth)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._query: Optional[str] = None
        self._workspace = None
        self._items: List[dict] = []
        self._depth = 0
        self._done = True
        self._epoch = 0
        self._thread = threading.Thread(target=self._run, name="file-scan", daemon=True)

    def _ensure_started(self) -> None:
        """惰性启动后台线程：未真正扫描过就不占线程"""
        if self._stop.is_set() or self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="file-scan", daemon=True)
        self._thread.start()

    def submit(self, prefix: str, workspace=None) -> None:
        """登记查询；interval 内的多次调用只触发一次扫描"""
        with self._lock:
            self._ensure_started()
            self._epoch += 1
            self._query = str(prefix or "")
            self._workspace = workspace
            self._items = []
            self._depth = 0
            self._done = False
        self._wake.set()

    def cancel(self) -> None:
        """丢弃在途扫描与已有结果"""
        with self._lock:
            self._epoch += 1
            self._query = None
            self._items = []
            self._depth = 0
            self._done = True
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {"query": self._query, "items": self._items, "depth": self._depth, "done": self._done}

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        served = -1
        while not self._stop.is_set():
            with self._lock:
                epoch, query, workspace = self._epoch, self._query, self._workspace
            if query is None or epoch == served:
                self._wake.wait(0.2)
                continue
            self._wake.clear()
            # 去抖：interval 内出现新提交则重新计时，避免连续输入触发多次扫描
            if self._wake.wait(self.interval):
                continue
            if self._scan(query, workspace, epoch):
                served = epoch

    def _scan(self, query: str, workspace, epoch: int) -> bool:
        """逐级推进深度，新提交打断时返回 False"""
        depth = 1
        while not self._stop.is_set():
            result = scan_candidates(query, workspace, depth=depth, limit=self.limit)
            with self._lock:
                if self._epoch != epoch:
                    return False
                self._items = result["items"]
                self._depth = depth
                self._done = not result["more"] or depth >= self.max_depth
                if self._done:
                    return True
            depth += 1
            if self._wake.wait(self.interval):
                return False
        return False


def parse_references(text: str) -> List[dict]:
    """解析文本中的 @路径，占位符保留原文。"""
    out = []
    for match in _REFERENCE_RE.finditer(str(text or "")):
        token = match.group(1)
        if token[:1] in ('"', "'") and token[-1:] == token[:1]:
            name = token[1:-1].replace('\\"', '"')
        else:
            name = token
        out.append({"token": match.group(0), "path": name, "start": match.start(), "end": match.end()})
    return out


def read_text(path: Path, max_bytes: int = MAX_FILE_BYTES) -> Tuple[Optional[str], str]:
    """读取 UTF-8/本地常见编码文本，返回内容和失败原因。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"无法读取: {exc}"
    if len(data) > max_bytes:
        return None, f"文件超过 {max_bytes} 字节限制"
    if b"\x00" in data:
        return None, "二进制文件不支持直接引用"
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding), ""
        except UnicodeDecodeError:
            continue
    return None, "文件不是支持的文本编码"


def resolve_references(text: str, workspace=None, max_context_bytes: int = MAX_CONTEXT_BYTES) -> dict:
    """读取文本中的文件引用，并把内容追加到原用户消息后。"""
    original = str(text or "")
    root = workspace_path(workspace)
    references = []
    blocks = []
    used = set()
    total = 0
    for item in parse_references(original):
        path = safe_path(item["path"], root)
        record = {"token": item["token"], "path": item["path"], "status": "missing"}
        if path is None or not path.is_file() or _ignored(path, root):
            record["message"] = "文件不存在或路径不在工作区内"
            references.append(record)
            continue
        rel = relative_path(path, root)
        record["path"] = rel or item["path"]
        if rel in used:
            record["status"] = "duplicate"
            references.append(record)
            continue
        content, error = read_text(path)
        if content is None:
            record["message"] = error
            references.append(record)
            continue
        size = len(content.encode("utf-8"))
        if total + size > max_context_bytes:
            record["status"] = "too_large"
            record["message"] = f"本轮文件内容超过 {max_context_bytes} 字节限制"
            references.append(record)
            continue
        used.add(rel)
        total += size
        record.update({"status": "loaded", "bytes": size})
        references.append(record)
        blocks.append(f"[引用文件: {rel}]\n```text\n{content}\n```")
    appended = ("\n\n" + "\n\n".join(blocks)) if blocks else ""
    return {"text": original + appended, "references": references, "bytes": total}


def file_metadata(text: str, workspace=None) -> List[dict]:
    """从已展开消息中提取成功引用元数据。"""
    result = resolve_references(text, workspace, max_context_bytes=MAX_CONTEXT_BYTES)
    return [item for item in result["references"] if item.get("status") == "loaded"]


def active_reference(text: str, cursor: int) -> Optional[dict]:
    """返回光标前正在输入的 @ 引用及替换范围。"""
    before = str(text or "")[: max(0, cursor)]
    at = before.rfind("@")
    if at < 0 or (at > 0 and not before[at - 1].isspace()):
        return None
    token = before[at + 1 :]
    if token.startswith('"'):
        query = token[1:]
        start = at
    else:
        if any(ch.isspace() for ch in token):
            return None
        query = token
        start = at
    return {"query": query, "start": start, "end": cursor}
