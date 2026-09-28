#项目级 RAG：当前为占位实现——内存文档列表 + 关键词匹配兜底检索。
#真实向量库/持久化通过 hooks 的 rag_add / rag_query 外部接口整体接管
#（config.external_apis.rag_add / rag_query 填 URL 即可挂接）。
from datetime import datetime
from typing import Callable, List, Optional

from core import hooks
from core.log import get_logger

log = get_logger("rag")


class RagStore:
    """项目级 RAG 占位存储：写入内存文档列表，检索优先走外部接口，无实现时做关键词匹配"""

    def __init__(self) -> None:
        self.docs: List[dict] = []

    def add(self, text: str, source: str = "", external_handler: Optional[Callable] = None) -> None:
        """写入一篇文档；同时触发 rag_add 扩展点（外部向量库可在该处落库）"""
        self.docs.append(
            {"text": text, "source": source, "time": datetime.now().isoformat(timespec="seconds")}
        )
        log.info("RAG 写入: source=%s 共%d篇", source or "-", len(self.docs))
        hooks.call_user_participating("rag_add", {"text": text, "source": source}, handler=external_handler)

    def query(self, query: str, external_handler: Optional[Callable] = None) -> str:
        """检索：外部接口优先；无实现时按空格分词做包含匹配（命中前 3 篇、各截 200 字）"""
        result = hooks.call_user_participating(
            "rag_query",
            {"query": query},
            handler=external_handler,
            default=None,
        )
        if isinstance(result, dict) and result.get("content"):
            return result["content"]
        if isinstance(result, str) and result:
            return result
        keywords = [w for w in query.replace("\n", " ").split() if len(w) >= 2]
        hits = []
        for doc in self.docs:
            text = doc.get("text", "")
            if any(k in text for k in keywords):
                hits.append(text[:200])
            if len(hits) >= 3:
                break
        return "\n---\n".join(hits)


_store: Optional[RagStore] = None


def get_store() -> RagStore:
    """进程级 RAG 单例"""
    global _store
    if _store is None:
        _store = RagStore()
    return _store
