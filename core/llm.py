# Agent 核心：LLM 调用、工具策略、token/余额
import json
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Callable, List, Optional

try:
    import openai

    HAS_OPENAI = True
except ImportError:
    openai = None
    HAS_OPENAI = False

from core import hooks
from core import policy
from core import register
from core import tokens as tokenmod
from core.config import normalize_base_url
from core.log import get_logger

log = get_logger("llm")

SYSTEM_PROMPT = (
    "你是 BAWCode 编码 Agent。根据用户任务与记忆/计划/步骤工作。"
    "复杂任务先 write_plan / generate_steps；执行中 update_step_status。"
    "工具调用可能被安全策略拦截，收到用户拒绝 error 时说明原因并调整。"
    "回复使用简洁中文。"
)


class LLM:
    def __init__(self, config):
        self.config = config
        self.meter = tokenmod.TokenMeter(config.model_name)
        self.meter.context_window = getattr(config, "context_window", 0) or 0
        self.meter.model = config.model_name
        self.client = None
        self._build_client()

    def _build_client(self) -> None:
        self.client = None
        if HAS_OPENAI and getattr(self.config, "api_key", ""):
            base = normalize_base_url(getattr(self.config, "base_url", "") or "")
            try:
                self.client = openai.OpenAI(api_key=self.config.api_key, base_url=base)
                log.debug("OpenAI 客户端已构建: %s", base)
            except Exception as e:
                self.client = None
                log.warn("OpenAI 客户端构建失败: %s", e)
        else:
            log.debug("未构建 OpenAI 客户端: openai=%s api_key=%s", HAS_OPENAI, bool(getattr(self.config, "api_key", "")))

    def refresh_from_config(self, config) -> None:
        self.config = config
        self.meter.set_model(config.model_name, getattr(config, "context_window", 0) or 0)
        self._build_client()
        self.query_balance()

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }

    def _chat_url(self) -> str:
        base = normalize_base_url(self.config.base_url or "")
        return f"{base}/chat/completions"

    def _http_chat(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._chat_url(), data=body, headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _native_chat(self, payload: dict) -> dict:
        if self.client is not None:
            resp = self.client.chat.completions.create(**payload)
            return resp.model_dump() if hasattr(resp, "model_dump") else resp
        return self._http_chat(payload)

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
            # 嵌套 data.balance
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

    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> dict:
        """调用 LLM（核心扩展点：llm_request）；model 可指定任务专用模型 id"""
        self.meter.measure_context(messages)
        # 任务模型若是完整 model_name，取 model_id 部分作为 API model
        model_id = model or self.config.model
        if model and "-" in str(model):
            # provider_id-model_id
            parts = str(model).split("-", 1)
            if len(parts) == 2 and parts[1]:
                # 若与当前 provider 匹配或本地能解析，用 model_id
                resolved = self.config.find_model(model) if hasattr(self.config, "find_model") else None
                if resolved:
                    model_id = resolved.get("model_id") or model
                else:
                    model_id = parts[1]
        payload = {
            "model": model_id,
            "messages": [{"role": m.get("role", "user"), "content": str(m.get("content", ""))} for m in messages],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if not self.config.api_key:
            log.warn("未配置 api_key，LLM 请求被拒绝")
            hooked = hooks.call_hook("llm_request", {"messages": messages, "tools": tools, "payload": payload}, default=None)
            if hooked is None:
                return {"content": "", "tool_calls": [], "error": "未配置 api_key"}
            return self._normalize(hooked)

        retry_times = int(self.config.data.get("llm", {}).get("retry_times", 3))
        retry_delay = float(self.config.data.get("llm", {}).get("retry_delay", 1.0))
        attempt = 0
        last_error = None
        start = time.monotonic()
        log.debug("LLM 请求 model=%s messages=%d tools=%d", model_id, len(messages), len(tools or []))
        while attempt <= max(retry_times, 0):
            try:
                external = hooks.call_hook(
                    "llm_request",
                    {"messages": messages, "tools": tools, "payload": payload},
                    default=None,
                )
                data = external if external is not None else self._native_chat(payload)
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
                # openai.NotFoundError/APIError 等不在旧捕获元组内，会穿透
                # 导致整个 TUI 崩溃；统一按可重试错误处理并最终返回 error
                last_error = e
                attempt += 1
                log.warn("LLM 调用失败(第%d/%d次): %s", attempt, max(retry_times, 0) + 1, e)
                log.debug("异常详情:\n%s", traceback.format_exc())
                if attempt > max(retry_times, 0):
                    break
                if retry_delay > 0:
                    time.sleep(retry_delay * attempt)
                self._build_client()
        log.error("LLM 调用最终失败: %s", last_error)
        return {"content": "", "tool_calls": [], "error": f"LLM 调用失败: {last_error}"}

    def _normalize(self, data: Any) -> dict:
        if isinstance(data, dict) and data.get("content") is not None and "tool_calls" in data:
            return {"content": data.get("content") or "", "tool_calls": data.get("tool_calls") or [], "raw": data.get("raw", data), "error": data.get("error")}
        if isinstance(data, dict) and data.get("error") and "choices" not in data:
            return {"content": "", "tool_calls": [], "raw": data, "error": data.get("error")}
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            return {"content": str(data.get("result", data)) if isinstance(data, dict) else str(data), "tool_calls": [], "raw": data if isinstance(data, dict) else {}}
        message = choices[0].get("message", {})
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": call.get("id"), "name": function.get("name"), "arguments": args})
        return {"content": message.get("content") or "", "tool_calls": tool_calls, "raw": data}

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
        if not self.config.api_key:
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
            return {"title": external.get("title", "任务计划"), "content": external["content"], "complexity": "high"}
        plan_model = None
        if hasattr(self.config, "get_task_model"):
            plan_model = self.config.get_task_model("plan")
        result = self.chat(
            [
                {"role": "system", "content": "输出 markdown 计划：目标、步骤、风险、验收。不要执行任务。"},
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
