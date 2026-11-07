# token 计算：优先读取API usage，否则用 tiktoken 真实分词
import json
from typing import Iterable, List, Optional, Tuple

try:
    import tiktoken

    HAS_TIKTOKEN = True
except ImportError:
    tiktoken = None
    HAS_TIKTOKEN = False

# model → encoding 名称
_FALLBACK_ENCODING = "cl100k_base"


def _encoding_for_model(model: str):
    if not HAS_TIKTOKEN or tiktoken is None:
        return None
    model = (model or "").lower()
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        pass
    # 常见兼容模型映射
    if "gpt-4o" in model or "o200k" in model:
        name = "o200k_base"
    elif "gpt-3.5" in model or "gpt-4" in model or "deepseek" in model or "qwen" in model:
        name = "cl100k_base"
    else:
        name = _FALLBACK_ENCODING
    try:
        return tiktoken.get_encoding(name)
    except Exception:
        try:
            return tiktoken.get_encoding(_FALLBACK_ENCODING)
        except Exception:
            return None


def count_tokens(text: str, model: str = "") -> int:
    """使用 tiktoken 计算 token 数；无 tiktoken 时抛错由调用方处理"""
    if not text:
        return 0
    enc = _encoding_for_model(model)
    if enc is None:
        raise RuntimeError("tiktoken 不可用，无法精确计 token")
    return len(enc.encode(text, disallowed_special=()))


def _count_single_message(msg: dict, model: str = "") -> int:
    """单条消息 token（OpenAI 消息结构，与 count_message_tokens 同口径）"""
    content = msg.get("content") or ""
    image_count = 0
    if isinstance(content, list):
        # 数组形态（多模态）：text 片段分词计价，图片块按常数计（无法按分辨率精确估算）
        image_count = sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
        content = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    elif not isinstance(content, str):
        content = str(content)
    total = 768 * image_count  # 单图粗略计价，与 memory._IMAGE_TOKEN_ESTIMATE 同值
    total += 4  # 每条消息开销
    total += count_tokens(str(msg.get("role") or ""), model)
    total += count_tokens(content, model)
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        total += count_tokens(str(tool_calls), model)
    return total


def count_message_tokens(messages: Iterable[dict], model: str = "") -> int:
    """按 OpenAI 消息结构估算完整请求 token（分词真实，开销按官方近似公式）"""
    total = 0
    for msg in messages:
        total += _count_single_message(msg, model)
    return total + 2


