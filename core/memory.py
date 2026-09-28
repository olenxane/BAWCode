#该脚本负责Agent的记忆部分，包含：1.llm参与的上下文压缩2.不必要的工具调用历史剥离3.长期记忆写入配置4.在agent对话时提供记忆补充内容5.针对特定项目的RAG模块
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from core import hooks
from core import rag as rag_mod
from core import session_store
from core.log import get_logger
from wcwidth import wcwidth as _char_width

log = get_logger("memory")

_ROOT = Path(__file__).resolve().parent.parent

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
        self.strip_tool_history = memory_cfg.get("strip_tool_history", True)
        self.strip_tool_keep = memory_cfg.get("strip_tool_keep", 6)
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

    def strip_old_tool_messages(self) -> int:
        """剥离多余的历史工具输出，保留最近若干条，其余替换为占位"""
        if not self.strip_tool_history:
            return 0
        tool_indexes = [i for i, m in enumerate(self.messages) if m.get("role") == "tool"]
        if len(tool_indexes) <= self.strip_tool_keep:
            return 0
        to_strip = tool_indexes[: -self.strip_tool_keep]
        for index in to_strip:
            self.messages[index]["content"] = "[已剥离的工具历史]"
            self.messages[index]["stripped"] = True
        log.debug("剥离历史工具输出: %d条（保留最近%d条）", len(to_strip), self.strip_tool_keep)
        return len(to_strip)

    def compress(self, llm_fn: Optional[Callable[[str], str]] = None, external_handler=None) -> str:
        """上下文压缩"""
        self.strip_old_tool_messages()
        if not self.messages:
            return ""
        history_text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in self.messages)
        summary = hooks.call_user_participating(
            "memory_compress",
            {"action": "compress", "history": history_text},
            handler=external_handler,
            default=None,
        )
        if not isinstance(summary, str) or not summary:
            if isinstance(summary, dict) and summary.get("summary"):
                summary = summary["summary"]
            elif llm_fn is not None:
                prompt = (
                    "请将以下对话历史压缩为简明摘要，保留任务目标、已完成步骤、"
                    "关键结论与未决问题，不超过300字：\n\n" + history_text
                )
                summary = llm_fn(prompt)
            else:
                return ""
        self.messages = [
            {
                "role": "system",
                "content": f"[上下文压缩摘要]\n{summary}",
                "type": "summary",
            }
        ]
        log.info("上下文压缩完成: %d字 -> 摘要%d字（%d条消息）", len(history_text), len(summary), len(self.messages))
        return summary

    def maybe_compress(self, llm_fn: Optional[Callable[[str], str]] = None) -> None:
        """按配置阈值自动压缩（阈值与估计均为 token 量纲）"""
        if not self.auto_compress:
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
        self.strip_old_tool_messages()
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

    def _derive_session_title(self) -> str:
        """标题 = 第一条用户任务输入（折叠空白后截断）；无任务输入时用占位"""
        for item in self.messages:
            if item.get("type") == "task" and item.get("role") == "user":
                text = " ".join(str(item.get("content") or "").split())
                if text:
                    return self._clip_title(text)
        return "（无标题会话）"

    def save_session(self) -> Optional[Path]:
        """自动保存当前会话；空会话不写盘（避免堆积空文件）"""
        if not self.messages:
            return None
        self.session_title = self._derive_session_title()
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
        """开启新会话：旋转 ID 并清空对话状态（旧会话已落盘，可 /sessions 切回）"""
        self.session_id = session_store.new_session_id()
        self.session_created_at = datetime.now().isoformat(timespec="seconds")
        self.session_title = ""
        self.messages.clear()
        self.plan = {"title": "", "complexity": "low", "content": "", "status": "empty"}
        self.steps = []
        log.info("已开启新会话: %s", self.session_id)
