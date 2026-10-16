#该脚本负责技能（Skill）加载：渐进式披露三层——启动只扫 SKILL.md frontmatter 元数据
#（每条百 token 级，进 [可用技能] 补充清单），模型按需经 load_skill 工具读正文，
#scripts/references 再按需经 read 工具取用；双层目录：全局 data/skills + 项目
#{workspace}/.bawcode/skills（项目级同名覆盖全局，与 Agent.md/Projects 范式一致）。
#格式兼容 SKILL.md + YAML frontmatter 规范，正文超限由 memory.add_tool_result
#行内限额统一外置，本模块不做二次切片。
import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from core.log import get_logger
from core import prompt_loader
from core import toolstore

log = get_logger("skills")

_ROOT = Path(__file__).resolve().parent.parent
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

try:
    import yaml
except ImportError:  # pyyaml 缺失时技能系统降级关闭（提示一次，不影响主流程）
    yaml = None
    log.warn("pyyaml 未安装，技能系统不可用（pip install pyyaml）")


class Skill:
    """技能条目：元数据必填，正文懒加载"""

    __slots__ = ("name", "description", "path", "dir", "body")

    def __init__(self, name: str, description: str, path: Path, body: str = ""):
        self.name = name
        self.description = description
        self.path = path
        self.dir = path.parent
        self.body = body

    @property
    def scripts(self) -> List[Path]:
        scripts_dir = self.dir / "scripts"
        if not scripts_dir.exists():
            return []
        return sorted(f for f in scripts_dir.rglob("*") if f.is_file())

    @property
    def references(self) -> List[Path]:
        ref_dir = self.dir / "references"
        if not ref_dir.exists():
            return []
        return sorted(f for f in ref_dir.rglob("*") if f.is_file())

    @property
    def token_count(self) -> int:
        """正文 token 数（tiktoken 不可用时字符数折半，与 toolstore 口径一致）"""
        return toolstore.count_tokens_safe(self.body)


def _parse_frontmatter(path: Path) -> Optional[Dict]:
    """仅解析 SKILL.md 的 YAML frontmatter；缺失/缺字段返回 None"""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warn("技能文件读取失败 %s: %s", path, e)
        return None
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except Exception as e:
        log.warn("技能 frontmatter 解析失败 %s: %s", path, e)
        return None
    if not isinstance(metadata, dict) or not metadata.get("description"):
        return None
    return metadata


def _split_body(path: Path) -> str:
    """读取 frontmatter 之后的正文"""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _FRONTMATTER_RE.sub("", content, count=1).strip()


def _resolve_config(config=None, workspace: Optional[Path] = None):
    """解析 skills 配置面：enabled/全局目录/项目目录/清单预算"""
    skills_cfg = (getattr(config, "data", None) or {}).get("skills", {}) if config else {}
    enabled = bool(skills_cfg.get("enabled", True)) and yaml is not None
    rel_dir = skills_cfg.get("dir") or "data/skills"
    p = Path(rel_dir)
    global_dir = p if p.is_absolute() else _ROOT / rel_dir
    ws = Path(workspace or Path.cwd())
    project_dir = ws / ".bawcode" / "skills"
    budget = int(skills_cfg.get("metadata_budget_tokens", 1024))
    return enabled, global_dir, project_dir, budget


# 插件技能目录（core/plugins.py 装载时注入）：扫描顺序 全局 → 插件 → 项目
_extra_dirs: List[Path] = []


def set_extra_dirs(dirs: Optional[List[Path]]) -> None:
    """设置插件技能目录（整体替换）；None/空清空。项目级目录始终最后覆盖"""
    global _extra_dirs
    _extra_dirs = [Path(d) for d in (dirs or [])]


