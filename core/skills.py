#该脚本负责技能（Skill）加载：渐进式披露三层——启动只扫 SKILL.md frontmatter 元数据
#（每条百 token 级，进 [可用技能] 补充清单），模型按需经 load_skill 工具读正文，
#scripts/references 再按需经 read 工具取用；双层目录：全局 data/skills + 项目
#{workspace}/.bawcode/skills（项目级同名覆盖全局，与 Agent.md/Projects 范式一致）。
#格式兼容 SKILL.md + YAML frontmatter 规范，正文超限由 memory.add_tool_result
#行内限额统一外置，本模块不做二次切片。
import re
from pathlib import Path
from typing import Dict, List, Optional

from core.log import get_logger
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
