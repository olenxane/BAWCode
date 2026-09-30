#该脚本负责Agent的记忆部分，包含：1.llm参与的上下文压缩2.工具调用记录管理（白名单保留/回合末剥离/结果外置磁盘）3.长期记忆写入配置4.在agent对话时提供记忆补充内容5.针对特定项目的RAG模块
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from core import hooks
from core import prompt_loader
from core import rag as rag_mod
from core import session_store
from core import toolstore
from core.log import get_logger
from wcwidth import wcwidth as _char_width

log = get_logger("memory")

_ROOT = Path(__file__).resolve().parent.parent

# 工具结果视为"已是简明记录"的长度阈值：拒绝原因/短错误不再剥离改写
_CONCISE_CONTENT_CHARS = 200

# 压缩摘要的 state_snapshot 结构节（与 core/prompts/compress.md 模板一一对应；程序按节解析校验）
_SNAPSHOT_SECTIONS = (
    "primary_request_and_intent",
    "key_technical_concepts",
    "files_and_code_sections",
    "errors_and_fixes",
    "problem_solving",
    "all_user_messages",
    "pending_tasks",
    "current_work",
    "next_step",
)

_COMPRESS_RETRY_HINT = (
    "\n\n注意：上一次输出无法被程序解析为 <state_snapshot> 结构。"
    "请重新输出：先 <analysis> 草稿，随后严格按模板输出 <state_snapshot> XML，"
    "XML 之外不要有任何文字（也不要代码围栏）。"
)


def parse_state_snapshot(text: str) -> Optional[dict]:
    """解析压缩模型输出的 <state_snapshot> 结构；返回 {"sections": dict, "xml": str} 或 None

    容错：剥除首尾代码围栏与 <analysis> 草稿块（参考 qwen-code 的草稿-定稿两段式）；
    至少 3 个节非空才认定有效——防止模型原样回显模板注释产出的空壳结构。
    """
    if not text or not text.strip():
        return None
    cleaned = re.sub(r"^\s*```[a-zA-Z]*\s*", "", text.strip())
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    cleaned = re.sub(r"<analysis>.*?</analysis>", "", cleaned, flags=re.DOTALL)
    match = re.search(r"<state_snapshot>.*?</state_snapshot>", cleaned, flags=re.DOTALL)
    if not match:
        return None
    xml = match.group(0).strip()
    sections = {}
    for tag in _SNAPSHOT_SECTIONS:
        sm = re.search(rf"<{tag}>(.*?)</{tag}>", xml, flags=re.DOTALL)
        sections[tag] = sm.group(1).strip() if sm else ""
    if sum(1 for v in sections.values() if v) < 3:
        return None
    return {"sections": sections, "xml": xml}

# 会话级记忆单例，由 main 初始化
_session: Optional["Memory"] = None


def init_session(config, project_identity_data: Optional[dict] = None) -> "Memory":
    """初始化全局记忆会话"""
    global _session
    _session = Memory(config, project_identity=project_identity_data)
    return _session


def get_session() -> Optional["Memory"]:
    """获取当前记忆会话"""
    return _session


