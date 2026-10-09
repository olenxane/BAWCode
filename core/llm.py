# Agent 核心：LLM 调用、工具策略、token/余额
import json
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from typing import Any, List, Optional

try:
    import openai

    HAS_OPENAI = True
except ImportError:
    openai = None
    HAS_OPENAI = False

from core import hooks
from core import policy
from core import prompt_loader
from core import register
from core import tokens as tokenmod
from core.config import THINKING_OPTIONS, normalize_base_url
from core.log import get_logger

log = get_logger("llm")

# chat() 取消哨兵：异步回合中断时返回，调用方据此丢弃在途回合
CANCELLED = "__CANCELLED__"

# 可通过等待解除的 HTTP 状态（限频/服务端瞬态）：自动重试；其余 4xx（401 鉴权、
# 404 模型不存在、400 参数等确定性失败）直接报错
_RETRYABLE_STATUS = {408, 409, 429}


def _is_retryable_error(e: BaseException) -> bool:
    """判定异常是否属于可通过时间解除的瞬态错误（自动重试与手动重试等待态的依据）"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in _RETRYABLE_STATUS or 500 <= e.code <= 599
    # openai SDK：APIStatusError 族携带 status_code；超时/连接错误无码但属瞬态
    status = getattr(e, "status_code", None)
    if isinstance(status, int) and status > 0:
        return status in _RETRYABLE_STATUS or 500 <= status <= 599
    if type(e).__name__ in ("APITimeoutError", "APIConnectionError"):
        return True
    # 无状态码的网络层异常（URLError/DNS/连接拒绝/超时）
    return isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError))


# 思考文本字段名各厂商不同，按序取首个非空
_REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking_content", "thinking")


def _reasoning_of(mapping) -> str:
    """从响应 message 或流式 delta 取思考文本，取不到返回空串"""
    if not isinstance(mapping, dict):
        return ""
    for key in _REASONING_FIELDS:
        value = mapping.get(key)
        if value:
            return value if isinstance(value, str) else str(value)
    return ""


def get_system_prompt(config=None, files: Optional[List[str]] = None, **kwargs) -> str:
    """从 core/prompts 加载系统提示词（占位符替换）；files 覆盖默认文件组"""
    return prompt_loader.get_system_prompt(config=config, files=files, **kwargs)

class _StreamAggregate:
    """流式 chunk 聚合器：content/reasoning_content/tool_calls 增量累积，
    产出伪 OpenAI 响应（复用 _normalize 第三分支做 arguments 解析/id 补齐/reasoning 分流）。
    parts 列表 + 末次 join，规避 str += 的 O(n²)。"""

    def __init__(self, on_delta=None):
        self.on_delta = on_delta
        self.content_parts: list = []
        self.reasoning_parts: list = []
        self.tool_buckets: dict = {}
        self.tool_order: list = []
        self.usage = None
        self.error = None      # midway 错误：已产出内容后传输中断（不重试，partial 定格）
        self.produced = 0      # 已回调 delta 数（reasoning/content），>0 即"已上屏"

    def _emit(self, kind: str, piece: str) -> None:
        if not piece:
            return
        if self.on_delta is not None:
            try:
                self.on_delta(kind, piece)
            except Exception as e:
                log.warn("流式回调异常（忽略）: %s", e)
        self.produced += 1

    def add_reasoning(self, piece: str) -> None:
        if not piece:
            return
        self.reasoning_parts.append(piece)
        self._emit("reasoning", piece)

    def add_content(self, piece: str) -> None:
        if not piece:
            return
        self.content_parts.append(piece)
        self._emit("content", piece)

    def add_tool_delta(self, index: int, call_delta: dict) -> None:
        bucket = self.tool_buckets.get(index)
        if bucket is None:
            bucket = {"id": "", "name": "", "arguments": ""}
            self.tool_buckets[index] = bucket
            self.tool_order.append(index)
        if call_delta.get("id"):
            bucket["id"] = call_delta["id"]
        fn = call_delta.get("function") or {}
        if fn.get("name"):
            bucket["name"] = fn["name"]
        if fn.get("arguments"):
            bucket["arguments"] += fn["arguments"]

    def set_usage(self, usage) -> None:
        if usage:
            self.usage = usage

    @property
    def produced_any(self) -> bool:
        return self.produced > 0 or bool(self.tool_buckets)

    def to_response(self) -> dict:
        tool_calls = [
            {
                "id": self.tool_buckets[index]["id"],
                "type": "function",
                "function": {
                    "name": self.tool_buckets[index]["name"],
                    "arguments": self.tool_buckets[index]["arguments"] or "{}",
                },
            }
            for index in self.tool_order
        ]
        return {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(self.content_parts),
                        "reasoning_content": "".join(self.reasoning_parts),
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": self.usage or {},
        }


class LLM:
    def __init__(self, config):
        self.config = config
        self.meter = tokenmod.TokenMeter(config.model_name)
        self.meter.context_window = getattr(config, "context_window", 0) or 0
        self.meter.model = config.model_name
        self.client = None
        self._client_provider_id = None
        self._inflight_resp = None
        self._cancel = threading.Event()
        self._build_client()

    # ----- 取消（异步回合中断） -----

    def cancel(self) -> None:
        """请求中断：置标记并关闭在途连接（openai SDK/httpx 与 urllib SSE 句柄均立即生效）。"""
        self._cancel.set()
        client = self.client
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass
        self.client = None
        self._client_provider_id = None
        resp = self._inflight_resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
            self._inflight_resp = None
        log.info("LLM 请求取消：已关闭在途连接")

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def _build_client(self, provider_id: Optional[str] = None, api_key: Optional[str] = None,
                      base_url: Optional[str] = None) -> None:
        """构建 OpenAI 兼容客户端；可按 provider 指定 key/base_url"""
        self.client = None
        pid = provider_id or getattr(self.config, "provider_id", None) or self.config.data.get("active_provider_id")
        key = api_key if api_key is not None else getattr(self.config, "api_key", "")
        base = base_url if base_url is not None else getattr(self.config, "base_url", "") or ""
        base = normalize_base_url(base or "")
        if HAS_OPENAI and key:
            try:
                # max_retries=0：重试统一由外层 retry_times 控制（避免 SDK 默认 2 次叠加成 12 次最坏重试）
                self.client = openai.OpenAI(api_key=key, base_url=base, max_retries=0)
                self._client_provider_id = pid
                log.debug("OpenAI 客户端已构建 provider=%s base=%s", pid, base)
            except Exception as e:
                self.client = None
                log.warn("OpenAI 客户端构建失败: %s", e)
        else:
            log.debug(
                "未构建 OpenAI 客户端: openai=%s api_key=%s provider=%s",
                HAS_OPENAI,
                bool(key),
                pid,
            )

    def refresh_from_config(self, config) -> None:
        self.config = config
        self.meter.set_model(config.model_name, getattr(config, "context_window", 0) or 0)
        self._build_client()
        self.query_balance()

    def resolve_request(self, model: Optional[str] = None) -> dict:
        """按任务/激活模型解析该次请求的 provider 凭证与 model_id

        DeepSeek/SenseNova 等多 provider 场景：model 属于谁就用谁的 api_key/base_url，
        避免「model_id 换了、client 仍是另一家」导致的 404。
        """
        cfg = self.config
        name = model or getattr(cfg, "model_name", None) or cfg.active_model_name()
        row = cfg.resolve_model_row(name)
        if row is None:
            row = cfg.find_model(name)
        if row is None or not row.get("api_key"):
            active_name = cfg.active_model_name()
            active_row = cfg.find_model(active_name)
            if active_row and active_row.get("api_key"):
                log.warn("模型 %s 不可用（无凭证/未找到），回退 %s", name, active_name)
                row = active_row
            else:
                log.warn("模型 %s 不可用，且激活模型亦无凭证", name)
                return {
                    "model_id": getattr(cfg, "model", "") or "",
                    "api_key": getattr(cfg, "api_key", "") or "",
                    "base_url": normalize_base_url(getattr(cfg, "base_url", "") or ""),
                    "provider_id": getattr(cfg, "provider_id", "") or "",
                    "temperature": getattr(cfg, "temperature", 1.0),
                    "max_tokens": getattr(cfg, "max_tokens", 2048),
                    "model_name": name,
                    "ok": False,
                }
        return {
            "model_id": row.get("model_id") or "",
            "api_key": row.get("api_key") or "",
            "base_url": normalize_base_url(row.get("base_url") or ""),
            "provider_id": row.get("provider_id") or "",
            # 0 是合法配置值（贪心解码），判缺省用 is None 而非 or
            "temperature": getattr(cfg, "temperature", 1.0) if row.get("temperature") is None else row.get("temperature"),
            "max_tokens": row.get("max_tokens") or getattr(cfg, "max_tokens", 2048),
            "model_name": row.get("model_name") or name,
            "thinking_effort": row.get("thinking_effort") or "none",
            "ok": True,
        }

    def _headers(self, req: Optional[dict] = None) -> dict:
        key = (req or {}).get("api_key") or getattr(self.config, "api_key", "")
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }

    def _chat_url(self, req: Optional[dict] = None) -> str:
        base = (req or {}).get("base_url") or getattr(self.config, "base_url", "") or ""
        return f"{normalize_base_url(base or '')}/chat/completions"

    def _http_chat(self, payload: dict, req: Optional[dict] = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._chat_url(req), data=body, headers=self._headers(req), method="POST"
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ----- 流式（SSE）：双路径聚合，产出伪 OpenAI 响应复用 _normalize -----

    @staticmethod
    def _feed_chunk(agg: "_StreamAggregate", data: dict) -> None:
        """把单个 SSE chunk / SDK chunk / 完整响应 dict 喂给聚合器（delta 与 message 形态兼容）"""
        if data.get("usage"):
            agg.set_usage(data["usage"])
        choices = data.get("choices") or []
        if not choices:
            return  # usage 终块 choices 为空
        head = choices[0] or {}
        delta = head.get("delta") or head.get("message") or {}
        agg.add_reasoning(_reasoning_of(delta))
        agg.add_content(delta.get("content") or "")
        for i, tc in enumerate(delta.get("tool_calls") or []):
            if isinstance(tc, dict):
                # 流式 delta 带 index；完整响应无 index 时按位置分桶
                agg.add_tool_delta(tc.get("index") if tc.get("index") is not None else i, tc)

    def _finish_stream(self, agg: "_StreamAggregate", exc: Optional[Exception]) -> Optional["_StreamAggregate"]:
        """流循环异常的统一收口：取消→None；已产出→midway 定格；未产出→抛给 chat 回落/重试"""
        if exc is not None and self._cancel.is_set():
            return None
        if exc is None:
            return agg
        if agg.produced_any:
            agg.error = f"流式传输中断: {exc}"
            log.warn("流式传输中断（已产出 %d 片，不重试）: %s", agg.produced, exc)
            return agg
        raise exc

    def _native_chat_stream(self, payload: dict, req: dict, on_delta) -> Optional["_StreamAggregate"]:
        """openai SDK 流式：逐 chunk 聚合并回调增量；返回 None 表示已取消"""
        agg = _StreamAggregate(on_delta)
        resp = self.client.chat.completions.create(
            **payload, stream=True, stream_options={"include_usage": True}
        )
        try:
            for chunk in resp:
                if self._cancel.is_set():
                    return None
                data = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
                self._feed_chunk(agg, data if isinstance(data, dict) else {})
            return self._finish_stream(agg, None)
        except Exception as e:
            return self._finish_stream(agg, e)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _http_chat_stream(self, payload: dict, req: Optional[dict], on_delta) -> Optional["_StreamAggregate"]:
        """urllib 降级流式：SSE 行迭代解析（timeout=120 语义自动变为块间超时）；None 表示已取消"""
        agg = _StreamAggregate(on_delta)
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}
        body = json.dumps(stream_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._chat_url(req), data=body, headers=self._headers(req), method="POST"
        )
        resp = urllib.request.urlopen(request, timeout=120)
        self._inflight_resp = resp
        try:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                # 服务端不支持流式（回了整段 JSON）：同一聚合器优雅降级
                self._feed_chunk(agg, json.loads(resp.read().decode("utf-8")))
                return self._finish_stream(agg, None)
            for raw_line in resp:
                if self._cancel.is_set():
                    return None
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    break
                try:
                    data = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                self._feed_chunk(agg, data)
            return self._finish_stream(agg, None)
        except Exception as e:
            return self._finish_stream(agg, e)
        finally:
            self._inflight_resp = None
            try:
                resp.close()
            except Exception:
                pass

    def _native_chat(self, payload: dict, req: Optional[dict] = None, on_delta=None):
        req = req or {}
        provider_id = req.get("provider_id")
        api_key = req.get("api_key")
        base_url = req.get("base_url")
        if HAS_OPENAI and api_key:
            if self.client is None or self._client_provider_id != provider_id:
                self._build_client(provider_id=provider_id, api_key=api_key, base_url=base_url)
            if self.client is not None:
                if on_delta is not None:
                    return self._native_chat_stream(payload, req, on_delta)
                resp = self.client.chat.completions.create(**payload)
                return resp.model_dump() if hasattr(resp, "model_dump") else resp
        if on_delta is not None:
            return self._http_chat_stream(payload, req, on_delta)
        return self._http_chat(payload, req)

    def query_balance(self) -> Optional[float]:
        """余额查询：provider.balance_url 支持时写入 meter；否则 None"""
        url = getattr(self.config, "balance_url", "") or ""
        if not url:
            self.meter.set_balance(None)
            return None
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log.warn("余额查询失败: %s", e)
            self.meter.set_balance(None)
            return None
        amount = None
        currency = ""
        if isinstance(data, dict):
            for key in ("balance", "total_balance", "available_balance", "amount", "credit"):
                if key in data and data[key] is not None:
                    try:
                        amount = float(data[key])
                        break
                    except (TypeError, ValueError):
                        continue
            for key in ("currency", "unit"):
                if data.get(key):
                    currency = str(data[key])
            if amount is None and isinstance(data.get("data"), dict):
                inner = data["data"]
                for key in ("balance", "available_balance", "total_balance"):
                    if key in inner:
                        try:
                            amount = float(inner[key])
                            break
                        except (TypeError, ValueError):
                            continue
        if amount is not None:
            log.info("余额查询成功: %s%s", amount, currency)
        else:
            log.debug("余额接口未返回可识别字段")
        self.meter.set_balance(amount, currency=currency, source="api")
        return amount

    @staticmethod
    def _ensure_call_id(call_id: Any) -> str:
        return call_id if call_id else f"call_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _map_api_messages(messages: List[dict]) -> List[dict]:
        """组装 DeepSeek/OpenAI Tool Calls 协议消息，透传 tool_call_id / tool_calls"""
        out: List[dict] = []
        for m in messages or []:
            role = m.get("role") or "user"
            content = m.get("content")
            if content is None:
                content = ""
            # content 原样出站：str 或数组形态（多模态图片块）均透传
            item: dict = {"role": role, "content": content if isinstance(content, (str, list)) else str(content)}
            if m.get("name"):
                item["name"] = m["name"]
            if role == "tool":
                # DeepSeek：tool 消息必须携带 tool_call_id；缺 id 不降级为 system（会破坏配对）
                tcid = m.get("tool_call_id")
                if tcid:
                    item["tool_call_id"] = tcid
                else:
                    log.warn("丢弃无 tool_call_id 的 tool 消息: tool=%s", m.get("tool_name") or m.get("name") or "tool")
                    continue
            if role == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    if not isinstance(c, dict):
                        continue
                    fn_name = c.get("name") or (c.get("function") or {}).get("name") or ""
                    args = c.get("arguments")
                    if args is None:
                        args = (c.get("function") or {}).get("arguments") or "{}"
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    calls.append(
                        {
                            "id": LLM._ensure_call_id(c.get("id")),
                            "type": "function",
                            "function": {"name": fn_name, "arguments": args},
                        }
                    )
                if calls:
                    item["tool_calls"] = calls
                    if not item["content"]:
                        item["content"] = None
            out.append(item)
        return out

    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        on_delta=None,
    ) -> dict:
        """调用 LLM（核心扩展点：llm_request）；model 可指定任务专用模型。

        on_delta(kind, piece) 提供（kind ∈ reasoning/content）且 config.llm.stream
        开启时走 SSE 流式：增量经回调上屏，聚合结果与非流式逐字段等价。
        首个 chunk 前失败自动回落非流式；已产出后失败 midway 定格不重试。
        """
        if self._cancel.is_set():
            return {"content": "", "tool_calls": [], "error": CANCELLED}
        self.meter.measure_context(messages)
        req = self.resolve_request(model)
        model_id = req.get("model_id") or req.get("model_name") or ""
        payload = {
            "model": model_id,
            "messages": self._map_api_messages(messages),
            "temperature": req.get("temperature", 1.0) if temperature is None else temperature,
            "max_tokens": int(req.get("max_tokens") or 2048),
        }
        # 思考强度 → reasoning_effort；none 不发，避免厂商因未知参数报错
        # 各厂商思考字段形态不同，后续在此处按 provider 分派，勿散落到调用点
        effort = str(req.get("thinking_effort") or "none").lower()
        if effort != "none" and effort in THINKING_OPTIONS:
            payload["reasoning_effort"] = effort
        if tools:
            payload["tools"] = tools
            # DeepSeek 思考模式不支持 required/指定 function，固定 auto
            payload["tool_choice"] = "auto"
        streaming = bool(on_delta) and bool((self.config.data or {}).get("llm", {}).get("stream", True))
        if not req.get("api_key"):
            log.warn("未配置可用 api_key，LLM 请求被拒绝 model=%s", model_id)
            hooked = hooks.call_hook(
                "llm_request", {"messages": messages, "tools": tools, "payload": payload}, default=None
            )
            if hooked is None:
                return {"content": "", "tool_calls": [], "error": "未配置 api_key"}
            return self._normalize(hooked, keep_reasoning=bool(tools))

        retry_times = int(self.config.data.get("llm", {}).get("retry_times", 3))
        retry_delay = float(self.config.data.get("llm", {}).get("retry_delay", 1.0))
        attempt = 0
        last_error = None
        start = time.monotonic()
        log.debug(
            "LLM 请求 provider=%s model=%s messages=%d tools=%d",
            req.get("provider_id"),
            model_id,
            len(payload["messages"]),
            len(tools or []),
        )
        while attempt <= max(retry_times, 0):
            if self._cancel.is_set():
                return {"content": "", "tool_calls": [], "error": CANCELLED}
            try:
                external = hooks.call_hook(
                    "llm_request",
                    {"messages": messages, "tools": tools, "payload": payload},
                    default=None,
                )
                if external is not None:
                    data = external  # 外部 hook 整段转发，保持非流式语义
                elif streaming:
                    try:
                        agg = self._native_chat(payload, req, on_delta=on_delta)
                    except Exception as stream_err:
                        if self._cancel.is_set():
                            return {"content": "", "tool_calls": [], "error": CANCELLED}
                        # 首个 chunk 前失败：回落非流式（本次尝试内完成；回落自身异常交外层重试）
                        log.warn("流式请求失败，回落非流式: %s", stream_err)
                        data = self._native_chat(payload, req)
                    else:
                        if agg is None:
                            return {"content": "", "tool_calls": [], "error": CANCELLED}
                        if agg.error:
                            # midway：partial 已上屏，不自动重试（重试交由手动重试等待态），
                            # 错误随结果返回
                            result = self._normalize(agg.to_response(), keep_reasoning=bool(tools))
                            self.meter.record_api_usage(result.get("raw", {}).get("usage"))
                            result["error"] = agg.error
                            result["retryable"] = True
                            return result
                        data = agg.to_response()
                else:
                    data = self._native_chat(payload, req)
                result = self._normalize(data, keep_reasoning=bool(tools))
                usage = result.get("raw", {}).get("usage") if isinstance(result.get("raw"), dict) else None
                self.meter.record_api_usage(usage)
                log.info(
                    "LLM 响应 model=%s content=%d字 tool_calls=%d 用时=%.2fs（第%d次尝试）",
                    model_id,
                    len(result.get("content") or ""),
                    len(result.get("tool_calls") or []),
                    time.monotonic() - start,
                    attempt + 1,
                )
                return result
            except Exception as e:
                if self._cancel.is_set():
                    return {"content": "", "tool_calls": [], "error": CANCELLED}
                last_error = e
                attempt += 1
                log.warn("LLM 调用失败(第%d/%d次): %s", attempt, max(retry_times, 0) + 1, e)
                log.debug("异常详情:\n%s", traceback.format_exc())
                if not _is_retryable_error(e):
                    # 401/404/400 等确定性失败：重试无意义，直接报错
                    log.error("不可自动重试的错误（%s.%s），终止重试", type(e).__module__, type(e).__name__)
                    break
                if attempt > max(retry_times, 0):
                    break
                if retry_delay > 0:
                    time.sleep(retry_delay * attempt)
                self._build_client(
                    provider_id=req.get("provider_id"),
                    api_key=req.get("api_key"),
                    base_url=req.get("base_url"),
                )
        log.error("LLM 调用最终失败: %s", last_error)
        return {
            "content": "",
            "tool_calls": [],
            "error": f"LLM 调用失败: {last_error}",
            # 瞬态错误（自动重试已耗尽）允许进入手动重试等待态；确定性失败直接报
            "retryable": _is_retryable_error(last_error) if last_error else False,
        }

    def _normalize(self, data: Any, keep_reasoning: bool = False) -> dict:
        if isinstance(data, dict) and data.get("content") is not None and "tool_calls" in data:
            normalized_calls = []
            for c in data.get("tool_calls") or []:
                if not isinstance(c, dict):
                    continue
                normalized_calls.append({**c, "id": self._ensure_call_id(c.get("id"))})
            return {
                "content": data.get("content") or "",
                "tool_calls": normalized_calls,
                "raw": data.get("raw", data),
                "error": data.get("error"),
            }
        if isinstance(data, dict) and data.get("error") and "choices" not in data:
            return {"content": "", "tool_calls": [], "raw": data, "error": data.get("error")}
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            return {
                "content": str(data.get("result", data)) if isinstance(data, dict) else str(data),
                "tool_calls": [],
                "raw": data if isinstance(data, dict) else {},
            }
        message = choices[0].get("message", {})
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                {
                    "id": self._ensure_call_id(call.get("id")),
                    "name": function.get("name"),
                    "arguments": args,
                    "type": call.get("type") or "function",
                }
            )
        content = message.get("content") or ""
        reasoning = _reasoning_of(message)
        if reasoning and not content and not keep_reasoning:
            # 无工具的内部文本调用（计划/压缩/判定/refine）：仅思考时顶替 content，保住下游取得到文本；
            # agent 工具轮（keep_reasoning=True）不顶替——思考进 thinking 字段，回合末可整体剥离
            content = reasoning
            reasoning = ""
        # 思考与正文并存时 reasoning 单独返回，由调用方以 thinking 字段随消息留档
        return {"content": content, "reasoning": reasoning, "tool_calls": tool_calls, "raw": data}

    def evaluate_tool(self, tool_name: str, args: dict) -> tuple:
        action, reason = policy.evaluate(tool_name, args, getattr(self.config, "mode", policy.MODE_AUTO))
        log.debug("工具策略 %s -> %s(%s)", tool_name, action, reason)
        return action, reason

    def execute_approved_tool(self, call: dict) -> dict:
        """确认通过后执行单个工具；before_tool 钩子可改写 args 或拒绝执行"""
        name = call.get("name")
        args = call.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        args = {k: v for k, v in args.items() if k != "description"}
        # before_tool（变换链）：返回 {"args": {...}} 改写参数；
        # 返回 {"decision": "deny", "message": "..."} 拒绝执行（不进工具本体）
        hooked = hooks.call_hook("before_tool", {"name": name, "args": args}, default=None)
        if isinstance(hooked, dict):
            if str(hooked.get("decision") or "").lower() in ("deny", "denied", "block", "blocked"):
                message = str(hooked.get("message") or f"工具 {name} 被 before_tool 钩子拒绝")
                log.info("工具 %s 被 before_tool 钩子拒绝: %s", name, message)
                return {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "tool_name": name,
                    "content": message,
                    "type": "tool",
                }
            if isinstance(hooked.get("args"), dict):
                args = hooked["args"]
        log.info("执行工具 %s（已确认）", name)
        started = time.monotonic()
        if not register.has_tool(name or ""):
            output = f"工具不存在: {name}"
            log.warn("调用未注册工具: %s", name)
        else:
            try:
                output = register.call(name, **args)
                log.debug("工具 %s 完成，用时 %.2fs", name, time.monotonic() - started)
            except TypeError as e:
                output = f"工具参数错误: {e}"
                log.error("工具 %s 参数错误: %s", name, e)
            except Exception as e:
                output = f"工具执行错误: {e}"
                log.error("工具 %s 执行错误: %s", name, e)
        # 多模态约定：工具返回 dict 可带 "images"（本地图片路径列表），摘出随消息携带，
        # 由 add_tool_result 落为 API 数组形态 content；正文取 content 键，无则序列化其余键
        images = None
        if isinstance(output, dict) and isinstance(output.get("images"), list) and output["images"]:
            images = [str(p) for p in output["images"] if p]
            output = {k: v for k, v in output.items() if k != "images"}
            text = output["content"] if isinstance(output.get("content"), str) else json.dumps(output, ensure_ascii=False)
        else:
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        # after_tool（观察链）：通知执行结果（处理函数异常已在 hooks 内隔离）
        hooks.collect_hook(
            "after_tool",
            {"name": name, "args": args, "output": text, "elapsed": round(time.monotonic() - started, 3)},
        )
        result = {
            "role": "tool",
            "tool_call_id": call.get("id"),
            "tool_name": name,
            "content": text,
            "type": "tool",
        }
        if images:
            result["images"] = images
        return result

    def refine_prompt(self, prompt: str) -> str:
        external = hooks.call_hook("prompt_refine", {"prompt": prompt}, default=None)
        if isinstance(external, dict) and external.get("prompt"):
            log.debug("prompt_refine 外部接口生效")
            return external["prompt"]
        if isinstance(external, str) and external:
            log.debug("prompt_refine 外部接口生效")
            return external
        if not getattr(self.config, "api_key", ""):
            return prompt
        result = self.chat(
            [
                {"role": "system", "content": "完善用户的编码任务提示，只输出完善后的 prompt。"},
                {"role": "user", "content": prompt},
            ]
        )
        return result.get("content") or prompt

    def generate_plan(self, prompt: str, model: Optional[str] = None) -> dict:
        external = hooks.call_hook("plan_generate", {"prompt": prompt}, default=None)
        if isinstance(external, dict) and external.get("content"):
            log.info("计划生成（外部接口）: %s", external.get("title", "任务计划"))
            return {
                "title": external.get("title", "任务计划"),
                "content": external["content"],
                "complexity": "high",
            }
        plan_model = model
        if plan_model is None and hasattr(self.config, "get_task_model"):
            plan_model = self.config.get_task_model("plan")
        try:
            system = prompt_loader.get_plan_prompt(prompt, config=self.config)
        except Exception as e:
            log.warn("加载 plan.md 失败: %s", e)
            system = "输出 markdown 计划：目标、步骤、风险、验收。不要执行任务。"
        result = self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            model=plan_model,
        )
        log.info("计划生成: %d字（model=%s）", len(result.get("content") or ""), plan_model or self.config.model)
        return {"title": "任务计划", "content": result.get("content") or "暂无计划", "complexity": "high"}

    def confirm_plan(self, plan: dict, action: str, feedback: str = "") -> dict:
        external = hooks.call_hook("plan_confirm", {"plan": plan, "action": action, "feedback": feedback}, default=None)
        if isinstance(external, dict) and external.get("plan"):
            return external["plan"]
        if action == "confirm":
            plan["status"] = "confirmed"
            return plan
        if action == "manual_edit":
            if feedback:
                plan["content"] = feedback
            plan["status"] = "draft"
            return plan
        if action == "llm_modify":
            result = self.chat(
                [
                    {"role": "system", "content": "根据反馈修改计划，输出完整 markdown。"},
                    {"role": "user", "content": f"原计划:\n{plan.get('content')}\n\n反馈:\n{feedback}"},
                ]
            )
            if result.get("content"):
                plan["content"] = result["content"]
            plan["status"] = "draft"
            return plan
        return plan
