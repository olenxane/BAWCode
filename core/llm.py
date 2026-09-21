# Agent 核心：LLM 调用、工具策略、token/余额
import json
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, List, Optional

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
from core.config import normalize_base_url
from core.log import get_logger

log = get_logger("llm")

_ROOT = Path(__file__).resolve().parent.parent


def _fallback_system_prompt() -> str:
    return (
        "你是 BAWCode 编码 Agent。根据用户任务与记忆/计划/步骤工作。"
        "复杂任务先 write_plan / generate_steps；执行中 update_step_status。"
        "工具调用可能被安全策略拦截，收到用户拒绝 error 时说明原因并调整。"
        "回复使用简洁中文。"
    )


def get_system_prompt(config=None, **kwargs) -> str:
    """从 core/prompts 加载系统提示词（占位符替换）"""
    try:
        return prompt_loader.get_system_prompt(config=config, **kwargs)
    except Exception as e:
        log.warn("加载系统提示词失败，使用兜底: %s", e)
        return _fallback_system_prompt()


# 兼容旧 import：惰性取当前配置提示词
class _SystemPromptProxy(str):
    pass


SYSTEM_PROMPT = _SystemPromptProxy(_fallback_system_prompt())


class LLM:
    def __init__(self, config):
        self.config = config
        self.meter = tokenmod.TokenMeter(config.model_name)
        self.meter.context_window = getattr(config, "context_window", 0) or 0
        self.meter.model = config.model_name
        self.client = None
        self._client_provider_id = None
        self._build_client()

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
                self.client = openai.OpenAI(api_key=key, base_url=base)
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
        row = None
        if hasattr(cfg, "resolve_model_row"):
            row = cfg.resolve_model_row(name)
        if row is None and hasattr(cfg, "find_model"):
            row = cfg.find_model(name)
        if row is None or not row.get("api_key"):
            active_name = cfg.active_model_name() if hasattr(cfg, "active_model_name") else name
            active_row = None
            if hasattr(cfg, "find_model"):
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
            "temperature": row.get("temperature") or getattr(cfg, "temperature", 1.0),
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

    def _native_chat(self, payload: dict, req: Optional[dict] = None) -> dict:
        req = req or {}
        provider_id = req.get("provider_id")
        api_key = req.get("api_key")
        base_url = req.get("base_url")
        if HAS_OPENAI and api_key:
            if self.client is None or self._client_provider_id != provider_id:
                self._build_client(provider_id=provider_id, api_key=api_key, base_url=base_url)
            if self.client is not None:
                resp = self.client.chat.completions.create(**payload)
                return resp.model_dump() if hasattr(resp, "model_dump") else resp
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
    def _map_api_messages(messages: List[dict]) -> List[dict]:
        """组装 DeepSeek/OpenAI Tool Calls 协议消息，透传 tool_call_id / tool_calls"""
        out: List[dict] = []
        for m in messages or []:
            role = m.get("role") or "user"
            content = m.get("content")
            if content is None:
                content = ""
            item: dict = {"role": role, "content": content if isinstance(content, str) else str(content)}
            if m.get("name"):
                item["name"] = m["name"]
            if role == "tool":
                # DeepSeek：tool 消息必须携带 tool_call_id
                tcid = m.get("tool_call_id")
                if tcid:
                    item["tool_call_id"] = tcid
                else:
                    # 兼容无 id 的旧消息：降级为 system 注记，避免协议错误
                    item = {
                        "role": "system",
                        "content": f"[tool:{m.get('tool_name') or 'tool'}]\n{item['content']}",
                    }
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
                            "id": c.get("id") or "",
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
    ) -> dict:
        """调用 LLM（核心扩展点：llm_request）；model 可指定任务专用模型"""
        self.meter.measure_context(messages)
        req = self.resolve_request(model)
        model_id = req.get("model_id") or req.get("model_name") or ""
        payload = {
            "model": model_id,
            "messages": self._map_api_messages(messages),
            "temperature": req.get("temperature", 1.0) if temperature is None else temperature,
            "max_tokens": int(req.get("max_tokens") or 2048),
        }
        if tools:
            payload["tools"] = tools
            # DeepSeek 思考模式不支持 required/指定 function，固定 auto
            payload["tool_choice"] = "auto"
        if not req.get("api_key"):
            log.warn("未配置可用 api_key，LLM 请求被拒绝 model=%s", model_id)
            hooked = hooks.call_hook(
                "llm_request", {"messages": messages, "tools": tools, "payload": payload}, default=None
            )
            if hooked is None:
                return {"content": "", "tool_calls": [], "error": "未配置 api_key"}
            return self._normalize(hooked)

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
            try:
                external = hooks.call_hook(
                    "llm_request",
                    {"messages": messages, "tools": tools, "payload": payload},
                    default=None,
                )
                data = external if external is not None else self._native_chat(payload, req)
                result = self._normalize(data)
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
                last_error = e
                attempt += 1
                log.warn("LLM 调用失败(第%d/%d次): %s", attempt, max(retry_times, 0) + 1, e)
                log.debug("异常详情:\n%s", traceback.format_exc())
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
        return {"content": "", "tool_calls": [], "error": f"LLM 调用失败: {last_error}"}

    def _normalize(self, data: Any) -> dict:
        if isinstance(data, dict) and data.get("content") is not None and "tool_calls" in data:
            return {
                "content": data.get("content") or "",
                "tool_calls": data.get("tool_calls") or [],
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
                    "id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": args,
                    "type": call.get("type") or "function",
                }
            )
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        if reasoning and not content:
            content = reasoning
        return {"content": content, "tool_calls": tool_calls, "raw": data}

    def evaluate_tool(self, tool_name: str, args: dict) -> tuple:
        action, reason = policy.evaluate(tool_name, args, getattr(self.config, "mode", policy.MODE_AUTO))
        log.debug("工具策略 %s -> %s(%s)", tool_name, action, reason)
        return action, reason

    def run_tool_calls(self, tool_calls: List[dict], mode: Optional[str] = None) -> List[dict]:
        """执行工具：按策略直接放行；需确认的由外层处理，这里仅执行已放行项"""
        mode = mode or getattr(self.config, "mode", policy.MODE_AUTO)
        results = []
        for call in tool_calls:
            name = call.get("name")
            args = call.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            action, reason = policy.evaluate(name, args, mode)
            if action != policy.ALLOW:
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "tool_name": name,
                        "content": policy.default_reject_message(reason),
                        "type": "tool",
                        "pending": action == policy.CONFIRM,
                        "call": call,
                    }
                )
                continue
            if not register.has_tool(name or ""):
                output = f"工具不存在: {name}"
                log.warn("调用未注册工具: %s", name)
            else:
                started = time.monotonic()
                try:
                    output = register.call(name, **args)
                    log.debug("工具 %s 完成，用时 %.2fs", name, time.monotonic() - started)
                except TypeError as e:
                    output = f"工具参数错误: {e}"
                    log.error("工具 %s 参数错误: %s", name, e)
                except Exception as e:
                    output = f"工具执行错误: {e}"
                    log.error("工具 %s 执行错误: %s", name, e)
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "tool_name": name,
                    "content": text,
                    "type": "tool",
                    "pending": False,
                    "call": call,
                }
            )
        return results

    def execute_approved_tool(self, call: dict) -> dict:
        """确认通过后执行单个工具"""
        name = call.get("name")
        args = call.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        log.info("执行工具 %s（已确认）", name)
        if not register.has_tool(name or ""):
            output = f"工具不存在: {name}"
            log.warn("调用未注册工具: %s", name)
        else:
            started = time.monotonic()
            try:
                output = register.call(name, **args)
                log.debug("工具 %s 完成，用时 %.2fs", name, time.monotonic() - started)
            except TypeError as e:
                output = f"工具参数错误: {e}"
                log.error("工具 %s 参数错误: %s", name, e)
            except Exception as e:
                output = f"工具执行错误: {e}"
                log.error("工具 %s 执行错误: %s", name, e)
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return {
            "role": "tool",
            "tool_call_id": call.get("id"),
            "tool_name": name,
            "content": text,
            "type": "tool",
        }

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

    def judge_complexity(self, prompt: str) -> str:
        external = hooks.call_hook("complexity_judge", {"prompt": prompt}, default=None)
        if isinstance(external, dict) and external.get("complexity"):
            return str(external["complexity"]).lower()
        keywords = ["重构", "架构", "系统", "完整", "多文件", "设计", "实现", "agent", "框架", "迁移"]
        text = prompt.lower()
        result = "high" if any(k in text for k in keywords) or len(prompt) > 400 else "low"
        log.info("复杂度判定: %s（%d字）", result, len(prompt))
        return result

    def generate_plan(self, prompt: str) -> dict:
        external = hooks.call_hook("plan_generate", {"prompt": prompt}, default=None)
        if isinstance(external, dict) and external.get("content"):
            log.info("计划生成（外部接口）: %s", external.get("title", "任务计划"))
            return {
                "title": external.get("title", "任务计划"),
                "content": external["content"],
                "complexity": "high",
            }
        plan_model = None
        if hasattr(self.config, "get_task_model"):
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

    def generate_steps(self, prompt: str, plan_content: str) -> List[str]:
        external = hooks.call_hook("step_generate", {"prompt": prompt, "plan": plan_content}, default=None)
        if isinstance(external, dict) and external.get("steps"):
            return [str(s) for s in external["steps"]]
        plan_model = self.config.get_task_model("plan") if hasattr(self.config, "get_task_model") else None
        result = self.chat(
            [
                {"role": "system", "content": "将任务拆成 3-8 个可执行步骤，每步一行，只输出步骤列表。"},
                {"role": "user", "content": f"任务:\n{prompt}\n\n计划:\n{plan_content}"},
            ],
            model=plan_model,
        )
        lines = [line.strip() for line in (result.get("content") or "").splitlines() if line.strip()]
        cleaned = []
        for line in lines:
            cleaned.append(line.lstrip("0123456789.、- ").strip() or line)
        steps = cleaned or ["理解任务", "执行主要工作", "检查并总结"]
        log.info("步骤生成: %d步", len(steps))
        return steps

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
