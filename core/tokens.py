# token 计算：优先 API usage，否则用 tiktoken 真实分词
from typing import Iterable, List, Optional

try:
    import tiktoken

    HAS_TIKTOKEN = True
except ImportError:
    tiktoken = None
    HAS_TIKTOKEN = False

# model → encoding 名称（无法按 model 查找时的回落）
_FALLBACK_ENCODING = "cl100k_base"


def _encoding_for_model(model: str):
    if not HAS_TIKTOKEN:
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


def count_message_tokens(messages: Iterable[dict], model: str = "") -> int:
    """按 OpenAI 消息结构估算完整请求 token（分词真实，开销按官方近似公式）"""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        total += 4  # 每条消息开销
        total += count_tokens(str(msg.get("role") or ""), model)
        total += count_tokens(content, model)
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            total += count_tokens(str(tool_calls), model)
    total += 2
    return total


class TokenMeter:
    """会话 token 计量：优先记录 API usage，否则用分词器累计上下文"""

    def __init__(self, model: str = ""):
        self.model = model
        self.api_prompt_tokens = 0
        self.api_completion_tokens = 0
        self.api_total_tokens = 0
        self.last_context_tokens = 0
        self.context_window = 0
        self.balance: Optional[float] = None
        self.balance_currency: str = ""
        self.balance_source: str = ""
        self.source = "none"  # api | tokenizer | none
        self.last_error = ""

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
        except (TypeError, ValueError):
            pass

    def measure_context(self, messages: List[dict]) -> int:
        """用 tiktoken 计算当前上下文 token（API 未返回 usage 时的精确来源）"""
        try:
            tokens = count_message_tokens(messages, self.model)
            self.last_context_tokens = tokens
            self.source = "tokenizer" if self.source != "api" else self.source
            return tokens
        except RuntimeError as e:
            self.last_error = str(e)
            return self.last_context_tokens

    @property
    def used_tokens(self) -> int:
        """状态栏用量：优先 API total，否则最近一次上下文分词结果"""
        if self.api_total_tokens > 0:
            return self.api_total_tokens
        return self.last_context_tokens

    @property
    def usage_ratio(self) -> float:
        if not self.context_window:
            return 0.0
        return min(1.0, self.used_tokens / float(self.context_window))

    def set_balance(self, amount: Optional[float], currency: str = "", source: str = "") -> None:
        self.balance = amount
        self.balance_currency = currency or ""
        self.balance_source = source or ("api" if amount is not None else "")

    def status_text(self) -> str:
        used = self.used_tokens
        cw = self.context_window
        src = self.source
        if cw > 0:
            pct = self.usage_ratio * 100
            token_part = f"tokens {used}/{cw} ({pct:.1f}%) [{src}]"
        else:
            token_part = f"tokens {used} [{src}]"
        if self.balance is not None:
            bal = f" · 余额 {self.balance}"
            if self.balance_currency:
                bal += f" {self.balance_currency}"
            return token_part + bal
        return token_part