class SkillLoader:
    """技能加载器：启动仅扫元数据，正文按需读取并缓存，支持热重载"""

    def __init__(self, config=None, workspace: Optional[Path] = None):
        self.enabled, self.global_dir, self.project_dir, self.metadata_budget_tokens = _resolve_config(
            config, workspace
        )
        self.extra_dirs = list(_extra_dirs)
        # name -> {"description", "path"}（仅元数据）；正文缓存 name -> Skill
        self._metadata: Dict[str, dict] = {}
        self._loaded: Dict[str, Skill] = {}
        if self.enabled:
            self.scan()

    def _scan_bases(self) -> List[Path]:
        """扫描顺序：全局 → 插件目录 → 项目（后者同名覆盖前者）"""
        return [self.global_dir, *self.extra_dirs, self.project_dir]

    def scan(self) -> int:
        """扫描多层目录，仅解析 frontmatter；后扫的同名覆盖先扫的。返回技能数"""
        self._metadata.clear()
        self._loaded.clear()
        if not self.enabled:
            return 0
        for base in self._scan_bases():
            if not base.is_dir():
                continue
            for skill_md in sorted(base.glob("*/SKILL.md")):
                metadata = _parse_frontmatter(skill_md)
                if metadata is None:
                    log.warn("技能跳过（frontmatter 缺 name/description）: %s", skill_md)
                    continue
                name = str(metadata.get("name") or skill_md.parent.name)
                if name in self._metadata:
                    log.debug("技能 %s 被覆盖: %s", name, skill_md)
                self._metadata[name] = {
                    "description": str(metadata["description"]).strip(),
                    "path": skill_md,
                }
        log.info(
            "技能扫描完成: %d 个（全局 %s · 插件 %d · 项目 %s）",
            len(self._metadata),
            self.global_dir,
            len(self.extra_dirs),
            self.project_dir,
        )
        return len(self._metadata)

    def reload(self) -> int:
        return self.scan()

    def list_names(self) -> List[str]:
        return list(self._metadata.keys())

    def __len__(self) -> int:
        return len(self._metadata)

    def listing(self) -> str:
        """元数据清单（供 [可用技能] 注入）；超预算先降级为仅名称，再截断"""
        if not self._metadata:
            return ""
        lines = [f"- {name}: {meta['description']}" for name, meta in self._metadata.items()]
        text = "\n".join(lines)
        if toolstore.count_tokens_safe(text) <= self.metadata_budget_tokens:
            return text
        text = "\n".join(f"- {name}" for name in self._metadata)
        log.warn("技能清单超预算（%d tokens），已降级为仅名称", self.metadata_budget_tokens)
        max_chars = self.metadata_budget_tokens * 2
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（更多技能见 /skill）"
        return text

    def load(self, name: str) -> Optional[Skill]:
        """按需加载正文（第三层 resources 由模型经 read 工具自行取用）"""
        if not self.enabled:
            return None
        key = str(name or "").strip()
        if key in self._loaded:
            return self._loaded[key]
        meta = self._metadata.get(key)
        if meta is None:
            return None
        skill = Skill(key, meta["description"], meta["path"], body=_split_body(meta["path"]))
        self._loaded[key] = skill
        return skill


# 注入过滤（工作流 skill 节点经 set_injection 设置；None=默认不过滤）
# enabled=False → 清单整段不注入；allow 非空 → 仅注入白名单内技能
_injection: Dict[str, object] = {"enabled": None, "allow": None}


def set_injection(enabled: Optional[bool] = None, allow: Optional[List[str]] = None) -> None:
    """设置回合级注入过滤；None 恢复默认（不过滤）。工作流每回合开始时重置"""
    _injection["enabled"] = enabled
    _injection["allow"] = list(allow) if allow else None


def filtered_listing(loader: Optional[SkillLoader]) -> str:
    """经注入过滤的清单（memory.build_context_supplements 使用）"""
    if loader is None:
        return ""
    if _injection["enabled"] is False:
        return ""
    allow = _injection["allow"]
    if not allow:
        return loader.listing()
    listing = loader.listing()
    if not listing:
        return ""
    keep = []
    for line in listing.splitlines():
        name = line.lstrip("- ").split(":", 1)[0].strip()
        if name in allow:
            keep.append(line)
    return "\n".join(keep)


# 全局单例（会话间复用；配置签名变化时重建，换技能目录也可走 /skill reload）
_loader: Optional[SkillLoader] = None


def get_loader(config=None, workspace: Optional[Path] = None) -> Optional[SkillLoader]:
    global _loader
    enabled, global_dir, project_dir, budget = _resolve_config(config, workspace)
    signature = (enabled, str(global_dir), str(project_dir), budget, tuple(str(d) for d in _extra_dirs))
    if _loader is None or getattr(_loader, "signature", None) != signature:
        _loader = SkillLoader(config=config, workspace=workspace)
        _loader.signature = signature
    return _loader if _loader.enabled else None


