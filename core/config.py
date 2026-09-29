#此文件实现配置文件读取和保存功能（多 provider）
import copy
import json
from pathlib import Path
from typing import List, Optional

from core.log import get_logger, init as init_logging

_ROOT = Path(__file__).resolve().parent.parent

log = get_logger("config")
DEFAULT_CONFIG_PATH = _ROOT / "data" / "config.json"
THEME_DIR = _ROOT / "data" / "theme"


def default_config() -> dict:
    """内置默认配置：多 provider，model_name = provider_id-model_id"""
    return {
        "providers": [
            {
                "provider_id": "deepseek",
                "name": "DeepSeek",
                "api_key": "",
                "base_url": "https://api.deepseek.com",
                "default_model_id": "deepseek-flash",
                "temperature": 1.0,
                "balance_url": "https://api.deepseek.com/user/balance",
                "models": [
                    {
                        "model_id": "deepseek-flash",
                        "context_window": 1000000,
                        "max_tokens": 384000,
                        "temperature": 1.0,
                        "modalities": ["text"],
                        "thinking_effort": "high",
                    },
                    {
                        "model_id": "deepseek-v4-pro",
                        "context_window": 1000000,
                        "max_tokens": 384000,
                        "temperature": 1.0,
                        "modalities": ["text"],
                        "thinking_effort": "high",
                    },
                ],
            }
        ],
        "active_provider_id": "deepseek",
        "active_model_id": "deepseek-flash",
        # 特定功能默认模型：model_name = provider_id-model_id
        "task_models": {
            "plan": "deepseek-deepseek-flash",
            "code": "deepseek-deepseek-flash",
            "review": "deepseek-deepseek-flash",
        },
        "prompt": {
            "dir": "core/prompts",
            "system_files": ["system_prompt.md"],
            "plan_files": ["plan.md"],
        },
        "system": {"font_size": 16},
        "ui": {
            "theme": "dark",
            "mode": "auto",
            "busy_send_mode": "queue",
            "logo": True,
            "send": "ctrl+enter",
            "newline": "shift+enter",
            "switch_focus": "tab",
            "switch_mode": "shift+tab",
            "expand": ["right", "l"],
            "collapse": ["left", "h"],
            "toggle_fold": ["enter", "space"],
            "scroll_up": ["pageup"],
            "scroll_down": ["pagedown"],
            "complete": "tab",
            "collapse_done_steps": True,
            "scroll_v0": 1.0,
            "scroll_hold_ms": 150,
            "scroll_max_step": 20,
            "tip_interval": 5,
        },
        # stream: 流式输出开关（SSE 增量上屏）；首个 chunk 前失败自动回落非流式，
        # 已收 chunk 后失败不回落不重试（内容已上屏）；false 完全走整段路径
        "llm": {"retry_times": 3, "retry_delay": 1.0, "stream": True},
        # 分级日志（core/log.py）：level 全局阈值 debug/info/warn/error/off；
        # modules 按模块覆盖粒度（如 {"llm": "debug", "ui": "off"}）；
        # console 同步输出 stderr；days_to_keep 保留天数（0 永久）
        "log": {
            "level": "info",
            "modules": {},
            "console": False,
            "days_to_keep": 14,
        },
        "memory": {
            "longterm_path": "data/memory.json",
            "longterm_dir": "data/memory",
            "auto_compress": True,
            "compress_threshold": 0.8,
            # 压缩保留尾段 token 预算（最近轮次原文不压缩，按轮次边界切尾，至少保留最后一轮）
            "compress_keep_recent_tokens": 16384,
            # 摘要硬上限 token（超限视为摘要失控，放弃压缩保持历史不变）
            "compress_summary_max_tokens": 1024,
        },
        # 上下文管理（core/toolstore.py + memory.finalize_turn）：
        # tool_whitelist 白名单内工具完整调用历史跨回合保留，会话累计超
        # whitelist_budget_tokens 后最老的先外置磁盘；白名单外工具回合末剥离，
        # 仅保留 description+调用id（最近 strip_keep_recent 条不剥离，0=严格全剥）；
        # 行内限额：普通工具 inline_limit_tokens，large_tools 类
        # inline_limit_tokens_large，超限部分外置并在结果中附落盘指针
        "context": {
            "tool_whitelist": [
                "write_plan",
                "update_plan",
                "generate_steps",
                "update_step_status",
                "memory_add_fact",
                "memory_add_project_note",
                "rag_add",
            ],
            "whitelist_budget_tokens": 32768,
            "inline_limit_tokens": 4096,
            "large_tools": ["read", "search", "write", "edit_file"],
            "inline_limit_tokens_large": 32768,
            "persist_dir": "data/toolcalls",
            "strip_keep_recent": 6,
        },
        "command_plugins": ["data/commands"],
        # 核心功能外部 API（UI/改配置不挂接口）
        "external_apis": {
            "prompt_refine": None,
            "complexity_judge": None,
            "plan_generate": None,
            "plan_confirm": None,
            "step_generate": None,
            "step_update": None,
            "memory_write": None,
            "memory_read": None,
            "rag_add": None,
            "rag_query": None,
            "computer_use": None,
            "llm_request": None,
            "tool_confirm": None,
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def make_model_name(provider_id: str, model_id: str) -> str:
    """对外 model_name：provider_id-model_id"""
    return f"{provider_id}-{model_id}"


def normalize_base_url(url: str) -> str:
    """规范化 OpenAI 兼容 base_url。

    - 自动补全 http(s):// 协议前缀（默认 https）
    - 去掉尾部斜杠
    - 去掉重复的 /v1 后缀（如 /v1/v1）
    - 缺少 /v1 时自动拼接，最终以 /v1 结尾
    识别格式示例：api.deepseek.com/v1、https://api.deepseek.com
    """
    text = (url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif not text.startswith(("http://", "https://")):
        text = "https://" + text
    while text.endswith("/"):
        text = text[:-1]
    # 去掉路径末尾重复的 /v1（大小写不敏感）
    while len(text) > 3 and text.lower().endswith("/v1"):
        text = text[:-3]
        while text.endswith("/"):
            text = text[:-1]
    return text + "/v1"


# 模型/系统枚举（设置页 ←→ 切换）
MODALITY_OPTIONS = ["text", "vision", "audio", "video", "embedding"]
THINKING_OPTIONS = ["none", "low", "medium", "high"]
TASK_ROLES = {
    "plan": "任务规划",
    "code": "代码编写",
    "review": "代码审查",
}


def normalize_model_entry(entry: dict) -> dict:
    """补全模型条目默认字段"""
    data = dict(entry or {})
    data.setdefault("model_id", data.get("model") or "")
    data.setdefault("context_window", 65536)
    data.setdefault("max_tokens", 8192)
    data.setdefault("temperature", 1.0)
    modalities = data.get("modalities")
    if not isinstance(modalities, list) or not modalities:
        modalities = ["text"]
    data["modalities"] = [str(m) for m in modalities if str(m) in MODALITY_OPTIONS] or ["text"]
    effort = str(data.get("thinking_effort") or "none").lower()
    data["thinking_effort"] = effort if effort in THINKING_OPTIONS else "none"
    return data


def parse_model_name(name: str) -> tuple:
    """解析 model_name → (provider_id, model_id)；无法解析时 (None, name)"""
    text = (name or "").strip()
    if not text:
        return None, ""
    if "-" in text:
        provider_id, model_id = text.split("-", 1)
        return provider_id, model_id
    return None, text


class Config:
    """多 Provider 配置"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.data = default_config()
        self._load()
        init_logging(self.data.get("log") or {})
        self.theme_dir = THEME_DIR
        self.apply_active()

    def _load(self) -> None:
        if not self.config_path.exists():
            log.info("配置文件不存在，使用内置默认配置: %s", self.config_path)
            return
        raw = self.config_path.read_text(encoding="utf-8").strip()
        if not raw:
            log.info("配置文件为空，使用内置默认配置: %s", self.config_path)
            return
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("配置文件 JSON 解析失败 %s: %s", self.config_path, e)
            raise
        if isinstance(loaded, dict):
            log.debug("已加载配置: %s", self.config_path)
            self.data = _deep_merge(self.data, loaded)
            self._migrate_legacy()
            self.apply_active()

    def _migrate_legacy(self) -> None:
        """兼容旧扁平 provider 字段，并规范化 models / task_models"""
        if "providers" in self.data and self.data["providers"]:
            first = self.data["providers"][0]
            if "provider_id" not in first and "name" in first:
                first["provider_id"] = str(first.get("name") or "default").lower()
            if "models" not in first and first.get("model"):
                first["models"] = [
                    {
                        "model_id": first.get("model"),
                        "context_window": first.get("context_window", 65536),
                        "max_tokens": first.get("max_tokens", 8192),
                    }
                ]
                first["default_model_id"] = first.get("model")
        for provider in self.data.get("providers") or []:
            provider.setdefault("provider_id", provider.get("name", "default"))
            if provider.get("base_url"):
                provider["base_url"] = normalize_base_url(str(provider["base_url"]))
            provider["models"] = [
                normalize_model_entry(m) for m in (provider.get("models") or []) if m.get("model_id") or m.get("model")
            ]
        task_models = self.data.setdefault("task_models", {})
        for role in TASK_ROLES:
            task_models.setdefault(role, self.active_model_name() if hasattr(self, "model_name") else "")

    def providers(self) -> List[dict]:
        return list(self.data.get("providers") or [])

    def get_provider(self, provider_id: Optional[str] = None) -> Optional[dict]:
        target = provider_id or self.data.get("active_provider_id")
        for item in self.providers():
            if item.get("provider_id") == target:
                return item
        items = self.providers()
        return items[0] if items else None

    def active_model_name(self) -> str:
        pid = self.data.get("active_provider_id") or ""
        mid = self.data.get("active_model_id") or ""
        return make_model_name(pid, mid)

    def list_models(self) -> List[dict]:
        """展开全部模型项：含 model_name 与模型参数"""
        rows = []
        for provider in self.providers():
            pid = provider.get("provider_id") or ""
            for model in provider.get("models") or []:
                mid = model.get("model_id") or ""
                rows.append(
                    {
                        "provider_id": pid,
                        "model_id": mid,
                        "model_name": make_model_name(pid, mid),
                        "context_window": model.get("context_window", 0),
                        "max_tokens": model.get("max_tokens", 0),
                        "temperature": model.get("temperature", provider.get("temperature", 1.0)),
                        "modalities": list(model.get("modalities") or ["text"]),
                        "thinking_effort": model.get("thinking_effort", "none"),
                        "base_url": normalize_base_url(provider.get("base_url", "")),
                        "api_key": provider.get("api_key", ""),
                        "balance_url": provider.get("balance_url", ""),
                        "is_active": (
                            pid == self.data.get("active_provider_id")
                            and mid == self.data.get("active_model_id")
                        ),
                    }
                )
        return rows

    def resolve_model_row(self, model_name: str) -> Optional[dict]:
        """解析 model_name；不存在或所属 provider 无 api_key 时返回 None"""
        row = self.find_model(model_name) if model_name else None
        if row and not row.get("api_key"):
            log.warn("任务模型无 api_key，视为不可用: %s", model_name)
            return None
        return row

    def task_models(self) -> dict:
        tm = self.data.get("task_models") or {}
        active = self.active_model_name()
        defaults = {
            "plan": active,
            "code": active,
            "review": active,
        }
        for role in TASK_ROLES:
            name = tm.get(role) or defaults[role]
            row = self.resolve_model_row(name)
            if row is None:
                if name and name != active:
                    log.warn("task_models.%s=%s 不可用，回退激活模型 %s", role, name, active)
                defaults[role] = active
                self.data.setdefault("task_models", {})[role] = active
            else:
                defaults[role] = row.get("model_name") or name
        return defaults

    def get_task_model(self, role: str) -> str:
        return self.task_models().get(role) or self.active_model_name()

    def set_task_model(self, role: str, model_name: str) -> bool:
        if role not in TASK_ROLES:
            return False
        self.data.setdefault("task_models", {})[role] = model_name
        return True

    def find_provider(self, provider_id: str) -> Optional[dict]:
        for item in self.providers():
            if item.get("provider_id") == provider_id:
                return item
        return None

    def add_or_update_provider(self, provider: dict) -> dict:
        """新增/更新提供商；models 规范化"""
        provider = dict(provider or {})
        pid = provider.get("provider_id") or ""
        if not pid:
            raise ValueError("provider_id 不能为空")
        provider["provider_id"] = pid
        provider.setdefault("name", pid)
        provider.setdefault("api_key", "")
        provider["base_url"] = normalize_base_url(provider.get("base_url", ""))
        provider.setdefault("balance_url", "")
        provider.setdefault("temperature", 1.0)
        provider["models"] = [normalize_model_entry(m) for m in provider.get("models") or []]
        provider.setdefault("default_model_id", (provider["models"][0]["model_id"] if provider["models"] else ""))
        providers = self.data.setdefault("providers", [])
        for i, item in enumerate(providers):
            if item.get("provider_id") == pid:
                merged = _deep_merge(item, provider)
                merged["models"] = provider["models"] or merged.get("models") or []
                providers[i] = merged
                break
        else:
            providers.append(provider)
        log.debug("提供商已保存: %s（%d 个模型）", pid, len(provider.get("models") or []))
        self.apply_active()
        return provider

    def add_model(self, provider_id: str, model: dict) -> dict:
        provider = self.find_provider(provider_id)
        if provider is None:
            provider = {
                "provider_id": provider_id,
                "name": provider_id,
                "api_key": "",
                "base_url": "",
                "models": [],
            }
            self.data.setdefault("providers", []).append(provider)
        entry = normalize_model_entry(model)
        models = provider.setdefault("models", [])
        for i, m in enumerate(models):
            if m.get("model_id") == entry["model_id"]:
                models[i] = entry
                break
        else:
            models.append(entry)
        if not provider.get("default_model_id"):
            provider["default_model_id"] = entry["model_id"]
        self.apply_active()
        return entry

    def update_model(self, provider_id: str, model_id: str, updates: dict) -> Optional[dict]:
        provider = self.find_provider(provider_id)
        if not provider:
            return None
        for i, m in enumerate(provider.get("models") or []):
            if m.get("model_id") == model_id:
                merged = normalize_model_entry({**m, **(updates or {})})
                # 允许改 model_id
                provider["models"][i] = merged
                if self.data.get("active_provider_id") == provider_id and self.data.get("active_model_id") == model_id:
                    self.data["active_model_id"] = merged.get("model_id", model_id)
                self.apply_active()
                return merged
        return None

    def save_provider_from_form(
        self,
        form: dict,
        models_after: Optional[List[dict]] = None,
    ) -> dict:
        """提供商独立保存：按 provider_id 创建或更新

        form: provider_id/name/api_key/base_url/balance_url/temperature/default_model_id
        models_after: 保存后期望的模型列表（可在模型页增改）；
                      程序与保存前列表 diff 得出新增/更新/重复。
                      不传时：更新保持原有模型；创建则模型列表为空。

        返回 action/existed/changes/model_dupes/model_added/model_updated/message
        """
        form = form or {}
        pid = str(form.get("provider_id") or "").strip()
        result = {
            "action": "",
            "existed": False,
            "changes": [],
            "model_dupes": [],
            "model_added": [],
            "model_updated": [],
            "message": "",
        }
        if not pid:
            result["message"] = "提供商ID不能为空，未保存"
            return result

        existing = self.find_provider(pid)
        api_key = str(form.get("api_key", ""))
        base_url = normalize_base_url(str(form.get("base_url", "")))
        name = str(form.get("name") or pid)
        balance_url = str(form.get("balance_url", ""))
        try:
            temperature = float(form.get("temperature", 1.0))
        except (TypeError, ValueError):
            temperature = 1.0
        default_model_id = str(form.get("default_model_id") or "").strip()

        before_list = [normalize_model_entry(m) for m in ((existing or {}).get("models") or [])]
        before = {m["model_id"]: m for m in before_list}

        # 保存后的模型列表
        if models_after is None:
            after_list = list(before_list) if existing is not None else []
        else:
            after_list = []
            seen = set()
            dupes = []
            for item in models_after:
                entry = normalize_model_entry(item if isinstance(item, dict) else {"model_id": item})
                mid = entry.get("model_id") or ""
                if not mid:
                    continue
                if mid in seen:
                    dupes.append(mid)
                    continue
                seen.add(mid)
                after_list.append(entry)
            result["model_dupes"] = dupes
            if dupes:
                result["changes"].append("模型重复:" + ",".join(dupes))

        after = {m["model_id"]: m for m in after_list}
        added = [mid for mid in after if mid not in before]
        updated = [
            mid
            for mid in after
            if mid in before and after[mid] != before[mid]
        ]
        removed = [mid for mid in before if mid not in after]
        result["model_added"] = added
        result["model_updated"] = updated
        if added:
            result["changes"].append("新增模型:" + ",".join(added))
        if updated:
            result["changes"].append("更新模型:" + ",".join(updated))
        if removed:
            result["changes"].append("移除模型:" + ",".join(removed))

        if default_model_id and after and default_model_id not in after:
            default_model_id = after_list[0]["model_id"]
            result["changes"].append("默认模型自动选择:" + default_model_id)
        if not default_model_id and after_list:
            default_model_id = after_list[0]["model_id"]

        provider = {
            "provider_id": pid,
            "name": name,
            "api_key": api_key,
            "base_url": base_url,
            "balance_url": balance_url,
            "temperature": temperature,
            "models": after_list,
            "default_model_id": default_model_id
            or (existing or {}).get("default_model_id")
            or (after_list[0]["model_id"] if after_list else ""),
        }

        if existing is None:
            self.add_or_update_provider(provider)
            result["action"] = "created"
            result["existed"] = False
            log.info("创建提供商 %s · %s", pid, result["message"])
            if not result["changes"]:
                result["changes"] = ["created"]
            else:
                result["changes"] = ["created"] + result["changes"]
            mids = ",".join(m["model_id"] for m in after_list) or "（无）"
            result["message"] = f"已创建提供商「{pid}」· 模型: {mids}"
            return result

        result["existed"] = True
        result["action"] = "updated"
        if api_key != str(existing.get("api_key") or ""):
            result["changes"].append("apikey已更新")
        if base_url != normalize_base_url(str(existing.get("base_url") or "")):
            result["changes"].append("baseurl已更改")
        if name != str(existing.get("name") or ""):
            result["changes"].append("名称已更新")
        if balance_url != str(existing.get("balance_url") or ""):
            result["changes"].append("余额URL已更新")
        if temperature != float(existing.get("temperature", 1.0)):
            result["changes"].append("温度已更新")

        self.add_or_update_provider(provider)
        if not result["changes"]:
            result["message"] = f"提供商「{pid}」无变更，已刷新保存"
            log.info("提供商 %s 无变更，已刷新保存", pid)
        else:
            result["message"] = f"已更新提供商「{pid}」· " + " · ".join(result["changes"])
            log.info("更新提供商 %s · %s", pid, " · ".join(result["changes"]))
        return result

    def provider_model_ids(self, provider_id: str) -> List[str]:
        provider = self.find_provider(provider_id)
        if not provider:
            return []
        return [m.get("model_id") for m in provider.get("models") or [] if m.get("model_id")]

    def list_model_names(self) -> List[str]:
        return [row["model_name"] for row in self.list_models()]

    def find_model(self, name: str) -> Optional[dict]:
        """按 model_name 或 model_id 查找；model_id 唯一时可省略 provider 前缀"""
        text = (name or "").strip()
        if not text:
            return None
        rows = self.list_models()
        for row in rows:
            if row["model_name"] == text:
                return row
        pid, mid = parse_model_name(text)
        hits = [r for r in rows if r["model_id"] == (mid or text) or r["model_name"] == text]
        if len(hits) == 1:
            return hits[0]
        for row in rows:
            if row["model_id"] == text:
                return row
        for row in rows:
            if row["provider_id"] == pid and row["model_id"] == mid:
                return row
        return None

    def switch_model(self, name: str) -> Optional[dict]:
        row = self.find_model(name)
        if not row:
            log.warn("切换模型失败，未找到: %s", name)
            return None
        self.data["active_provider_id"] = row["provider_id"]
        self.data["active_model_id"] = row["model_id"]
        self.apply_active()
        log.info("已切换模型: %s", row["model_name"])
        return row

    def match_models(self, prefix: str, limit: int = 12) -> List[dict]:
        """补全：先 model_name，再 model_id/provider_id；model_name 结果在上"""
        prefix = (prefix or "").strip().lower()
        name_hits, other_hits = [], []
        for row in self.list_models():
            model_name = row["model_name"].lower()
            model_id = row["model_id"].lower()
            provider_id = row["provider_id"].lower()
            if prefix and model_name.startswith(prefix):
                name_hits.append(row)
            elif prefix and (model_id.startswith(prefix) or provider_id.startswith(prefix) or prefix in model_name):
                other_hits.append(row)
            elif not prefix:
                other_hits.append(row)
        name_hits.sort(key=lambda r: (len(r["model_name"]), r["model_name"]))
        other_hits.sort(key=lambda r: (len(r["model_name"]), r["model_name"]))
        return (name_hits + other_hits)[:limit]

    def apply_active(self) -> None:
        provider = self.get_provider() or {}
        mid = self.data.get("active_model_id") or provider.get("default_model_id")
        self.data["active_model_id"] = mid
        model_row = None
        for model in provider.get("models") or []:
            if model.get("model_id") == mid:
                model_row = model
                break
        if model_row is None:
            models = provider.get("models") or []
            model_row = models[0] if models else {}
            if model_row:
                self.data["active_model_id"] = model_row.get("model_id")
        self.api_key = provider.get("api_key", "")
        self.base_url = normalize_base_url(provider.get("base_url", ""))
        self.provider_id = provider.get("provider_id", "")
        self.provider_name = provider.get("name", self.provider_id)
        self.model = self.data.get("active_model_id") or ""
        self.model_name = self.active_model_name()
        self.temperature = provider.get("temperature", 1.0)
        self.context_window = model_row.get("context_window", 0) if model_row else 0
        self.max_tokens = model_row.get("max_tokens", 2048) if model_row else 2048
        self.balance_url = provider.get("balance_url", "")
        system = self.data.get("system", {})
        self.font_size = system.get("font_size", 16)
        ui = self.data.get("ui", {})
        self.theme = ui.get("theme", "dark")
        self.mode = ui.get("mode", "auto")

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.data["active_provider_id"] = self.data.get("active_provider_id")
        self.data["active_model_id"] = self.data.get("active_model_id")
        self.config_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.debug("配置已保存: %s", self.config_path)

    def list_themes(self) -> List[str]:
        """扫描 data/theme/*.json；无效文件在 load_theme 过滤"""
        if not self.theme_dir.exists():
            return ["dark"]
        names = ["dark"]
        for path in sorted(self.theme_dir.glob("*.json")):
            stem = path.stem
            if stem != "dark" and stem not in names:
                names.append(stem)
        return names
