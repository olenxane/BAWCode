"""rag —— 结构感知项目 RAG v0.3.0（消息驱动主动召回 + 手动索引持久化）。

索引：遍历当前项目工作区（ctx.workspace），tree-sitter 按 AST 提取类/函数/
方法切片（路由如 "main.py > Computer > use"），markdown 按标题分节；每个切片
额外提取**注释与 docstring** 作为自然语言匹配基（doc 字段）——召回时用自然
语言描述匹配，而非直接匹配代码体。

主动召回（context_supplement）：用户每发送消息即触发，按该消息检索切片注入。
匹配方式由配置项 match_mode 选择：
  - keyword   关键词加权：doc 命中(5) > 路由(2) > 文件名(1) > 代码体(0.5，
              用户要求代码块与消息的匹配程度应较低)
  - embedding 嵌入模型：对切片 doc 文本与用户消息各算向量（OpenAI 兼容
              /v1/embeddings，插件独立配置 embedding_base_url/model/api_key），
              余弦相似度召回（阈值 embedding_min_score）。API 未配置/失败回落
              keyword。向量落盘 embeddings.json，仅对有 doc 的切片计算。
索引文件持久化在 ctx.storage_dir()（{workspace}/.bawcode/plugin-data/rag/），
生命周期由插件自行处置：built_at 超过 index_ttl_days（缺省 30 天）即废弃删除；
set_rag 的项目级开关存 state.json（不随索引过期）。

手动触发：索引只在显式调用时构建——模型工具 rag_index，或用户 /rag build。
增量按 (mtime_ns, size) 只重解析变化文件。

工具面（模型可见）：
  - rag_search 关键词检索切片（Agent 主动检索；路由(4) > 代码体(2) > doc(1)，
    返回带索引信息的代码块）
  - rag_index  手动重建索引并持久化，返回统计
  - set_rag    开启/关闭本项目的 RAG（召回与检索同时停用；子代理不可用）
用户命令 /rag：status | build | on | off | clear。

不含 add 类工具（记忆相关将独立为另一个插件）；无外部向量库接管点。
依赖缺失（tree-sitter）时优雅降级：工具返回安装提示，其余不崩溃。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import tree_sitter as _ts
    from tree_sitter_language_pack import get_parser as _get_parser

    _TS_OK = True
except Exception:  # ImportError 或 ABI 不配对等
    _TS_OK = False

# 扩展名 → (tree-sitter 语言名, 定义节点类型)；节点类型在解析时按语法
# node_kind 内省过滤（各 grammar 版本节点名有差异，未知类型直接丢弃）
LANG_BY_EXT: Dict[str, Tuple[str, set]] = {
    ".py": ("python", {"class_definition", "function_definition"}),
    ".js": ("javascript", {"class_declaration", "function_declaration",
                           "method_definition", "generator_function_declaration"}),
    ".jsx": ("javascript", {"class_declaration", "function_declaration",
                            "method_definition", "generator_function_declaration"}),
    ".mjs": ("javascript", {"class_declaration", "function_declaration",
                            "method_definition", "generator_function_declaration"}),
    ".cjs": ("javascript", {"class_declaration", "function_declaration",
                            "method_definition", "generator_function_declaration"}),
    ".ts": ("typescript", {"class_declaration", "function_declaration",
                           "method_definition", "interface_declaration",
                           "abstract_class_declaration"}),
    ".tsx": ("tsx", {"class_declaration", "function_declaration",
                     "method_definition", "interface_declaration"}),
    ".go": ("go", {"function_declaration", "method_declaration"}),
    ".rs": ("rust", {"function_item", "struct_item", "enum_item", "trait_item"}),
    ".java": ("java", {"class_declaration", "method_declaration",
                       "interface_declaration", "enum_declaration"}),
    ".kt": ("kotlin", {"function_declaration", "class_declaration"}),
    ".kts": ("kotlin", {"function_declaration", "class_declaration"}),
    ".c": ("c", {"function_definition", "struct_specifier", "enum_specifier"}),
    ".h": ("c", {"function_definition", "struct_specifier", "enum_specifier"}),
    ".cpp": ("cpp", {"function_definition", "class_specifier",
                     "struct_specifier", "namespace_definition"}),
    ".cc": ("cpp", {"function_definition", "class_specifier",
                    "struct_specifier", "namespace_definition"}),
    ".cxx": ("cpp", {"function_definition", "class_specifier",
                     "struct_specifier", "namespace_definition"}),
    ".hpp": ("cpp", {"function_definition", "class_specifier",
                     "struct_specifier", "namespace_definition"}),
    ".cs": ("csharp", {"class_declaration", "method_declaration",
                       "interface_declaration", "struct_declaration"}),
    ".rb": ("ruby", {"method", "singleton_method", "class", "module"}),
    ".php": ("php", {"method_declaration", "class_declaration", "function_definition"}),
    ".sh": ("bash", {"function_definition"}),
    ".bash": ("bash", {"function_definition"}),
    ".lua": ("lua", {"function_declaration", "function_definition"}),
    ".swift": ("swift", {"function_declaration", "class_declaration"}),
}

# 父链路由认定的"类容器"节点：函数祖先命中即在路由前插入容器名
CLASS_LIKE = {
    "class_definition", "class_declaration", "class_specifier",
    "struct_specifier", "impl_item", "namespace_definition",
    "interface_declaration", "abstract_class_declaration",
    "struct_declaration", "class", "module",
}
# 个别容器节点的名字不在 "name" 字段（rust impl_item 的类型在 "type" 字段）
NAME_FIELD_OVERRIDE = {"impl_item": "type"}

# 注释语法（doc 匹配基提取用）；python 另有 docstring
LINE_COMMENT = {"python": "#", "ruby": "#", "bash": "#", "php": "#",
                "yaml": "#", "lua": "--"}
BLOCK_LANGS = {"javascript", "typescript", "tsx", "go", "rust", "java", "kotlin",
               "c", "cpp", "csharp", "php", "swift", "scala"}

MARKDOWN_EXTS = {".md", ".markdown"}
TEXT_FALLBACK_EXTS = {".txt", ".rst"}

# 内置跳过目录（追加 dot 目录一律跳过）；用户可在 exclude_dirs 追加
NOISE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", "target",
    ".bawcode", "_recycle", ".zcode", ".idea", ".vscode", ".tox", ".eggs",
    "__pypackages__", ".next", ".nuxt", "coverage",
}

MAX_FILES = 3000             # 索引文件数上限（防失控仓库）
MAX_FILE_BYTES = 256 * 1024  # 单文件上限
_CHUNK_MAX_CHARS = 8000      # 单切片内容截断保护（异常巨大的函数）
_DOC_MAX_CHARS = 600         # 单切片 doc（注释/docstring）截断
_EMB_BATCH = 32              # 嵌入 API 单批条数
_EMB_MAX_CHUNKS = 5000       # 嵌入切片数上限（防超大规模仓库）

_INDEX_VERSION = 1


def setup(ctx):
    storage = ctx.storage_dir()  # {workspace}/.bawcode/plugin-data/rag/
    index_file = storage / "index.json"
    emb_file = storage / "embeddings.json"
    state_file = storage / "state.json"

    docs_index: Dict[str, dict] = {}   # rel -> {"stat": (mt,sz), "chunks": [...]}
    embs: Dict[str, List[float]] = {}  # uid -> 向量（匹配 embeddings.json 的 model）
    emb_model_used = ""                # 现存向量所属模型（不匹配即视为不可用）
    proj_enabled: Optional[bool] = None  # set_rag 写入的项目级开关（None=随配置）
    recall_cache: Tuple[str, str, List[str]] = ("", "", [])  # (mode, msg, sections)
    parsers: Dict[str, Any] = {}
    meta = {"built_at": "", "workspace": ""}
    lock = threading.Lock()

    # ---------- 配置 ----------

    def _cfg() -> dict:
        data = (getattr(ctx.config, "data", None) or {})
        persisted = dict((data.get("plugins_config") or {}).get("rag") or {})
        out = dict(ctx.settings)
        out.update(persisted)
        return out

    def _enabled() -> bool:
        if proj_enabled is not None:
            return proj_enabled
        return bool(_cfg().get("enabled", True))

    def _mode() -> str:
        return "embedding" if str(_cfg().get("match_mode") or "keyword") == "embedding" else "keyword"

    def _ttl_days() -> int:
        try:
            return max(1, int(_cfg().get("index_ttl_days") or 30))
        except (TypeError, ValueError):
            return 30

    # ---------- 持久化（原子写） ----------

    def _dump(path, obj) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _load_all() -> None:
        nonlocal proj_enabled, emb_model_used
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            proj_enabled = state.get("enabled") if isinstance(state.get("enabled"), bool) else None
        except Exception:
            proj_enabled = None
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data and data.get("version") == _INDEX_VERSION:
            built = str(data.get("built_at") or "")
            age = _age_days(built)
            if age is not None and age > _ttl_days():
                ctx.log.info("RAG 索引已超 %d 天（%.1f 天），废弃删除", _ttl_days(), age)
                _clear_files()
                embs.clear()
                emb_model_used = ""
                return  # 索引已废弃，向量随之失效，不再加载
            meta["built_at"] = built
            meta["workspace"] = str(data.get("workspace") or "")
            files = data.get("files") or {}
            docs_index.clear()
            for rel, chunks in (data.get("chunks_by_file") or {}).items():
                st = files.get(rel)
                docs_index[rel] = {"stat": tuple(st) if st else None, "chunks": chunks}
        try:
            edata = json.loads(emb_file.read_text(encoding="utf-8"))
            emb_model_used = str(edata.get("model") or "")
            embs.update(edata.get("vecs") or {})
        except Exception:
            pass

    def _age_days(built_iso: str) -> Optional[float]:
        if not built_iso:
            return None
        try:
            delta = datetime.now() - datetime.fromisoformat(built_iso)
            return delta.total_seconds() / 86400
        except ValueError:
            return None

    def _clear_files() -> None:
        for f in (index_file, emb_file):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    def _persist(stats: dict) -> None:
        meta["built_at"] = datetime.now().isoformat(timespec="seconds")
        meta["workspace"] = str(ctx.workspace)
        _dump(index_file, {
            "version": _INDEX_VERSION,
            "built_at": meta["built_at"],
            "workspace": meta["workspace"],
            "ttl_days": _ttl_days(),
            "files": {rel: list(e["stat"] or []) for rel, e in docs_index.items()},
            "chunks_by_file": {rel: e["chunks"] for rel, e in docs_index.items()},
            "stats": stats,
        })
        if emb_model_used:
            _dump(emb_file, {"model": emb_model_used, "vecs": embs})

    # ---------- doc 匹配基提取（注释 + docstring） ----------

    _PY_DOCSTRING_RE = re.compile(r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')')
    _LINE_RE = {c: re.compile(re.escape(c) + r"[^\n]*") for c in set(LINE_COMMENT.values())}
    _SLASH_LINE_RE = re.compile(r"//[^\n]*")
    _BLOCK_RE = re.compile(r"/\*[\s\S]*?\*/")

    def _extract_doc(text: str, lang: str, kind: str) -> str:
        """切片的自然语言匹配基：注释 + docstring（markdown/text 即正文本身）"""
        if kind in ("file", "heading") and lang in ("markdown", "text"):
            return text[:_DOC_MAX_CHARS]
        parts: List[str] = []
        if lang == "python":
            m = _PY_DOCSTRING_RE.search(text)
            if m:
                parts.append(m.group(1).strip("\"'"))
            lc = _LINE_RE["#"]
        elif lang in LINE_COMMENT:
            lc = _LINE_RE[LINE_COMMENT[lang]]
        else:
            lc = None
        if lang in BLOCK_LANGS:
            parts.extend(x.strip("/* \t") for x in _BLOCK_RE.findall(text))
            parts.extend(x.strip("/ \t") for x in _SLASH_LINE_RE.findall(text))
        if lc is not None:
            parts.extend(x.lstrip("#-").strip() for x in lc.findall(text))
        doc = "\n".join(p for p in (x.strip() for x in parts) if p)
        return doc[:_DOC_MAX_CHARS]

    # ---------- AST 切片 ----------

    def _parser_for(lang: str):
        p = parsers.get(lang)
        if p is None:
            p = _get_parser(lang)
            parsers[lang] = p
        return p

    def _grammar_types(lang: str) -> set:
        """枚举语法全部合法节点名（0.26 无 Language.node_types，用 node_kind_* 内省）"""
        L = _parser_for(lang).language
        names = set()
        for i in range(L.node_kind_count):
            try:
                n = L.node_kind_for_id(i)
            except Exception:
                continue
            if isinstance(n, str) and n:
                names.add(n)
        return names

    def _node_name(node, src: bytes) -> Optional[str]:
        fld = NAME_FIELD_OVERRIDE.get(node.type, "name")
        n = node.child_by_field_name(fld)
        if n is not None:
            return src[n.start_byte:n.end_byte].decode("utf-8", "ignore")
        for ch in node.children:
            if ch.type in ("identifier", "constant", "property_identifier",
                           "type_identifier", "field_identifier"):
                return src[ch.start_byte:ch.end_byte].decode("utf-8", "ignore")
        return None

    def _route_of(node, src: bytes) -> List[str]:
        route: List[str] = []
        cur = node.parent
        while cur is not None:
            if cur.type in CLASS_LIKE:
                name = _node_name(cur, src)
                if name:
                    route.insert(0, name)
            cur = cur.parent
        return route

    def _parse_code(rel: str, lang: str, def_types: set, data: bytes) -> List[dict]:
        if not def_types:
            return []
        parser = _parser_for(lang)
        tree = parser.parse(data)
        q = _ts.Query(tree.language, "(" + ") @d (".join(sorted(def_types)) + ") @d")
        caps = _ts.QueryCursor(q).captures(tree.root_node)
        chunks: List[dict] = []
        seen: set = set()
        for node in caps.get("d", []):
            if node.start_byte in seen:
                continue
            seen.add(node.start_byte)
            start_node = node
            # python 装饰器：content 扩到 decorated_definition（含 @ 行）
            if node.parent is not None and node.parent.type == "decorated_definition":
                start_node = node.parent
            route = _route_of(node, data)
            name = _node_name(node, data)
            if name:
                route = route + [name]
            text = data[start_node.start_byte:start_node.end_byte]
            line0 = data.count(b"\n", 0, start_node.start_byte) + 1
            content = text.decode("utf-8", "ignore")
            if len(content) > _CHUNK_MAX_CHARS:
                content = content[:_CHUNK_MAX_CHARS] + "\n…（切片超长截断）"
            chunks.append({
                "file": rel, "lang": lang, "node": node.type, "route": route,
                "content": content, "line0": line0,
                "line1": line0 + text.count(b"\n"),
                "doc": _extract_doc(content, lang, node.type),
            })
        return chunks

    def _parse_markdown(rel: str, text: str) -> List[dict]:
        lines = text.splitlines()
        heads = [i for i, ln in enumerate(lines) if re.match(r"^#{1,6}\s", ln)]
        if not heads:
            return [_mk_chunk(rel, "markdown", "file", [], text)]
        heads.append(len(lines))
        chunks = []
        for a, b in zip(heads, heads[1:]):
            if a >= len(lines):
                break
            section = "\n".join(lines[a:b]).rstrip()
            if not section:
                continue
            title = re.sub(r"^#+\s*", "", lines[a]).strip()
            chunks.append(_mk_chunk(rel, "markdown", "heading", [title],
                                    section, line0=a + 1, line1=b))
        return chunks

    def _mk_chunk(rel: str, lang: str, node_type: str, route: List[str],
                  content: str, line0: int = 1, line1: int = 0) -> dict:
        if len(content) > _CHUNK_MAX_CHARS:
            content = content[:_CHUNK_MAX_CHARS] + "\n…（切片超长截断）"
        if line1 <= 0:
            line1 = line0 + content.count("\n")
        return {
            "file": rel, "lang": lang, "node": node_type, "route": route,
            "content": content, "line0": line0, "line1": line1,
            "doc": _extract_doc(content, lang, node_type),
        }

    def _scan(root: str, excludes: set) -> Iterator[Tuple[str, str, os.stat_result]]:
        count = 0
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            dirnames[:] = [d for d in dirnames
                           if d not in NOISE_DIRS and not d.startswith(".")
                           and d not in excludes]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                ext = os.path.splitext(fn)[1].lower()
                if ext not in LANG_BY_EXT and ext not in MARKDOWN_EXTS \
                        and ext not in TEXT_FALLBACK_EXTS:
                    continue
                count += 1
                if count > MAX_FILES:
                    return
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue
                yield rel, full, st

    # ---------- 嵌入 ----------

    def _emb_config() -> Tuple[str, str, str]:
        cfg = _cfg()
        return (str(cfg.get("embedding_base_url") or ""),
                str(cfg.get("embedding_model") or ""),
                str(cfg.get("embedding_api_key") or ""))

    def _emb_ready() -> bool:
        base, model, _ = _emb_config()
        return _mode() == "embedding" and bool(base) and bool(model)

    def _embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
        """OpenAI 兼容 /v1/embeddings；失败返回 None（调用方回落 keyword）"""
        base, model, key = _emb_config()
        if not base or not model:
            return None
        try:
            from openai import OpenAI

            client = OpenAI(base_url=base, api_key=key or "EMPTY", timeout=30, max_retries=1)
            out: List[Optional[List[float]]] = [None] * len(texts)
            for i in range(0, len(texts), _EMB_BATCH):
                resp = client.embeddings.create(model=model, input=texts[i:i + _EMB_BATCH])
                for d in resp.data:
                    out[i + d.index] = [round(v, 5) for v in d.embedding]
            if any(v is None for v in out):
                return None
            return out  # type: ignore[return-value]
        except Exception as e:
            ctx.log.warn("RAG 嵌入失败，回落关键词匹配: %s", e)
            return None

    def _cosine(a: List[float], b: List[float]) -> float:
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    # ---------- 索引构建（手动触发） ----------

    def refresh() -> dict:
        """增量重建：只重解析 (mtime, size) 变化的文件；embedding 模式下为
        变化文件中带 doc 的切片补算向量。构建后持久化。"""
        nonlocal emb_model_used
        if not _TS_OK:
            return {"files": 0, "chunks": 0, "changed": 0, "elapsed": 0.0, "embedded": 0}
        cfg = _cfg()
        excludes = {d.strip() for d in str(cfg.get("exclude_dirs") or "").split(",") if d.strip()}
        t0 = time.monotonic()
        changed = 0
        nfiles = 0
        nchunks = 0
        with lock:
            root = str(ctx.workspace)
            seen = set()
            for rel, full, st in _scan(root, excludes):
                nfiles += 1
                seen.add(rel)
                key = (st.st_mtime_ns, st.st_size)
                ent = docs_index.get(rel)
                if ent and ent["stat"] == key:
                    nchunks += len(ent["chunks"])
                    continue
                ext = os.path.splitext(rel)[1].lower()
                try:
                    with open(full, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                if ext in LANG_BY_EXT:
                    lang, def_types = LANG_BY_EXT[ext]
                    try:
                        valid = _grammar_types(lang)
                        chunks = _parse_code(rel, lang, def_types & valid, data)
                    except Exception:
                        chunks = []
                    if not chunks:  # 语法异常/无定义节点 → 整文件一片兜底
                        chunks = [_mk_chunk(rel, lang, "file", [],
                                            data.decode("utf-8", "ignore"))]
                elif ext in MARKDOWN_EXTS:
                    chunks = _parse_markdown(rel, data.decode("utf-8", "ignore"))
                else:
                    chunks = [_mk_chunk(rel, "text", "file", [],
                                        data.decode("utf-8", "ignore"))]
                docs_index[rel] = {"stat": key, "chunks": chunks}
                nchunks += len(chunks)
                changed += 1
            for rel in [r for r in docs_index if r not in seen]:
                del docs_index[rel]
            # 向量维护：模型变更即全量失效；uid 失效清理
            model_now = _emb_config()[1]
            if emb_model_used and model_now and emb_model_used != model_now:
                embs.clear()
                emb_model_used = ""
            live_uids = set()
            for rel, ent in docs_index.items():
                for ch in ent["chunks"]:
                    live_uids.add(_uid(ch))
            for uid in [u for u in embs if u not in live_uids]:
                embs.pop(uid, None)
            # 补算清单在锁内取快照；嵌入请求在锁外执行（网络 I/O 持锁会阻塞召回与状态查询）
            todo = [(_uid(ch), ch["doc"]) for ent in docs_index.values()
                    for ch in ent["chunks"]
                    if _uid(ch) not in embs and ch.get("doc")]
        embedded = 0
        if todo and _emb_ready() and (not emb_model_used or emb_model_used == model_now):
            todo = todo[:_EMB_MAX_CHUNKS]
            vecs = _embed_texts([t for _, t in todo])
            if vecs is not None:
                with lock:
                    for (uid, _), v in zip(todo, vecs):
                        embs[uid] = v
                    embedded = len(todo)
                    emb_model_used = model_now
        stats = {"files": nfiles, "chunks": nchunks, "changed": changed,
                 "elapsed": time.monotonic() - t0, "embedded": embedded}
        with lock:
            _persist(stats)
        return stats

    def _uid(ch: dict) -> str:
        # uid 含内容指纹：行内编辑（起始行不变）也能让旧向量失效重算，避免旧向量持续召回
        digest = hashlib.md5(ch["doc"].encode("utf-8")).hexdigest()[:8]
        return f"{ch['file']}#{ch['line0']}#{digest}"

    # ---------- 召回 ----------

    _SPLIT_RE = re.compile(r"[\s,;:()\[\]{}<>\"'`~!@#\$%\^&\*\+\=\\\|/\?？，。：；（）【】！？、]")

    # 英文虚词/问句引导词：命中无区分度（"python" 含 "on" 之类的假阳性）
    _STOPWORDS = {
        "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is",
        "are", "it", "its", "be", "been", "was", "how", "what", "when",
        "where", "which", "who", "this", "that", "these", "those", "with",
        "as", "at", "by", "from", "into", "not", "no", "do", "does", "did",
        "can", "could", "should", "would", "will", "shall", "may", "might",
        "you", "your", "we", "our", "my", "me", "he", "she", "they", "them",
    }

    def _tokens(query: str) -> List[str]:
        return [t for t in _SPLIT_RE.split((query or "").lower())
                if len(t) >= 2 and t not in _STOPWORDS][:16]

    def _format_chunk(ch: dict) -> str:
        route = " > ".join([ch["file"]] + ch["route"])
        fence = "```"
        lang_tag = "" if ch["lang"] in ("markdown", "text") else ch["lang"]
        return (f"[{route} | {ch['lang']} | L{ch.get('line0', '?')}-L{ch.get('line1', '?')}]\n"
                f"{fence}{lang_tag}\n{ch['content']}\n{fence}")

    def _all_chunks() -> List[dict]:
        with lock:
            return [ch for e in docs_index.values() for ch in e["chunks"]]

    def _search_keyword(query: str, top_k: int, doc_w: float, route_w: float,
                        file_w: float, content_w: float) -> List[Tuple[float, dict]]:
        toks = _tokens(query)
        if not toks:
            return []
        scored: List[Tuple[float, dict]] = []
        for ch in _all_chunks():
            route = " > ".join([ch["file"]] + ch["route"]).lower()
            s = 0.0
            for t in toks:
                if t in ch.get("doc", "").lower():
                    s += doc_w
                if t in route:
                    s += route_w
                if t in ch["file"].lower():
                    s += file_w
                if t in ch["content"].lower():
                    s += content_w
            if s:
                scored.append((s, ch))
        # 同分偏向更具体的切片：路由更深（方法 > 类）、内容更紧凑者优先
        scored.sort(key=lambda x: (-x[0], -len(x[1]["route"]), len(x[1]["content"])))
        return scored[:top_k]

    def _search_embedding(message: str, top_k: int) -> Optional[List[Tuple[float, dict]]]:
        """doc 向量召回；无向量/模型不匹配/调用失败返回 None（回落 keyword）"""
        if emb_model_used != _emb_config()[1] or not embs:
            return None
        qv = _embed_texts([message])
        if qv is None:
            return None
        uid2chunk = {_uid(ch): ch for ch in _all_chunks()}
        scored = []
        for uid, vec in embs.items():
            ch = uid2chunk.get(uid)
            if ch is None:
                continue
            sim = _cosine(qv[0], vec)
            if sim >= float(_cfg().get("embedding_min_score") or 0.15):
                scored.append((sim, ch))
        scored.sort(key=lambda x: (-x[0], -len(x[1]["route"]), len(x[1]["content"])))
        return scored[:top_k]

    def _within_budget(sections: List[str], budget: int) -> List[str]:
        out: List[str] = []
        used = 0
        for sec in sections:
            if used + len(sec) > budget and out:
                break
            if len(sec) > budget:
                sec = sec[:budget]
            out.append(sec)
            used += len(sec)
        return out

    def _recall(message: str) -> List[str]:
        """主动召回：消息驱动，embedding 优先（配置时），回落 keyword。
        doc 是主要匹配基；keyword 模式代码体命中权重压低。"""
        cfg = _cfg()
        top_k = int(cfg.get("top_k") or 3)
        budget = int(cfg.get("budget_chars") or 4000)
        mode = _mode()
        hits: Optional[List[Tuple[float, dict]]] = None
        if mode == "embedding":
            hits = _search_embedding(message, top_k)
        if hits is None:
            hits = _search_keyword(message, top_k, doc_w=5, route_w=2,
                                   file_w=1, content_w=0.5)
        return _within_budget([_format_chunk(ch) for _, ch in hits], budget)

    # ---------- 模型可见工具 ----------

    @ctx.register_tool(
        name="rag_search",
        description="Search the project RAG index by keywords; returns matching code chunks with structure info (file > class > method, language, line range)",
        usage="rag_search <keywords> [top_k]",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords: symbol/class/function names or comment words"},
                "top_k": {"type": "integer", "description": "Max results to return, default from config top_k"},
            },
            "required": ["query"],
        },
    )
    def rag_search(query: str, top_k: int = 0) -> str:
        if not _enabled():
            return "当前项目 RAG 已关闭（可用 set_rag 工具开启）"
        if not _TS_OK:
            return ("tree-sitter 依赖未安装（pip install tree_sitter "
                    "tree-sitter-language-pack），结构索引不可用")
        if not docs_index:
            return "索引为空：先用 rag_index 工具（或 /rag build）手动建立索引"
        top_k = int(top_k or _cfg().get("top_k") or 3)
        hits = _search_keyword(query, top_k, doc_w=1, route_w=4,
                               file_w=1, content_w=2)
        if not hits:
            return "（无命中；试试类名/函数名等符号关键词）"
        return "\n---\n".join(_within_budget([_format_chunk(ch) for _, ch in hits],
                                             int(_cfg().get("budget_chars") or 4000)))

    @ctx.register_tool(
        name="rag_index",
        description="Manually rebuild the project RAG structure index (file tree × AST chunks with comment/docstring basis) and persist it; returns stats",
        usage="rag_index",
        schema={"type": "object", "properties": {}},
    )
    def rag_index() -> str:
        if not _TS_OK:
            return ("tree-sitter 依赖未安装（pip install tree_sitter "
                    "tree-sitter-language-pack），结构索引不可用")
        st = refresh()
        extra = f"，嵌入向量 {st['embedded']} 条" if st.get("embedded") else ""
        return (f"索引完成并已持久化：源文件 {st['files']} 个 / 切片 {st['chunks']} 条，"
                f"重解析 {st['changed']} 个{extra}，耗时 {st['elapsed']:.2f}s")

    @ctx.register_tool(
        name="set_rag",
        description="Enable or disable RAG for the current project (affects proactive recall and rag_search)",
        usage="set_rag <on|off>",
        schema={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean",
                            "description": "true = enable RAG for this project, false = disable"},
            },
            "required": ["enabled"],
        },
    )
    def set_rag(enabled: bool) -> str:
        nonlocal proj_enabled, recall_cache
        proj_enabled = bool(enabled)
        with lock:
            _dump(state_file, {"enabled": proj_enabled})
            recall_cache = ("", "", [])
        return f"本项目 RAG 已{'开启' if proj_enabled else '关闭'}"

    # ---------- 每回合主动召回（用户消息驱动） ----------

    def _supplement(payload) -> Optional[str]:
        nonlocal recall_cache
        if not _enabled():
            return None
        if not _TS_OK:
            return None
        message = str((payload or {}).get("user_message") or "")
        if not message.strip() or not docs_index:
            return None
        cfg = _cfg()
        mode = _mode()
        c_mode, c_msg, c_secs = recall_cache
        if c_msg == message and c_mode == mode:
            sections = c_secs
        else:
            sections = _recall(message)
            recall_cache = (mode, message, sections)
        if not sections:
            return None
        return f"{cfg.get('prefix') or '项目RAG补充'}:\n" + "\n---\n".join(sections)

    ctx.register_context_supplement(_supplement)

    # ---------- 用户命令 /rag ----------

    def _rag_cmd(cctx, args: str) -> str:
        sub = (args or "").strip().split()
        cmd = sub[0].lower() if sub else "status"
        if cmd == "build":
            return rag_index()
        if cmd == "on":
            return set_rag(True)
        if cmd == "off":
            return set_rag(False)
        if cmd == "clear":
            with lock:
                _clear_files()
                docs_index.clear()
                embs.clear()
                meta["built_at"] = ""
            return "已清除索引文件（set_rag 的项目开关保留）"
        # status
        with lock:
            nchunks = sum(len(e["chunks"]) for e in docs_index.values())
            nfiles_idx = len(docs_index)
            has_vecs = bool(embs) and emb_model_used == _emb_config()[1]
        age = _age_days(meta["built_at"])
        base, model, _ = _emb_config()
        emb_state = "关闭" if _mode() == "keyword" else (
            f"{model}@{base}（向量{'就绪' if has_vecs else '未构建，请 /rag build'}）")
        return ("RAG 状态：{}\n索引：{}（{} 文件 / {} 切片，{}）\n匹配模式：{}\n"
                "索引位置：{}".format(
                    "开启" if _enabled() else "关闭",
                    "已构建" if meta["built_at"] else "未构建（/rag build 手动触发）",
                    nfiles_idx, nchunks,
                    f"年龄 {age:.1f} 天" if age is not None else "-",
                    "嵌入模型 " + emb_state if _mode() == "embedding" else "关键词",
                    index_file))

    ctx.register_command(
        "/rag",
        hint="RAG：状态/手动建索引/开关/清除",
        usage="/rag [status|build|on|off|clear]",
        handler=_rag_cmd,
    )

    # ---------- 装载：读持久化索引（超 TTL 自动废弃） ----------
    _load_all()
    if docs_index:
        ctx.log.info("RAG 索引已从磁盘恢复：%d 文件（建于 %s）",
                     len(docs_index), meta["built_at"])