def count_tool_defs_tokens(tools, model: str = "") -> int:
    """工具定义 token：schema JSON 分词，tiktoken 不可用时按字符数折半"""
    if not tools:
        return 0
    try:
        text = json.dumps(tools, ensure_ascii=False)
    except (TypeError, ValueError):
        return 0
    try:
        return count_tokens(text, model)
    except Exception:
        return max(1, len(text) // 2)


class TokenMeter:
    """LLM 实例 token 计量：优先记录 API usage，否则用分词器测量上下文

    分类预算（breakdown）：请求预算按 system_prompt/tools/mcp/history/memory 分开记录，
    总数仍以 API usage（缺省本地分词）为准；分项恒为本地分词估算口径，UI 只显示总数，
    /context 命令查看分项。"""

    # 分类键与展示顺序
    BREAKDOWN_KEYS = ("system_prompt", "tools", "mcp", "history", "memory")
    BREAKDOWN_LABELS = {
        "system_prompt": "系统提示词",
        "tools": "工具定义(内置)",
        "mcp": "工具定义(MCP)",
        "history": "会话历史",
        "memory": "记忆/补充",
    }

    def __init__(self, model: str = ""):
        self.model = model
        self.api_prompt_tokens = 0
        self.api_completion_tokens = 0
        self.api_total_tokens = 0
        # 累计缓存命中 token：OpenAI 系在 prompt_tokens_details.cached_tokens，
        # DeepSeek 在 prompt_cache_hit_tokens；未返回时保持 0
        self.cached_prompt_tokens = 0
        self.last_context_tokens = 0
        self.context_window = 0
        self.balance: Optional[float] = None
        self.balance_currency: str = ""
        self.balance_source: str = ""
        self.source = "none"  # api | tokenizer | none
        self.last_error = ""
        # 本轮输出累计（本地分词，不依赖提供商 usage）：各轮生成 completion
        self.turn_output_tokens = 0
        self.turn_peak_context_tokens = 0
        # 分类预算：最近一次请求各来源 token（本地分词）
        self.breakdown: dict = {k: 0 for k in self.BREAKDOWN_KEYS}

    def begin_turn(self) -> None:
        """回合开始：复位本轮输出 token 累计"""
        self.turn_output_tokens = 0
        self.turn_peak_context_tokens = 0

    def reset_context(self) -> None:
        """清空当前上下文计量，保留实例累计 API 用量。"""
        self.last_context_tokens = 0
        self.turn_output_tokens = 0
        self.turn_peak_context_tokens = 0
        self.breakdown = {k: 0 for k in self.BREAKDOWN_KEYS}
        self.source = "none"
        self.last_error = ""

    def count_text(self, text: str) -> int:
        """本地分词计数；tiktoken 不可用返回 0"""
        try:
            return count_tokens(text or "", self.model)
        except Exception:
            return 0

    def add_turn_output(self, n: int) -> None:
        self.turn_output_tokens += max(0, int(n or 0))

    def set_model(self, model: str, context_window: int = 0) -> None:
        self.model = model or self.model
        if context_window:
            self.context_window = context_window

    def record_api_usage(self, usage: Optional[dict]) -> None:
        """写入 API 返回的 usage"""
        if not usage:
            return
        try:
            self.api_prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.api_completion_tokens += int(usage.get("completion_tokens") or 0)
            self.api_total_tokens += int(
                usage.get("total_tokens")
                or ((usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0))
            )
            self.source = "api"
            self.last_context_tokens = int(usage.get("prompt_tokens") or self.last_context_tokens)
            self.turn_peak_context_tokens = max(self.turn_peak_context_tokens, self.last_context_tokens)
            details = usage.get("prompt_tokens_details")
            cached = details.get("cached_tokens") if isinstance(details, dict) else None
            if cached is None:
                cached = usage.get("prompt_cache_hit_tokens") or 0
            self.cached_prompt_tokens += int(cached or 0)
        except (TypeError, ValueError):
            pass

    def measure_context(
        self,
        messages: List[dict],
        tools_grouped: Optional[tuple] = None,
        system_parts: Optional[dict] = None,
    ) -> int:
        """用 tiktoken 计算当前上下文 token（API 未返回 usage 时的精确来源）

        tools_grouped=(内置定义, MCP定义)：可选，分类记录工具定义分项。
        system_parts={"core": n, "environment": n, "memory": n}：可选，系统提示词
        内部拆分（组装方对三段分别计数），并入提示词的记忆块单列 memory 分项。
        分项失败不影响总数。"""
        try:
            tokens = count_message_tokens(messages, self.model)
            self.last_context_tokens = tokens
            self.turn_peak_context_tokens = max(self.turn_peak_context_tokens, tokens)
            self.source = "tokenizer" if self.source != "api" else self.source
            self._measure_breakdown(messages, tools_grouped, system_parts)
            return tokens
        except RuntimeError as e:
            self.last_error = str(e)
            return self.last_context_tokens

    def _measure_breakdown(
        self,
        messages: List[dict],
        tools_grouped: Optional[tuple] = None,
        system_parts: Optional[dict] = None,
    ) -> None:
        """分类计量（本地分词）：首条 system=系统提示词，其余 system=记忆/补充，余下=历史"""
        try:
            bd: dict = {k: 0 for k in self.BREAKDOWN_KEYS}
            for i, msg in enumerate(messages):
                if msg.get("role") != "system":
                    bd["history"] += _count_single_message(msg, self.model)
                    continue
                n = _count_single_message(msg, self.model)
                if i == 0:
                    mem = int((system_parts or {}).get("memory") or 0)
                    if mem > 0:
                        bd["system_prompt"] += max(0, n - mem)
                        bd["memory"] += mem
                    else:
                        bd["system_prompt"] += n
                else:
                    bd["memory"] += n
            if tools_grouped:
                builtin, mcp = tools_grouped
                if builtin:
                    bd["tools"] += count_tool_defs_tokens(builtin, self.model)
                if mcp:
                    bd["mcp"] += count_tool_defs_tokens(mcp, self.model)
            self.breakdown = bd
        except Exception as e:  # 分项是辅助信息，失败只留诊断
            self.last_error = f"breakdown: {e}"

    def breakdown_rows(self) -> List[Tuple[str, int]]:
        """分项明细（/context 展示用）：按固定顺序返回 (标签, token)"""
        return [
            (self.BREAKDOWN_LABELS.get(k, k), int(self.breakdown.get(k, 0) or 0))
            for k in self.BREAKDOWN_KEYS
        ]

    @property
    def context_tokens(self) -> int:
        """当前上下文占用（用于与 context_window 比）"""
        return self.last_context_tokens

    @property
    def billed_tokens(self) -> int:
        """LLM 实例累计 API 用量（计费展示，不参与 usage_ratio）"""
        return self.api_total_tokens

    @property
    def used_tokens(self) -> int:
        """状态栏占用：当前上下文，而非会话累计"""
        return self.context_tokens

    @property
    def usage_ratio(self) -> float:
        if not self.context_window:
            return 0.0
        return min(1.0, self.context_tokens / float(self.context_window))

    def set_balance(self, amount: Optional[float], currency: str = "", source: str = "") -> None:
        self.balance = amount
        self.balance_currency = currency or ""
        self.balance_source = source or ("api" if amount is not None else "")

    def status_text(self) -> str:
        ctx = self.context_tokens
        billed = self.billed_tokens
        cw = self.context_window
        src = self.source
        if cw > 0:
            pct = self.usage_ratio * 100
            token_part = f"tokens {ctx}/{cw} ({pct:.1f}%) [{src}]"
        else:
            token_part = f"tokens {ctx} [{src}]"
        if billed > 0:
            token_part += f" · 累计 {billed}"
        if self.cached_prompt_tokens > 0:
            token_part += f" · 缓存命中 {self.cached_prompt_tokens}"
        if self.balance is not None:
            bal = f" · 余额 {self.balance}"
            if self.balance_currency:
                bal += f" {self.balance_currency}"
            return token_part + bal
        return token_part
