#该脚本负责Agent的记忆部分，包含：1.llm参与的上下文压缩2.工具调用记录管理（白名单保留/回合末剥离/结果外置磁盘）3.长期记忆写入配置4.在agent对话时提供记忆补充内容
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Union

from core import hooks
from core import prompt_loader
from core import session_store
from core import toolstore
from core.log import get_logger
from wcwidth import wcwidth as _char_width

log = get_logger("memory")

_ROOT = Path(__file__).resolve().parent.parent

# 工具结果视为"已是简明记录"的长度阈值：拒绝原因/短错误不再剥离改写
_CONCISE_CONTENT_CHARS = 200

# ----- 多模态消息（tool 结果可携带本地图片，content 用 OpenAI 数组形态） -----

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_IMAGE_MAX_BYTES = 4 * 1024 * 1024  # 单图原始字节上限（base64 后约 5.4MB，兼容主流网关请求体限制）
_IMAGE_MAX_COUNT = 4                # 单条工具结果最多携带图片数
# 预算估算时单张图片的粗略 token 计价（token 计数器只认文本，图片按常数计，与 tokens.py 同值）
_IMAGE_TOKEN_ESTIMATE = 768


def content_text(content) -> str:
    """消息 content 的纯文本部分：str 原样返回，数组形态拼接 text 片段"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def count_image_parts(content) -> int:
    """content 中图片块数量（str 恒为 0）"""
    if isinstance(content, list):
        return sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
    return 0


def build_image_parts(paths) -> tuple:
    """本地图片路径列表 → API 图片块（data URL base64）；越界/缺失/超限项降级为文字说明。
    返回 (parts, notes)：parts 追加在 text 块之后，notes 由调用方并入正文说明"""
    import base64

    parts, notes = [], []
    path_list = [str(p) for p in (paths or []) if p]
    if len(path_list) > _IMAGE_MAX_COUNT:
        notes.append(f"仅加载前 {_IMAGE_MAX_COUNT} 张（共 {len(path_list)} 张）")
        path_list = path_list[:_IMAGE_MAX_COUNT]
    for raw in path_list:
        path = Path(raw)
        suffix = path.suffix.lower()
        if suffix not in _IMAGE_MIME:
            notes.append(f"{path.name}: 不支持的图片格式（{suffix or '无后缀'}）")
            continue
        try:
            size = path.stat().st_size
            if size > _IMAGE_MAX_BYTES:
                notes.append(f"{path.name}: 超过单图 {_IMAGE_MAX_BYTES // (1024 * 1024)}MB 上限")
                continue
            data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            notes.append(f"{path.name}: 读取失败（{exc.__class__.__name__}）")
            continue
        parts.append({"type": "image_url", "image_url": {"url": f"data:{_IMAGE_MIME[suffix]};base64,{data}"}})
    return parts, notes

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
    # session_start 观察链：处理函数异常已在 hooks 内隔离
    hooks.collect_hook(
        "session_start",
        {
            "project_id": getattr(_session, "project_id", ""),
            "session_id": getattr(_session, "session_id", ""),
            "workspace": str(getattr(_session, "workspace", "") or ""),
        },
    )
    return _session


def get_session() -> Optional["Memory"]:
    """获取当前记忆会话"""
    return _session


def conv_block_bounds(messages: List[dict], idx: int) -> tuple:
    """消息 idx 所在"原子块"的闭区间 [start, end]：树回退的切割边界。

    - user/普通 assistant（无 tool_calls）：块就是自己
    - assistant(tool_calls)：向后吞掉连续 role=tool 结果（tool_call/tool_result 配对
      不可拆——截在中间 API 会因孤儿 tool 消息报错）
    - role=tool：向前归属其 assistant(tool_calls) 父块（同上再向后吞）
    假设 tool 结果紧跟其 assistant 消息（本项目的记录顺序）。
    """
    n = len(messages)
    if idx < 0 or idx >= n:
        return (idx, idx)
    start = end = idx
    if str(messages[idx].get("role")) == "tool":
        # 向前越过连续 tool 结果，归属紧邻的 assistant(tool_calls) 父块
        start = idx
        while start > 0 and str(messages[start - 1].get("role")) == "tool":
            start -= 1
        if start > 0:
            prev = messages[start - 1]
            if str(prev.get("role")) == "assistant" and prev.get("tool_calls"):
                start -= 1
    if str(messages[start].get("role")) == "assistant" and messages[start].get("tool_calls"):
        end = start
        while end + 1 < n and str(messages[end + 1].get("role")) == "tool":
            end += 1
    return (start, end)


class Memory:
    """Agent 记忆：消息、计划、步骤、长期记忆（md）"""

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
        toolstore.ledger_reset()  # 文件台账是会话级 RAM 状态，新会话从零开始
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
        self.longterm = self._load_longterm()
        self._ensure_md_files()
        self._migrate_legacy_json()
        self.migrate_legacy_memory_files()
        self.ensure_memory_notice()
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
        """旧 memory.json 的 facts/notes 迁入 md 文件（幂等）。

        已落入 md 的条目同步从内存清除，防止 save_longterm 把它们原样写回
        memory.json（否则下次启动重复注入，md 文件永远无法退役）；有残留则
        保留待下次启动重试，不丢数据。"""
        facts = self.longterm.get("facts") or []
        notes = self.longterm.get("project_notes") or []
        if not facts and not notes:
            return
        facts_left = list(facts)
        try:
            text = self.agent_md_path.read_text(encoding="utf-8") if self.agent_md_path.exists() else ""
        except OSError:
            text = ""
        if facts and "## 用户偏好" in text:
            for f in facts:
                line = f"- {f}"
                if line not in text:
                    text = text.replace("## 用户偏好", f"## 用户偏好\n{line}", 1)
            self.agent_md_path.write_text(text, encoding="utf-8")
            facts_left = []
        notes_left = list(notes)
        if notes and self.project_md_path.exists():
            try:
                ptext = self.project_md_path.read_text(encoding="utf-8")
            except OSError:
                ptext = ""
            if "## 项目约定" in ptext:
                for n in notes:
                    line = f"- {n}"
                    if line not in ptext:
                        ptext = ptext.replace("## 项目约定", f"## 项目约定\n{line}", 1)
                self.project_md_path.write_text(ptext, encoding="utf-8")
                notes_left = []
        if not facts_left and not notes_left:
            backup = self.longterm_path.with_suffix(".json.bak")
            try:
                self.longterm_path.replace(backup)
                log.info("旧 memory.json 已迁移并备份: %s", backup)
            except OSError:
                log.warn("memory.json 备份失败")
        self.longterm["facts"] = facts_left
        self.longterm["project_notes"] = notes_left

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

    def add_message(self, role: str, content: str, **extra) -> dict:
        """追加一条消息，extra 可含 type/tool_name/tool_call_id/tool_calls"""
        message = {"role": role, "content": content}
        message.update(extra)
        self.messages.append(message)
        # message_added 观察链：外部同步/转发方消费（content 可能为多模态数组）；
        # 处理函数异常已在 hooks 内隔离，处理函数内不得再写会话消息（会递归）
        hooks.collect_hook("message_added", {"role": role, "content": content, **extra})
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

    # ----- 关键词记忆（多 md 文件） -----
    # global/{关键词}.md 全局记忆：build_context_supplements 全量常驻注入（不设预算）；
    # Projects/{pid}/{关键词}.md 项目记忆：仅注入关键词索引，正文按需 read_memory；
    # 旧 Agent.md / Projects/{pid}.md 启动时一次性拆分迁移（.bak 备份，幂等）

    KEYWORD_MAX_LEN = 40

    @staticmethod
    def sanitize_keyword(keyword) -> str:
        """关键词清洗为合法文件名：非法字符替换为 -、空白折叠、去首尾点、截断"""
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "-", str(keyword or ""))
        cleaned = re.sub(r"\s+", "-", cleaned).strip().strip(".")
        return cleaned[: Memory.KEYWORD_MAX_LEN]

    def memory_dir_for(self, mtype: str) -> Optional[Path]:
        """关键词记忆目录：global=memory/global，project=Projects/{pid}"""
        if mtype == "global":
            return self.memory_dir / "global"
        if mtype == "project":
            return self.projects_dir / (self.project_id or "_default")
        return None

    def list_memory_keywords(self, mtype: str) -> List[str]:
        d = self.memory_dir_for(mtype)
        if d is None or not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.md"))

    def read_memory_file(self, mtype: str, keyword: str) -> Optional[str]:
        d = self.memory_dir_for(mtype)
        key = self.sanitize_keyword(keyword)
        if d is None or not key:
            return None
        path = d / f"{key}.md"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            log.warn("读取记忆失败 %s: %s", path, e)
            return None

    def write_memory_file(self, mtype: str, keyword: str, content) -> tuple:
        """新建关键词记忆；返回 (status, key)，status: created/exists/invalid/error"""
        d = self.memory_dir_for(mtype)
        key = self.sanitize_keyword(keyword)
        if d is None or not key:
            return "invalid", ""
        path = d / f"{key}.md"
        if path.exists():
            return "exists", key
        d.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(str(content or ""), encoding="utf-8")
        except OSError as e:
            log.error("写入记忆失败 %s: %s", path, e)
            return "error", key
        log.info("写入关键词记忆(%s): %s -> %s", mtype, key, path)
        return "created", key

    def update_memory_file(self, mtype: str, keyword: str, content, new_keyword: str = "") -> tuple:
        """更新内容/改名；返回 (status, key)，status: ok/missing/conflict/invalid/error"""
        d = self.memory_dir_for(mtype)
        key = self.sanitize_keyword(keyword)
        if d is None or not key:
            return "invalid", ""
        path = d / f"{key}.md"
        if not path.exists():
            return "missing", key
        new_key = self.sanitize_keyword(new_keyword) if new_keyword else ""
        try:
            if new_key and new_key != key:
                target = d / f"{new_key}.md"
                if target.exists():
                    return "conflict", new_key
                path.write_text(str(content or ""), encoding="utf-8")
                path.rename(target)
                log.info("记忆已更新并改名(%s): %s -> %s", mtype, key, new_key)
                return "ok", new_key
            path.write_text(str(content or ""), encoding="utf-8")
        except OSError as e:
            log.error("更新记忆失败 %s: %s", path, e)
            return "error", key
        log.info("记忆已更新(%s): %s", mtype, key)
        return "ok", key

    def delete_memory_file(self, mtype: str, keyword: str) -> tuple:
        """删除关键词记忆；返回 (status, key)，status: deleted/missing/invalid/error"""
        d = self.memory_dir_for(mtype)
        key = self.sanitize_keyword(keyword)
        if d is None or not key:
            return "invalid", ""
        path = d / f"{key}.md"
        if not path.exists():
            return "missing", key
        try:
            path.unlink()
        except OSError as e:
            log.error("删除记忆失败 %s: %s", path, e)
            return "error", key
        log.info("删除关键词记忆(%s): %s", mtype, key)
        return "deleted", key

    @staticmethod
    def _parse_legacy_md_entries(text: str) -> List[str]:
        """旧长文按 '- ' 列表项拆条目（# 标题/空行切断段落）；
        无列表项但有实质正文段落时整文单条目兜底（纯标题空模板不迁移）"""
        entries: List[str] = []
        cur: List[str] = []
        has_body = False
        for line in (text or "").splitlines():
            s = line.strip()
            if s.startswith("- "):
                if cur:
                    entries.append("\n".join(cur).strip())
                    cur = []
                entries.append(s[2:].strip())
                has_body = True
            elif s.startswith("#") or not s:
                if cur:
                    entries.append("\n".join(cur).strip())
                    cur = []
            else:
                cur.append(s)
                has_body = True
        if cur:
            entries.append("\n".join(cur).strip())
        if not entries and has_body and (text or "").strip():
            entries = [text.strip()]
        return [e for e in entries if e]

    def migrate_legacy_memory_files(self) -> None:
        """旧 Agent.md / 项目 md 拆分为关键词 md：条目逐条成文件，旧文件 .bak 备份。

        幂等：新目录已有 md 时跳过（二次启动/迁移失败重跑安全）。"""
        for path, mtype in ((self.agent_md_path, "global"), (self.project_md_path, "project")):
            if not path.exists():
                continue
            d = self.memory_dir_for(mtype)
            if d is None:
                continue
            if d.exists() and any(d.glob("*.md")):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                log.warn("旧记忆读取失败 %s: %s", path, e)
                continue
            entries = self._parse_legacy_md_entries(text)
            if not entries:
                continue
            d.mkdir(parents=True, exist_ok=True)
            for item in entries:
                base = self.sanitize_keyword(item.splitlines()[0]) if item else ""
                key = base or "migrated"
                n = 1
                while (d / f"{key}.md").exists():
                    n += 1
                    key = f"{base or 'migrated'}-{n}"
                try:
                    (d / f"{key}.md").write_text(item, encoding="utf-8")
                except OSError as e:
                    log.warn("迁移条目写入失败 %s: %s", key, e)
            backup = path.with_name(path.name + ".bak")
            try:
                path.rename(backup)
                log.info("旧记忆已迁移: %s -> %s（%d 条，备份 %s）", path, d, len(entries), backup)
            except OSError as e:
                log.warn("旧记忆备份失败 %s: %s（条目已写入新目录）", path, e)

    def ensure_memory_notice(self) -> None:
        """会话树注入全局记忆清单提示（幂等）：替换旧提示保持清单最新；无全局记忆不显示"""
        keywords = self.list_memory_keywords("global")
        if not keywords:
            return
        content = f"[已注入全局记忆 {len(keywords)} 条：{'、'.join(keywords)}]"
        for m in self.messages:
            if m.get("role") == "system" and str(m.get("content") or "").startswith("[已注入全局记忆"):
                m["content"] = content
                return
        self.add_message("system", content, type="help")

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

    def set_steps(self, steps: List[Union[str, dict]]) -> None:
        """整体替换步骤列表（str 项归一化为 pending 状态 dict）"""
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
            total += len(content_text(message.get("content")))
        return total

    def estimate_context_tokens(self) -> int:
        """当前上下文 token 估计：优先 tiktoken，不可用时按字符粗折算"""
        return self._count_tokens(self.messages)

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

    def add_tool_result(self, call: dict, content, images=None) -> dict:
        """记录一次工具调用结果：description 随消息留档，行内超限部分立即外置磁盘并附回读指针；
        images 为本地图片路径列表（多模态），与正文一并落为 API 数组形态 content（外置只落文本）"""
        tool_name = str(call.get("name") or "")
        call_id = str(call.get("id") or call.get("tool_call_id") or "")
        description = str(call.get("description") or "").strip() or self._fallback_description(call)
        args = call.get("arguments")
        if not isinstance(args, dict):
            args = {}
        if not isinstance(content, str):
            content = content_text(content)
        image_parts, image_notes = build_image_parts(images)
        if image_notes:
            content = (content.rstrip() + "\n" if content.strip() else "") + "[图片说明] " + "；".join(image_notes)
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
        # 数组形态：text 块在前，图片块随后
        message_content: Union[str, list] = (
            [{"type": "text", "text": content}] + image_parts if image_parts else content
        )
        extra = {
            "type": "tool",
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "description": description,
        }
        if tool_name == "read":
            file_path = str((args or {}).get("file_path") or "")
            if file_path:
                extra["file_path"] = file_path  # 随消息留档，供回合末剥离记录附新鲜度状态
        message = self.add_message("tool", message_content, **extra)
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
        text = content_text(content)
        # 带图片的记录不受短文本守卫保护：图片滞留上下文的代价远高于一条短记录
        if len(text) < _CONCISE_CONTENT_CHARS and not count_image_parts(content):
            return False
        if not message.get("persisted"):
            path = toolstore.persist(
                self.config, self.project_id, self.session_id,
                str(message.get("tool_call_id") or ""),
                str(message.get("tool_name") or ""),
                {}, str(message.get("description") or ""), text,
            )
            if path is not None:
                message["persisted"] = True
                message["persist_path"] = str(path)
                message["total_lines"] = len(text.splitlines())
        parts = [f"[{prefix}·{message.get('tool_name') or 'tool'}]"]
        if message.get("description"):
            parts.append(str(message["description"]))
        if message.get("tool_call_id"):
            parts.append(f"call_id={message['tool_call_id']}")
        if message.get("persist_path"):
            parts.append(f"完整输出: {message['persist_path']}")
        if message.get("tool_name") == "read" and message.get("file_path"):
            # 剥离后的 read 记录升级为状态路标：模型据此判断可否直接 edit_file，免一次试探
            parts.append(f"[{toolstore.ledger_fresh_hint(Path(message['file_path']))}]")
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
                # 图片块 token 计数器看不见，按常数计入预算防图片记录挤占上下文
                tokens = toolstore.count_tokens_safe(content_text(message.get("content") or ""), model)
                tokens += _IMAGE_TOKEN_ESTIMATE * count_image_parts(message.get("content"))
                entries.append((index, tokens))
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
        return (item.get("type") or "") not in ("system_prompt", "help", "workflow_node")

    def _count_tokens(self, messages: List[dict]) -> int:
        """tiktoken 计消息列表 token；不可用时字符数减半兜底（estimate_context_tokens 共用）"""
        from core import tokens as tokenmod

        model = getattr(self.config, "model_name", "") or ""
        try:
            return tokenmod.count_message_tokens(messages, model)
        except Exception:
            chars = sum(len(content_text(m.get("content"))) for m in messages)
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

        # content 取纯文本部分：数组形态（图片块）直接内插会把 base64 整段拼进提示词
        history_text = "\n".join(
            f"{m.get('role')}: {content_text(m.get('content'))}"
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
        """关键词记忆（全局全量+项目索引）+ 计划/步骤（队首 system 区域）"""
        supplements = []
        parts = []
        # 全局记忆：每轮全量常驻注入（每回合重建，不参与回合末剥离/压缩，不设预算）
        global_blocks = []
        for kw in self.list_memory_keywords("global"):
            text = (self.read_memory_file("global", kw) or "").strip()
            if text:
                global_blocks.append(f"## {kw}\n{text}")
        if global_blocks:
            parts.append("[全局记忆]\n" + "\n\n".join(global_blocks))
        # 项目记忆：只注入关键词索引，正文按需 read_memory（结果入工具白名单不被剥离）
        project_keywords = self.list_memory_keywords("project")
        if project_keywords:
            parts.append(
                "[项目记忆索引] " + "、".join(project_keywords)
                + "\n（正文不自动注入；需要时用 read_memory 工具按关键词读取）"
            )
        if self.plan.get("status") not in ("empty", ""):
            parts.append(
                f"当前计划[{self.plan.get('status')}]: {self.plan.get('title')}\n{self.plan.get('content')}"
            )
        if self.steps:
            step_lines = [f"{s.get('id')}. [{s.get('status')}] {s.get('title')}" for s in self.steps]
            parts.append("当前步骤:\n" + "\n".join(step_lines))
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
        # 插件上下文补充（core/plugins.py）：context_supplement 观察链收集文本，
        # 与技能清单同为每回合重建，不进会话历史（处理函数异常已在 hooks 内隔离）。
        # payload 带当前用户消息（最后一条可见 user 消息），供 RAG 类插件做
        # 按消息内容的主动召回（数组形态 content 取文本部分，防图片块 repr 污染检索）
        user_message = ""
        for m in reversed(self.messages):
            if m.get("role") == "user" and self._api_visible(m):
                user_message = content_text(m.get("content"))
                break
        plugin_texts = [
            t for t in hooks.collect_hook(
                "context_supplement",
                {"project_id": self.project_id, "session_id": self.session_id,
                 "user_message": user_message},
            )
            if isinstance(t, str) and t.strip()
        ]
        if plugin_texts:
            supplements.append(
                {
                    "role": "system",
                    "content": "[插件补充]\n" + "\n".join(plugin_texts),
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
        if mtype in ("system_prompt", "help", "workflow_node"):
            return {}
        # content 原样出站：str 或数组形态（多模态图片块）均透传
        out: dict = {"role": role, "content": content if isinstance(content, (str, list)) else str(content)}
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
        if thinking and not item.get("thinking_stripped") and isinstance(out["content"], str):
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
        toolstore.ledger_reset()  # 台账不跨会话：resume 后首次 edit 需重新 read（安全优先）
        self.ensure_memory_notice()
        log.info("会话已切换: %s · id=%s · %d条消息", self.session_title, self.session_id, len(self.messages))

    def start_new_session(self) -> None:
        """开启新会话：旋转 ID 并清空对话状态（旧会话已落盘，可 /resume 切回）"""
        self.session_id = session_store.new_session_id()
        self.session_created_at = datetime.now().isoformat(timespec="seconds")
        self.session_title = ""
        self.messages.clear()
        self.plan = {"title": "", "complexity": "low", "content": "", "status": "empty"}
        self.steps = []
        toolstore.ledger_reset()
        self.ensure_memory_notice()
        log.info("已开启新会话: %s", self.session_id)

    def clear(self) -> None:
        """清空当前会话内容：原地恢复为空对话（保留 id），并删除落盘文件、外置工具记录与压缩归档防 /resume 复活旧内容"""
        session_store.delete_session_data(self.sessions_dir, self.session_id)
        toolstore.clear_session(self.config, self.project_id, self.session_id)
        self._delete_precompact_archives()
        toolstore.ledger_reset()
        self.messages.clear()
        self.plan = {"title": "", "complexity": "low", "content": "", "status": "empty"}
        self.steps = []
        self.session_title = ""
        self.ensure_memory_notice()
        log.info("会话已清空: %s", self.session_id)