def reset_loader() -> None:
    """测试/重载辅助：丢弃单例，下次 get_loader 重建"""
    global _loader
    _loader = None


# ----- 技能生成（/skill summon）：完整上下文 + 技能生成提示词请求 API，产物落盘后重扫生效 -----

# 落盘级别：global → 全局技能目录（config.skills.dir）；project → 项目 {workspace}/.bawcode/skills
SUMMON_SCOPES = ("global", "project")

# 模型可能把 SKILL.md 包进代码围栏或先说一段前言，故按行首 --- 定位 frontmatter 起点
_FRONTMATTER_SCAN_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL | re.MULTILINE)
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*$", re.MULTILINE)
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*$")

# 解析失败时回显给用户看的内容长度
SUMMON_PREVIEW_CHARS = 200


def strip_code_fence(text: str) -> str:
    """取出整体包裹的 Markdown 围栏，保留 SKILL.md 正文中的代码示例"""
    raw = str(text or "").strip()
    lines = raw.splitlines()
    openings = [(i, _FENCE_OPEN_RE.fullmatch(line.rstrip())) for i, line in enumerate(lines)]
    opening = next(((i, match) for i, match in openings if match is not None), None)
    if opening is None:
        return raw
    start, match = opening
    frontmatter = next((i for i, line in enumerate(lines) if line.strip() == "---"), None)
    if frontmatter is None or frontmatter <= start:
        return raw
    fence = match.group(1)
    end = next(
        (
            i for i in range(len(lines) - 1, start, -1)
            if (closing := _FENCE_CLOSE_RE.fullmatch(lines[i].rstrip()))
            and closing.group(1)[0] == fence[0]
            and len(closing.group(1)) >= len(fence)
        ),
        None,
    )
    if end is None or frontmatter >= end:
        return raw
    return "\n".join(lines[start + 1:end]).strip()


def parse_generated_skill(text: str) -> Optional[Dict]:
    """解析模型产出的 SKILL.md：返回 {"name", "description", "content"}；缺 name/description 返回 None"""
    if yaml is None:
        return None
    cleaned = strip_code_fence(text)
    match = _FRONTMATTER_SCAN_RE.search(cleaned)
    if not match:
        return None
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except Exception as e:
        log.warn("生成技能 frontmatter 解析失败: %s", e)
        return None
    if not isinstance(metadata, dict):
        return None
    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    if not name or not description:
        return None
    return {"name": name, "description": description, "content": cleaned[match.start():].strip() + "\n"}


def skill_dir_name(name: str) -> str:
    """技能名收敛为单层目录名：去掉路径分隔与首尾点，避免写到技能目录之外"""
    cleaned = re.sub(r"[^\w.\-]+", "-", str(name or "").strip(), flags=re.UNICODE).strip("-._")
    return cleaned or "skill"