class Memory:
    """Agent 记忆：消息、计划、步骤、长期记忆（md）、RAG 接口"""

    def __init__(self, config, project_identity: Optional[dict] = None):
        self.config = config
        memory_cfg = config.data.get("memory", {})
        self.longterm_path = _ROOT / memory_cfg.get("longterm_path", "data/memory.json")
        longterm_dir = memory_cfg.get("longterm_dir") or "data/memory"
        self.memory_dir = Path(longterm_dir) if Path(longterm_dir).is_absolute() else _ROOT / longterm_dir
        self.agent_md_path = self.memory_dir / "Agent.md"
        self.projects_dir = self.memory_dir / "Projects"
        self.project_identity = project_identity or {}
        self.project_id = self.project_identity.get("project_id") or ""
        self.project_md_path = (
            self.projects_dir / f"{self.project_id}.md" if self.project_id else self.projects_dir / "_default.md"
        )
        self.auto_compress = memory_cfg.get("auto_compress", True)
        self.compress_threshold = memory_cfg.get("compress_threshold", 0.8)
        # 压缩管线：尾段保留预算（最近轮次原文）与摘要硬上限；连续失败熔断计数
        self.compress_keep_recent_tokens = int(memory_cfg.get("compress_keep_recent_tokens", 16384))
        self.compress_summary_max_tokens = int(memory_cfg.get("compress_summary_max_tokens", 1024))
        self._compress_fail_streak = 0
        # 上下文管理（core/toolstore.py）：白名单/预算/行内限额/回合末剥离保护
        ctx_cfg = config.data.get("context", {})
        self.tool_whitelist = set(ctx_cfg.get("tool_whitelist") or [])
        self.whitelist_budget_tokens = int(ctx_cfg.get("whitelist_budget_tokens", 32768))
        self.inline_limit_tokens = int(ctx_cfg.get("inline_limit_tokens", 4096))
        self.large_tools = set(ctx_cfg.get("large_tools") or ["read", "write", "edit_file"])
        self.inline_limit_tokens_large = int(ctx_cfg.get("inline_limit_tokens_large", 32768))
        self.strip_keep_recent = int(ctx_cfg.get("strip_keep_recent", 6))
        self.sessions_dir = session_store.sessions_dir(config, self.project_id)
        self.session_id = session_store.new_session_id()
        self.session_created_at = datetime.now().isoformat(timespec="seconds")
        self.session_title = ""
        self._llm_fn: Optional[Callable[[str], str]] = None
        self.messages: List[dict] = []
        self.plan = {
            "title": "",
            "complexity": "low",
            "content": "",
            "status": "empty",
        }
        self.steps: List[dict] = []
        self.rag = rag_mod.get_store()
        self.longterm = self._load_longterm()
        self._ensure_md_files()
        self._migrate_legacy_json()
        log.info(
            "记忆会话已初始化: Agent.md=%s 项目记忆=%s 自动压缩=%s 阈值=%.0f%%",
            self.agent_md_path,
            self.project_md_path,
            self.auto_compress,
            self.compress_threshold * 100,
        )

    def _memory_dir(self) -> Path:
        return self.memory_dir

    def _load_longterm(self) -> dict:
        """读取长期记忆；优先 md，兼容旧 memory.json"""
        external = hooks.call_user_participating(
            "memory_read",
            {
                "path": str(self.longterm_path),
                "agent_md_path": str(self.agent_md_path),
                "project_md_path": str(self.project_md_path),
            },
            default=None,
        )
        if isinstance(external, dict) and external.get("memory"):
            log.debug("长期记忆来自外部接口")
            return external["memory"]
        data = {"facts": [], "project_notes": [], "updated_at": ""}
        if self.longterm_path.exists():
            raw = self.longterm_path.read_text(encoding="utf-8").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        data.update(loaded)
                except json.JSONDecodeError as e:
                    log.error("长期记忆 JSON 解析失败 %s: %s", self.longterm_path, e)
        return data

    def _ensure_md_files(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        if not self.agent_md_path.exists():
            self.agent_md_path.write_text(
                "# Agent 长期记忆（全局）\n\n## 用户偏好\n\n## 编码规范\n\n## 测试规范\n",
                encoding="utf-8",
            )
        if self.project_id and not self.project_md_path.exists():
            self.project_md_path.write_text(
                f"# 项目记忆：{self.project_identity.get('workspace_name') or self.project_id}\n\n"
                f"## 项目约定\n\n## 技术栈\n\n## 未决事项\n",
                encoding="utf-8",
            )

    def _migrate_legacy_json(self) -> None:
        """旧 memory.json 有 facts 时迁入 Agent.md，避免丢失"""
        facts = self.longterm.get("facts") or []
        notes = self.longterm.get("project_notes") or []
        if not facts and not notes:
            return
        try:
            text = self.agent_md_path.read_text(encoding="utf-8") if self.agent_md_path.exists() else ""
        except OSError:
            text = ""
        changed = False
        if facts and "## 用户偏好" in text:
            for f in facts:
                line = f"- {f}"
                if line not in text:
                    text = text.replace("## 用户偏好", f"## 用户偏好\n{line}", 1)
                    changed = True
        if notes and self.project_md_path.exists():
            try:
                ptext = self.project_md_path.read_text(encoding="utf-8")
            except OSError:
                ptext = ""
            for n in notes:
                line = f"- {n}"
                if line not in ptext:
                    ptext = ptext.replace("## 项目约定", f"## 项目约定\n{line}", 1)
                    changed = True
            if changed:
                self.project_md_path.write_text(ptext, encoding="utf-8")
        if changed:
            self.agent_md_path.write_text(text, encoding="utf-8")
            backup = self.longterm_path.with_suffix(".json.bak")
            try:
                self.longterm_path.replace(backup)
                log.info("旧 memory.json 已迁移并备份: %s", backup)
            except OSError:
                log.warn("memory.json 备份失败")

    def save_longterm(self, external_handler=None) -> None:
        """写入长期记忆（md 为事实来源；json 仅作兼容快照）"""
        self.longterm["updated_at"] = datetime.now().isoformat(timespec="seconds")
        payload = {
            "memory": self.longterm,
            "path": str(self.longterm_path),
            "agent_md_path": str(self.agent_md_path),
            "project_md_path": str(self.project_md_path),
        }
        hooks.call_user_participating("memory_write", payload, handler=external_handler)
        self.longterm_path.parent.mkdir(parents=True, exist_ok=True)
        self.longterm_path.write_text(
            json.dumps(self.longterm, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._ensure_md_files()
        log.debug("长期记忆已保存: md=%s", self.agent_md_path)

    def read_agent_md(self) -> str:
        try:
            return self.agent_md_path.read_text(encoding="utf-8") if self.agent_md_path.exists() else ""
        except OSError:
            return ""

    def read_project_md(self) -> str:
        try:
            return self.project_md_path.read_text(encoding="utf-8") if self.project_md_path.exists() else ""
        except OSError:
            return ""

    def _append_to_md(self, path: Path, section: str, line: str) -> bool:
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError:
            text = ""
        if not text:
            text = f"# 记忆\n\n## {section}\n"
        entry = f"- {line}"
        if entry in text:
            return False
        if f"## {section}" in text:
            text = text.replace(f"## {section}", f"## {section}\n{entry}", 1)
        else:
            text = text.rstrip() + f"\n\n## {section}\n{entry}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True

    def add_message(self, role: str, content: str, **extra) -> dict:
        """追加一条消息，extra 可含 type/tool_name/tool_call_id/tool_calls"""
        message = {"role": role, "content": content}
        message.update(extra)
        self.messages.append(message)
        if role == "user" and self._compress_fail_streak:
            # 新用户回合复位压缩熔断：上轮的 API 故障可能已恢复，重试频率限制为每回合一轮
            self._compress_fail_streak = 0
            log.debug("新用户回合，压缩熔断计数复位")
        log.debug(
            "消息追加: role=%s type=%s call_id=%s 当前%d条",
            role,
            extra.get("type") or "-",
            extra.get("tool_call_id") or "-",
            len(self.messages),
        )
        return message

    def add_fact(self, fact: str, scope: str = "agent") -> None:
        """写入长期记忆事实；scope=agent → Agent.md，project → 项目 md"""
        if not fact:
            return
        # 兼容旧精确去重
        if fact in self.longterm.get("facts", []):
            return
        self.longterm.setdefault("facts", []).append(fact)
        path = self.agent_md_path if scope != "project" else self.project_md_path
        section = "用户偏好" if scope != "project" else "项目约定"
        self._append_to_md(path, section, fact)
        self.save_longterm()
        log.info("写入长期记忆(%s): %s -> %s", scope, fact, path)

    def add_project_note(self, note: str) -> None:
        if not note:
            return
        if note not in self.longterm.get("project_notes", []):
            self.longterm.setdefault("project_notes", []).append(note)
        self._append_to_md(self.project_md_path, "项目约定", note)
        self.save_longterm()
        log.info("写入项目记忆: %s", note)

    def set_plan(self, title: str, content: str, complexity: str = "medium") -> dict:
        """更新任务计划"""
        self.plan = {
            "title": title,
            "complexity": complexity,
            "content": content,
            "status": "draft",
        }
        log.info("计划已设置: %s（复杂度=%s，%d字）", title, complexity, len(content))
        return self.plan

    def update_plan_status(self, status: str) -> None:
        self.plan["status"] = status
        log.debug("计划状态: %s", status)

    def set_steps(self, steps: List[dict]) -> None:
        """整体替换步骤列表"""
        normalized = []
        for index, item in enumerate(steps, start=1):
            if isinstance(item, str):
                normalized.append({"id": index, "title": item, "status": "pending", "detail": ""})
            else:
                step = dict(item)
                step.setdefault("id", index)
                step.setdefault("status", "pending")
                step.setdefault("detail", "")
                step.setdefault("title", step.get("name", f"步骤{index}"))
                normalized.append(step)
        self.steps = normalized
        log.info("步骤已设置: %d步", len(steps))

    def update_step_status(self, step_id: int, status: str, detail: str = "") -> Optional[dict]:
        """更新单步状态；用户参与型可挂 step_update 扩展点"""
        target = None
        for step in self.steps:
            if step.get("id") == step_id:
                step["status"] = status
                if detail:
                    step["detail"] = detail
                target = step
                break
        if target is None:
            log.warn("步骤状态更新失败，步骤不存在: id=%s status=%s", step_id, status)
        else:
            log.debug("步骤%d状态: %s %s", step_id, status, detail or "")
        if target is not None:
            hooks.call_user_participating("step_update", {"step": target}, default=None)
        return target

    def set_llm_fn(self, fn: Optional[Callable[[str], str]]) -> None:
        """注入上下文压缩用的 LLM 调用，供 build_messages 循环内自动压缩"""
        self._llm_fn = fn

    def estimate_token_chars(self) -> int:
        """粗估当前消息占用字符数（仅作 fallback，不直接与 token 阈值比较）"""
        total = 0
        for message in self.messages:
            total += len(str(message.get("content") or ""))
        return total

    def estimate_context_tokens(self) -> int:
        """当前上下文 token 估计：优先 tiktoken，不可用时按字符粗折算"""
        from core import tokens as tokenmod

        model = getattr(self.config, "model_name", "") or ""
        try:
            return tokenmod.count_message_tokens(self.messages, model)
        except Exception:
            chars = self.estimate_token_chars()
            return max(1, chars // 2)

    # ----- 工具调用记录管理（白名单保留 / 回合末剥离 / 结果外置磁盘） -----

    def _inline_cap(self, tool_name: str) -> int:
        """单条工具结果的行内 token 上限：读写类工具放宽"""
        if tool_name in self.large_tools:
            return self.inline_limit_tokens_large
        return self.inline_limit_tokens

    @staticmethod
    def _fallback_description(call: dict) -> str:
        """模型未传 description 时的兜底简明记录：工具名+参数摘要"""
        name = str(call.get("name") or "tool")
        args = call.get("arguments") or {}
        try:
            summary = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            summary = str(args)
        summary = " ".join(summary.split())
        if len(summary) > 80:
            summary = summary[:77] + "..."
        return f"{name}({summary})" if summary and summary != "{}" else name

    def add_tool_result(self, call: dict, content: str) -> dict:
        """记录一次工具调用结果：description 随消息留档，行内超限部分立即外置磁盘并附回读指针"""
        tool_name = str(call.get("name") or "")
        call_id = str(call.get("id") or call.get("tool_call_id") or "")
        description = str(call.get("description") or "").strip() or self._fallback_description(call)
        args = call.get("arguments")
        if not isinstance(args, dict):
            args = {}
        total_lines = len(content.splitlines())
        cap = self._inline_cap(tool_name)
        persist_path = None
        if toolstore.count_tokens_safe(content, getattr(self.config, "model_name", "")) > cap:
            persist_path = toolstore.persist(
                self.config, self.project_id, self.session_id,
                call_id, tool_name, args, description, content,
            )
            if persist_path is not None:
                preview = toolstore.slice_to_tokens(content, cap, getattr(self.config, "model_name", ""))
                content = f"{preview}\n\n[{toolstore.pointer_line(persist_path, total_lines)}]"
        message = self.add_message(
            "tool",
            content,
            type="tool",
            tool_name=tool_name,
            tool_call_id=call_id,
            description=description,
        )
        if persist_path is not None:
            message["persisted"] = True
            message["persist_path"] = str(persist_path)
            message["total_lines"] = total_lines
        return message

    def finalize_turn(self) -> None:
        """回合末上下文维护：非白名单工具记录剥离为简明记录（保护最近 strip_keep_recent 条），
        白名单历史累计超预算时最老的先外置，思维链（thinking 字段）打标剥离——回合内已并回
        content 拼接，下一轮对话起不再出站（会话记录保留）。幂等；在 _agent_turn 的 finally
        调用，覆盖正常结束/出错/中断全部路径。tool 消息本体保留以维持 tool_call/tool_result 配对。"""
        if not self.messages:
            return
        model = getattr(self.config, "model_name", "")
        tool_indexes = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"]
        protected = set(tool_indexes[-self.strip_keep_recent:]) if self.strip_keep_recent > 0 else set()
        stripped = 0
        for index in tool_indexes:
            if index in protected:
                continue
            message = self.messages[index]
            if message.get("stripped") or message.get("tool_name") in self.tool_whitelist:
                continue
            if self._strip_tool_message(message, prefix="已省略"):
                stripped += 1
        evicted = self._evict_whitelist_overflow(model)
        think_stripped = 0
        for message in self.messages:
            if (
                message.get("role") == "assistant"
                and message.get("thinking")
                and not message.get("thinking_stripped")
            ):
                message["thinking_stripped"] = True
                think_stripped += 1
        if stripped or evicted or think_stripped:
            log.info(
                "回合末维护: 剥离工具记录%d条 白名单外置%d条 思维链剥离%d条（保护最近%d条）",
                stripped, evicted, think_stripped, self.strip_keep_recent,
            )

    def _strip_tool_message(self, message: dict, prefix: str) -> bool:
        """把单条 tool 消息改写为简明记录（description+call_id+落盘路径）；返回是否改写"""
        content = message.get("content") or ""
        if len(content) < _CONCISE_CONTENT_CHARS:
            return False
        if not message.get("persisted"):
            path = toolstore.persist(
                self.config, self.project_id, self.session_id,
                str(message.get("tool_call_id") or ""),
                str(message.get("tool_name") or ""),
                {}, str(message.get("description") or ""), content,
            )
            if path is not None:
                message["persisted"] = True
                message["persist_path"] = str(path)
                message["total_lines"] = len(content.splitlines())
        parts = [f"[{prefix}·{message.get('tool_name') or 'tool'}]"]
        if message.get("description"):
            parts.append(str(message["description"]))
        if message.get("tool_call_id"):
            parts.append(f"call_id={message['tool_call_id']}")
        if message.get("persist_path"):
            parts.append(f"完整输出: {message['persist_path']}")
        message["content"] = " ".join(parts)
        message["stripped"] = True
        return True

    def _evict_whitelist_overflow(self, model: str) -> int:
        """白名单工具历史会话累计超预算时，最老的先外置为占位，直至回到预算内"""
        budget = self.whitelist_budget_tokens
        if budget <= 0:
            return 0
        entries = []
        for index, message in enumerate(self.messages):
            if (
                message.get("role") == "tool"
                and message.get("tool_name") in self.tool_whitelist
                and not message.get("stripped")
            ):
                entries.append((index, toolstore.count_tokens_safe(message.get("content") or "", model)))
        total = sum(tokens for _, tokens in entries)
        if total <= budget:
            return 0
        evicted = 0
        for index, tokens in entries:
            if total <= budget:
                break
            message = self.messages[index]
            if self._strip_tool_message(message, prefix="白名单外置"):
                message["stripped"] = True
                total -= tokens
                evicted += 1
        return evicted

    # ----- 上下文压缩（轮次切尾 + 软目标摘要 + 校验 + 归档，失败不动历史） -----

    @staticmethod
    def _api_visible(item: dict) -> bool:
        """与 _api_session_item 同规则的可见性判断（UI 专用注入不参与压缩与轮次统计）"""
        return (item.get("type") or "") not in ("system_prompt", "help")

    def _count_tokens(self, messages: List[dict]) -> int:
        """tiktoken 计消息列表 token；不可用时字符数减半兜底（与 estimate_context_tokens 同策略）"""
        from core import tokens as tokenmod

        model = getattr(self.config, "model_name", "") or ""
        try:
            return tokenmod.count_message_tokens(messages, model)
        except Exception:
            chars = sum(len(str(m.get("content") or "")) for m in messages)
            return max(1, chars // 2)

    def _round_boundaries(self) -> List[int]:
        """轮次起始索引：一条可见 user 消息开启一轮，轮内包含完整的 tool_calls→tool 序列"""
        return [
            i
            for i, m in enumerate(self.messages)
            if m.get("role") == "user" and self._api_visible(m)
        ]

    def _tail_start_index(self, budget_tokens: int) -> int:
        """定位尾段起点：从最新轮次向前累积，预算内保留尽量多的轮次（至少保留最后一轮）"""
        boundaries = self._round_boundaries()
        if not boundaries:
            return len(self.messages)
        tail_start = boundaries[-1]
        accumulated = self._count_tokens(self.messages[boundaries[-1]:])
        for i in range(len(boundaries) - 2, -1, -1):
            start = boundaries[i]
            end = boundaries[i + 1]
            round_tokens = self._count_tokens(self.messages[start:end])
            if accumulated + round_tokens > budget_tokens:
                break
            accumulated += round_tokens
            tail_start = start
        return tail_start

    def _compress_fail(self, reason: str) -> str:
        """压缩失败统一出口：计数熔断，历史保持不变"""
        self._compress_fail_streak += 1
        suffix = "，自动压缩暂停" if self._compress_fail_streak >= 2 else ""
        log.warn(
            "压缩中止: %s，历史保持不变（连续失败%d次%s）",
            reason, self._compress_fail_streak, suffix,
        )
        return ""

    def _archive_old_segment(self, old_segment: List[dict]) -> Optional[Path]:
        """旧段原文归档落盘（内存替换后原文仅存于此）；失败不阻塞压缩"""
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.sessions_dir / f"{self.session_id}-precompact-{ts}.json"
            data = {
                "version": session_store.SESSION_VERSION,
                "id": self.session_id,
                "kind": "precompact-archive",
                "archived_at": datetime.now().isoformat(timespec="seconds"),
                "message_count": len(old_segment),
                "messages": old_segment,
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return path
        except OSError as e:
            log.warn("压缩归档写入失败（压缩继续）: %s", e)
            return None

    def _delete_precompact_archives(self) -> None:
        """删除本会话压缩归档（与 /clear 同步，防 /resume 复活旧内容，同 toolstore.clear_session 范式）"""
        if not self.sessions_dir.exists():
            return
        for path in self.sessions_dir.glob(f"{self.session_id}-precompact-*.json"):
            try:
                path.unlink()
                log.debug("压缩归档已删除: %s", path.name)
            except OSError as e:
                log.warn("压缩归档删除失败 %s: %s", path.name, e)

    def compress(self, llm_fn: Optional[Callable[[str], str]] = None, external_handler=None) -> str:
        """上下文压缩：旧段 LLM 结构化摘要 + 保留预算内最近轮次原文

        管线：按轮次边界切尾（预算 compress_keep_recent_tokens，至少保留最后一轮）
        → 旧段先走 memory_compress 钩子，无结果再走 LLM（提示词由 core/prompts 加载，
        token 目标按旧段规模程序计算：5% 钳制到 [150, 800]）
        → 解析校验 state_snapshot 结构（失败重试一次，仍失败按压缩失败处理不替换历史）
        → 旧段归档落盘 → 新上下文 = [摘要] + 尾段。
        任何一步失败返回 "" 且不替换历史。
        """
        if not self.messages:
            return ""
        tail_start = self._tail_start_index(self.compress_keep_recent_tokens)
        old_segment = self.messages[:tail_start]
        tail = self.messages[tail_start:]
        if not old_segment:
            log.debug("压缩跳过: 旧段为空（最近轮次已覆盖全部历史）")
            return ""
        old_tokens = self._count_tokens(old_segment)

        history_text = "\n".join(
            f"{m.get('role')}: {m.get('content')}"
            for m in old_segment
            if self._api_visible(m)
        )
        summary = hooks.call_user_participating(
            "memory_compress",
            {"action": "compress", "history": history_text},
            handler=external_handler,
            default=None,
        )
        if isinstance(summary, dict) and summary.get("summary"):
            summary = summary["summary"]
        structured = None
        if isinstance(summary, str) and summary.strip():
            structured = parse_state_snapshot(summary)
        if not (isinstance(summary, str) and summary.strip()):
            if llm_fn is None:
                return self._compress_fail("无钩子结果且未注入 llm_fn")
            # 软目标：旧段 token 的 5%，钳制到 [150, 800]；硬上限另由校验执行
            target = min(800, max(150, int(old_tokens * 0.05)))
            prompt = (
                prompt_loader.get_compress_prompt(target, config=self.config)
                + "\n\n## 对话历史\n"
                + history_text
            )
            try:
                summary = llm_fn(prompt)
            except Exception as e:
                return self._compress_fail(f"摘要 LLM 调用异常 ({e})")
            structured = parse_state_snapshot(summary)
            if structured is None:
                # 结构校验失败重试一次；仍失败走 _compress_fail 熔断，不可解析的摘要绝不入库
                try:
                    retry_summary = llm_fn(prompt + _COMPRESS_RETRY_HINT)
                except Exception as e:
                    return self._compress_fail(f"摘要重试 LLM 调用异常 ({e})")
                retry_structured = parse_state_snapshot(retry_summary)
                if retry_structured is not None:
                    summary, structured = retry_summary, retry_structured
            if structured is None:
                return self._compress_fail("摘要无法解析为 state_snapshot 结构（已重试一次）")

        # 钩子提供的纯文本摘要保持既有契约（非结构化但可用）；LLM 路径必为结构化
        summary_text = structured["xml"] if structured is not None else summary.strip()
        if len(summary_text.strip()) < 50:
            return self._compress_fail("摘要为空或过短")
        from core import tokens as tokenmod

        model = getattr(self.config, "model_name", "") or ""
        try:
            summary_tokens = tokenmod.count_tokens(summary_text, model)
        except Exception:
            summary_tokens = max(1, len(summary_text) // 2)
        if summary_tokens > self.compress_summary_max_tokens:
            return self._compress_fail(
                f"摘要 {summary_tokens} token 超过上限 {self.compress_summary_max_tokens}"
            )

        self._compress_fail_streak = 0
        archive_path = self._archive_old_segment(old_segment)
        pointer = f"\n\n[完整历史已归档: {archive_path}]" if archive_path else ""
        summary_msg = {
            "role": "system",
            "content": f"[上下文压缩摘要]\n{summary_text}{pointer}",
            "type": "summary",
            "metadata": {
                "compressed_at": datetime.now().isoformat(timespec="seconds"),
                "archived_messages": len(old_segment),
                "retained_messages": len(tail),
                "structured": structured is not None,
            },
        }
        if structured is not None:
            summary_msg["sections"] = structured["sections"]
        self.messages = [summary_msg] + tail
        log.info(
            "上下文压缩完成: 旧段%d条(约%d token) -> 摘要%d token(%s) + 尾段%d条原文%s",
            len(old_segment), old_tokens, summary_tokens,
            "结构化" if structured is not None else "纯文本", len(tail),
            f"，归档={archive_path.name}" if archive_path else "（归档失败，摘要继续）",
        )
        return summary_text

    def maybe_compress(self, llm_fn: Optional[Callable[[str], str]] = None) -> None:
        """按配置阈值自动压缩（阈值与估计均为 token 量纲）；连续失败≥2次熔断，
        新用户回合自动复位（add_message）"""
        if not self.auto_compress:
            return
        if self._compress_fail_streak >= 2:
            log.debug("自动压缩已熔断（连续失败%d次），跳过", self._compress_fail_streak)
            return
        if llm_fn is None:
            llm_fn = self._llm_fn
        limit = int(self.config.context_window * self.compress_threshold)
        if limit <= 0:
            return
        estimate = self.estimate_context_tokens()
        if estimate >= limit:
            log.info("触发自动压缩: 约%d token >= 阈值%d token", estimate, limit)
            self.compress(llm_fn=llm_fn)
        else:
            log.debug("未达压缩阈值: 约%d token/%d token", estimate, limit)

    def build_context_supplements(self) -> List[dict]:
        """长期记忆（Agent.md + 项目 md）+ 计划/步骤（队首 system 区域）"""
        supplements = []
        parts = []
        agent_md = self.read_agent_md().strip()
        project_md = self.read_project_md().strip()
        if agent_md:
            parts.append(f"[长期记忆 Agent.md]\n{agent_md}")
        if project_md:
            parts.append(f"[项目记忆 {self.project_md_path.name}]\n{project_md}")
        # 兼容字段（若 md 未写入但 json 仍有）
        facts = self.longterm.get("facts") or []
        if facts and not agent_md:
            parts.append("长期记忆事实:\n" + "\n".join(f"- {x}" for x in facts[-10:]))
        if self.plan.get("status") not in ("empty", ""):
            parts.append(
                f"当前计划[{self.plan.get('status')}]: {self.plan.get('title')}\n{self.plan.get('content')}"
            )
        if self.steps:
            step_lines = [f"{s.get('id')}. [{s.get('status')}] {s.get('title')}" for s in self.steps]
            parts.append("当前步骤:\n" + "\n".join(step_lines))
        rag_text = self.rag_query("当前任务相关资料")
        if rag_text:
            parts.append(f"项目RAG补充:\n{rag_text}")
        if parts:
            supplements.append(
                {
                    "role": "system",
                    "content": "[记忆补充]\n" + "\n".join(parts),
                    "type": "memory",
                }
            )
        # 技能元数据清单（core/skills.py）：每回合重建，不进会话历史，
        # 故不参与回合末剥离与压缩；正文由模型按需 load_skill 取用
        try:
            from core import skills as skills_mod

            loader = skills_mod.get_loader(self.config)
            if loader is not None:
                listing = skills_mod.filtered_listing(loader)  # 工作流 skill 节点可过滤/关闭
                if listing:
                    supplements.append(
                        {
                            "role": "system",
                            "content": (
                                "[可用技能]\n"
                                "以下技能可按需加载完整操作指南：当任务匹配某技能时，"
                                "先调用 load_skill 工具读取其正文，遵循其中的流程与约定执行。\n"
                                + listing
                            ),
                            "type": "memory",
                        }
                    )
        except Exception as e:
            log.debug("技能清单注入失败: %s", e)
        return supplements

    @staticmethod
    def _api_session_item(item: dict) -> dict:
        """会话消息 → API 消息（DeepSeek Tool Calls 协议）"""
        role = item.get("role") or "user"
        content = item.get("content") or ""
        mtype = item.get("type") or ""
        # UI 专用注入提示不进 API
        if mtype in ("system_prompt", "help"):
            return {}
        out: dict = {"role": role, "content": content if isinstance(content, str) else str(content)}
        if role == "tool":
            out["role"] = "tool"
            if item.get("tool_call_id"):
                out["tool_call_id"] = item["tool_call_id"]
            else:
                # 缺 id 的 tool 不进 API（降级为 system 会破坏 tool_calls 配对）
                return {}
        # thinking 字段（思维链留档）回合内出站时并回 content；回合末 finalize_turn 打
        # thinking_stripped 标后仅留档不再并回——下一轮对话起模型不可见
        thinking = item.get("thinking")
        if thinking and not item.get("thinking_stripped"):
            out["content"] = f"{thinking}\n\n{out['content']}" if out["content"] else thinking
        if role == "assistant" and item.get("tool_calls"):
            import json as _json
            from uuid import uuid4

            calls = []
            for c in item["tool_calls"]:
                if not isinstance(c, dict):
                    continue
                fn_name = c.get("name") or (c.get("function") or {}).get("name") or ""
                args = c.get("arguments")
                if args is None:
                    args = (c.get("function") or {}).get("arguments") or "{}"
                if not isinstance(args, str):
                    args = _json.dumps(args, ensure_ascii=False)
                calls.append(
                    {
                        "id": c.get("id") or f"call_{uuid4().hex[:12]}",
                        "type": "function",
                        "function": {"name": fn_name, "arguments": args},
                    }
                )
            if calls:
                out["tool_calls"] = calls
                if not out["content"]:
                    out["content"] = None
        return out

    def build_messages(self, extra_system: Optional[str] = None) -> List[dict]:
        """组装发送给 LLM 的完整消息列表

        顺序：system提示词 → 长期记忆等补充 → 会话历史（含 tool/tool_calls）。
        """
        self.maybe_compress(llm_fn=self._llm_fn)
        messages: List[dict] = []
        if extra_system:
            messages.append({"role": "system", "content": extra_system})
        messages.extend(self.build_context_supplements())
        for item in self.messages:
            mapped = self._api_session_item(item)
            if mapped:
                messages.append(mapped)
        return messages

    def rag_add(self, text: str, source: str = "", external_handler=None) -> None:
        """项目级 RAG 写入；实现见 core/rag.py（占位：内存 + 外部接口）"""
        self.rag.add(text, source=source, external_handler=external_handler)

    def rag_query(self, query: str, external_handler=None) -> str:
        """项目级 RAG 检索；实现见 core/rag.py"""
        return self.rag.query(query, external_handler=external_handler)

    # ----- 历史会话保存/切换 -----

    @staticmethod
    def _clip_title(text: str, limit: int = 30) -> str:
        """按显示宽度截断标题（中文占 2 列），超长补省略号"""
        out: List[str] = []
        used = 0
        for ch in text:
            cw = _char_width(ch)
            if cw < 0:  # wcwidth 对控制字符返回 -1，按 1 列记
                cw = 1
            if used + cw > limit - 1:
                return "".join(out) + "…"
            out.append(ch)
            used += cw
        return "".join(out)

    def rename_session(self, title: str) -> str:
        """手动命名当前会话（折叠空白、按显示宽度截断）；随下次 save_session 落盘"""
        self.session_title = self._clip_title(" ".join(str(title or "").split()))
        log.info("会话已重命名: %s (id=%s)", self.session_title, self.session_id)
        return self.session_title

    def save_session(self) -> Optional[Path]:
        """自动保存当前会话；空会话不写盘（避免堆积空文件）；标题仅取手动命名"""
        if not self.messages:
            return None
        data = {
            "version": session_store.SESSION_VERSION,
            "id": self.session_id,
            "title": self.session_title,
            "project_id": self.project_id,
            "created_at": self.session_created_at,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "message_count": len(self.messages),
            "messages": self.messages,
            "plan": self.plan,
            "steps": self.steps,
        }
        try:
            path = session_store.save_session_data(self.sessions_dir, data)
        except OSError as e:
            log.error("会话保存失败 %s: %s", self.session_id, e)
            return None
        log.debug("会话已保存: %s (%d条消息)", path, len(self.messages))
        return path

    def switch_to(self, data: dict) -> None:
        """切换到历史会话：原地替换状态（main/ui 持有同一 Memory 实例，不可换对象）"""
        plan_default = {"title": "", "complexity": "low", "content": "", "status": "empty"}
        self.session_id = data.get("id") or session_store.new_session_id()
        self.session_created_at = data.get("created_at") or datetime.now().isoformat(timespec="seconds")
        self.session_title = data.get("title") or ""
        messages = data.get("messages")
        self.messages = list(messages) if isinstance(messages, list) else []
        plan = data.get("plan")
        self.plan = dict(plan) if isinstance(plan, dict) else plan_default
        steps = data.get("steps")
        self.steps = list(steps) if isinstance(steps, list) else []
        log.info("会话已切换: %s · id=%s · %d条消息", self.session_title, self.session_id, len(self.messages))

    def start_new_session(self) -> None:
        """开启新会话：旋转 ID 并清空对话状态（旧会话已落盘，可 /resume 切回）"""
        self.session_id = session_store.new_session_id()
        self.session_created_at = datetime.now().isoformat(timespec="seconds")
        self.session_title = ""
        self.messages.clear()
        self.plan = {"title": "", "complexity": "low", "content": "", "status": "empty"}
        self.steps = []
        log.info("已开启新会话: %s", self.session_id)

    def clear(self) -> None:
        """清空当前会话内容：原地恢复为空对话（保留 id），并删除落盘文件、外置工具记录与压缩归档防 /resume 复活旧内容"""
        session_store.delete_session_data(self.sessions_dir, self.session_id)
        toolstore.clear_session(self.config, self.project_id, self.session_id)
        self._delete_precompact_archives()
        self.messages.clear()
        self.plan = {"title": "", "complexity": "low", "content": "", "status": "empty"}
        self.steps = []
        self.session_title = ""
        log.info("会话已清空: %s", self.session_id)