def save_generated_skill(text: str, base_dir) -> Dict:
    """生成的 SKILL.md 文本写入 base_dir/<name>/SKILL.md；同名技能已存在时不覆盖"""
    parsed = parse_generated_skill(text)
    if parsed is None:
        preview = strip_code_fence(text)[:SUMMON_PREVIEW_CHARS]
        return {"ok": False, "error": f"返回内容不是合法 SKILL.md（frontmatter 缺 name/description）：{preview}"}
    target = Path(base_dir) / skill_dir_name(parsed["name"]) / "SKILL.md"
    if target.exists():
        return {"ok": False, "error": f"同名技能已存在，未覆盖：{target}"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(parsed["content"], encoding="utf-8")
    log.info("技能已生成: %s", target)
    return {"ok": True, "name": parsed["name"], "description": parsed["description"], "path": target}


# 生成保护：同一请求可能被重复投递（客户端重试/网络层重放），而模型每次自取的名字不同，
# 仅靠"同名不覆盖"拦不住，于是同一份上下文会落下两份技能。这里在 API 调用前收口：
#   1) 生成在途 → 直接拒绝并发重复提交，不再发出第二次请求；
#   2) 短时间内已完成的同请求 → 复用上次结果，不再发出请求。
_summon_guard = threading.Lock()
_summon_inflight = False
_summon_recent: Dict[str, dict] = {}
SUMMON_DEDUP_SECONDS = 120
SUMMON_RECENT_MAX = 8

# 仅 UI 可见、不进 API 的消息类型（与 memory._api_session_item 同口径）
_SUMMON_HIDDEN_TYPES = ("system_prompt", "help", "workflow_node", "subagent")


def _summon_fingerprint(session, scope: str) -> str:
    """请求指纹：会话 + 落盘级别 + 出站上下文；UI 专用消息不参与"""
    digest = hashlib.sha1()
    digest.update(f"{getattr(session, 'session_id', '')}|{scope}|".encode("utf-8"))
    for item in getattr(session, "messages", None) or []:
        if (item.get("type") or "") in _SUMMON_HIDDEN_TYPES:
            continue
        digest.update(f"{item.get('role')}\x1f{item.get('content')}\x1e".encode("utf-8", "replace"))
    return digest.hexdigest()


def _recent_summon_result(fingerprint: str) -> Optional[Dict]:
    """取短时间内已完成的同请求结果；顺带清理过期项"""
    now = time.monotonic()
    for key in [k for k, v in _summon_recent.items() if now - v["at"] > SUMMON_DEDUP_SECONDS]:
        _summon_recent.pop(key, None)
    entry = _summon_recent.get(fingerprint)
    return dict(entry["result"]) if entry else None


def _remember_summon_result(fingerprint: str, result: Dict) -> None:
    """只记住成功结果：失败（含 API 报错）允许下次重试"""
    _summon_recent[fingerprint] = {"at": time.monotonic(), "result": dict(result)}
    for key in list(_summon_recent)[:-SUMMON_RECENT_MAX]:
        _summon_recent.pop(key, None)


def summon(session, llm, config, scope: str = "global", system_prompt: Optional[str] = None) -> Dict:
    """按当前会话的完整上下文生成技能

    系统提示词仍走 extra_system、技能生成提示词只作为末条用户消息发出，
    请求前缀与普通回合一致（提示词缓存命中）；产物按 scope 落盘并重扫使其立即可用。
    重复提交只调用一次 API、只落盘一份，返回值带 duplicate 标记。
    """
    global _summon_inflight

    workspace = getattr(session, "workspace", None)
    enabled, global_dir, project_dir, _budget = _resolve_config(config, workspace)
    if not enabled:
        return {"ok": False, "error": "技能系统未启用（config.skills.enabled 或缺少 pyyaml）"}
    key = str(scope or "global").strip().lower()
    if key not in SUMMON_SCOPES:
        return {"ok": False, "error": f"未知落盘级别：{scope}（可用 {'/'.join(SUMMON_SCOPES)}）"}

    fingerprint = _summon_fingerprint(session, key)
    cached = _recent_summon_result(fingerprint)
    if cached is not None:
        log.info("技能生成重复提交，复用上次结果: %s", cached.get("name"))
        return dict(cached, duplicate=True)

    with _summon_guard:
        if _summon_inflight:
            log.info("技能生成进行中，忽略重复提交")
            return {"ok": False, "duplicate": True, "error": "技能生成进行中：本次重复提交已忽略，结果见进行中的那次"}
        _summon_inflight = True
    try:
        if system_prompt is None:
            system_files = None
            from core import workflow as workflow_mod

            if workflow_mod.workflow_enabled(config):
                workflow_data = workflow_mod.load_workflow(config)
                for node in workflow_data.get("nodes") or []:
                    if node.get("enabled", True) and node.get("type") == "system_prompt":
                        system_files = node.get("files") or ["system_prompt.md"]
            system_prompt = prompt_loader.get_system_prompt(config=config, files=system_files)
        messages = session.build_messages(extra_system=system_prompt)
        messages.append({"role": "user", "content": prompt_loader.get_skill_prompt(config=config)})
        result = llm.chat(messages)
        if result.get("error"):
            return {"ok": False, "error": f"技能生成请求失败：{result['error']}"}
        saved = save_generated_skill(result.get("content") or "", global_dir if key == "global" else project_dir)
        saved["scope"] = key
        if saved.get("ok"):
            loader = get_loader(config, workspace)
            saved["total"] = loader.reload() if loader is not None else 0
            _remember_summon_result(fingerprint, saved)
        return saved
    finally:
        with _summon_guard:
            _summon_inflight = False
