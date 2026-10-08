# -*- coding: utf-8 -*-
"""设置面板：标签页表单、字段构建、交互循环、行内编辑、保存应用。

从 core/ui.py 抽出，以混入类 SettingsPanelMixin 供 TuiApp 继承。文本字段编辑
走 core.keyinput（prompt_toolkit 后端）的行内编辑器——与主输入框同一输入管线，
中文 IME 上屏（char 事件）可用；不再借道内建 input()（其已被 raw_mode 关闭
行缓冲/回显，且泵线程会抽干控制台输入）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import keyinput
from core import policy
from core.config import THINKING_OPTIONS, make_model_name
from core.log import get_logger
from core.textbuf import TextBuffer

log = get_logger("settings")


# ----- ui 层纯函数（模块级，运行期惰性导入避免循环依赖）-----

def _clip(text: str, width: int) -> str:
    from core import ui
    return ui._clip(text, width)


def _clip_keep_ansi(text: str, width: int) -> str:
    from core import ui
    return ui._clip_keep_ansi(text, width)


def _pad(text: str, width: int) -> str:
    from core import ui
    return ui._pad(text, width)


def _display_width(text: str) -> int:
    from core import ui
    return ui._display_width(text)


def _read_key():
    return keyinput.read_key_event()


def _flush_input() -> None:
    keyinput.flush_input()


def _key_direction(kind: str, value: str) -> Optional[str]:
    return keyinput.direction_of(kind, value)


class SettingsPanelMixin:
    """设置面板状态与方法（由 TuiApp 继承）。"""

    def _init_settings_state(self) -> None:
        self.settings_mode = False
        self.settings_fields: List[dict] = []
        self.settings_index = 0
        self.settings_scratch: Dict[str, Any] = {}
        self.settings_tab = 0
        self._plugin_expanded: set = set()
        self.settings_provider_id = ""
        self.settings_model_name = ""
        self._settings_scroll = 0
        self.settings_notice = ""
        self.settings_model_work: Dict[str, List[dict]] = {}
        self._settings_baseline: Dict[str, Any] = {}
        self._settings_esc_stage = 0
        self.settings_edit: Optional[dict] = None

    def _compose_settings(self, w: int, h: int) -> List[str]:
        """设置：标签页 + 表单；choice 用 ←→，text 直接键入"""
        tabs = self.settings_tabs
        tab_i = self.settings_tab
        lines = []
        lines.append(self.c("title") + _pad(_clip(f" {self.project_name} · 设置 · {self.model_name}", w), w) + self.RESET)
        # 标签栏
        tab_parts = []
        for i, name in enumerate(tabs):
            if i == tab_i:
                tab_parts.append(self.C_HL + f" {name} " + self.RESET)
            else:
                tab_parts.append(self.c("dim") + f" {name} " + self.RESET)
        tab_line = " ".join(tab_parts) + self.c("dim") + "  Tab切换标签 · ↑↓选项 · ←→切换 · Enter保存 · Esc放弃" + self.RESET
        lines.append(_clip_keep_ansi(tab_line, w))
        lines.append(self._rule(w))

        fields = self.settings_fields or []
        list_h = max(8, h - 8)
        if self.settings_index < self._settings_scroll:
            self._settings_scroll = self.settings_index
        if self.settings_index >= self._settings_scroll + list_h:
            self._settings_scroll = self.settings_index - list_h + 1
        self._settings_scroll = max(0, min(self._settings_scroll, max(0, len(fields) - list_h)))

        edit = getattr(self, "settings_edit", None)
        body = []
        if not fields:
            body.append(self.c("dim") + "  （本页无配置项）" + self.RESET)
        else:
            visible = fields[self._settings_scroll : self._settings_scroll + list_h]
            for offset, field in enumerate(visible):
                i = self._settings_scroll + offset
                editing = edit is not None and i == self.settings_index
                label = field.get("label", "")
                ftype = field.get("type", "text")
                value = self.settings_scratch.get(field["key"], field.get("current"))
                if ftype == "plugin":
                    # 树状插件行：展开箭头 + 开关状态；可展开项附 Enter 提示
                    state = "开" if field.get("current") is True else "关"
                    arrow = "▾" if field.get("expanded") else ("▸" if field.get("expandable") else "·")
                    extra = " · Enter配置" if field.get("expandable") else ""
                    display = _clip(f"{arrow} {label:20} < {state} > ←→{extra}", w - 4)
                elif ftype == "pbool":
                    # 勾选框模式（缩进于插件行下）
                    mark = "[x]" if field.get("current") in (True, "true", "1", 1) else "[ ]"
                    display = _clip(f"    {mark} {label}", w - 4)
                elif ftype == "pchoice":
                    show = "" if field.get("current") is None else str(field.get("current"))
                    display = _clip(f"    {label:18} < {show} > ←→", w - 4)
                elif ftype == "ptext":
                    show = self._edit_caret_text(edit) if editing else ("" if field.get("current") is None else str(field.get("current")))
                    display = _clip(f"    {label:18} {show}", w - 4)
                elif ftype == "choice":
                    show = "" if value is None else str(value)
                    hint_lr = " ←→"
                    display = _clip(f"{label:20}  < {show} >{hint_lr}", w - 4)
                elif ftype == "bool":
                    show = "开" if value in (True, "true", "1", 1) else "关"
                    display = _clip(f"{label:20}  < {show} > ←→", w - 4)
                elif ftype == "action":
                    display = _clip(f"{label:20}  [ Enter 执行 ]", w - 4)
                elif ftype == "sep":
                    display = _clip(f"  {label}", w - 4)
                elif editing:
                    # 正在编辑：字段行内直接显示缓冲与光标（像输入框一样就地输入）
                    display = _clip(f"{label:20}  {self._edit_caret_text(edit)}", w - 4)
                else:
                    show = "" if value is None else str(value)
                    marker = "▌" if i == self.settings_index else ""
                    display = _clip(f"{label:20}  {show}{marker}", w - 4)
                if field.get("type") == "sep":
                    body.append(self.c("dim") + _pad(display, w) + self.RESET)
                    continue
                prefix = f"▸ {display}" if i == self.settings_index else f"  {display}"
                if i == self.settings_index:
                    body.append(self.C_HL + _pad(prefix, w) + self.RESET)
                else:
                    body.append(self.c("ink") + prefix + self.RESET)
        body.extend([""] * (list_h - len(body)))
        lines.extend(body[:list_h])
        lines.append(self._rule(w))
        if getattr(self, "settings_edit", None) is not None:
            lines.append(self._compose_edit_line(w))
        elif getattr(self, "settings_notice", ""):
            lines.append(self.c("ok") + _clip(" " + self.settings_notice, w) + self.RESET)
        else:
            cur = fields[self.settings_index] if 0 <= self.settings_index < len(fields) else {}
            hint = f" {cur.get('label','')} · {cur.get('hint','')} · type={cur.get('type','text')}"
            lines.append(self.c("dim") + _clip(hint, w) + self.RESET)
        lines.append(self._rule(w))
        lines.append(self.c("accent") + _pad(_clip(f"→{self.project_name} · {self.model_name} · {policy.MODE_LABELS.get(self.mode, self.mode)}", w), w) + self.RESET)
        while len(lines) < h:
            lines.append(" ")
        return lines[:h]

    # ----- 设置：三标签页 -----
    settings_tabs = ["提供商", "模型", "系统", "快捷键", "插件"]

    # 快捷键可选值（设置页 ←→）
    SHORTCUT_OPTIONS = {
        "switch_mode": [
            "shift+tab",
            "f2",
            "f3",
            "ctrl+g",
            "ctrl+t",
            "ctrl+e",
            "`",
            "~",
        ],
        "switch_focus": ["tab", "f4", "ctrl+o"],
        "send": ["ctrl+enter", "f5", "ctrl+s"],
        "newline": ["shift+enter", "enter"],
        "complete": ["tab"],
        "scroll_up": ["pageup", "ctrl+up"],
        "scroll_down": ["pagedown", "ctrl+down"],
        "expand": ["right", "l"],
        "collapse": ["left", "h"],
        "rollback": ["ctrl+z"],
    }

    def _settings_model_names(self, config) -> List[str]:
        names = config.list_model_names() or [config.model_name]
        return names

    @staticmethod
    def _settings_workflow_names(config) -> List[str]:
        """已识别的工作流列表（data/workflows/*.json），至少含当前激活项"""
        from core import workflow as workflow_mod

        names = workflow_mod.list_workflows(config)
        active = workflow_mod.active_name(config)
        if active not in names:
            names.append(active)
        return names or ["default"]

    @staticmethod
    def _settings_workflow_current(config) -> str:
        from core import workflow as workflow_mod

        return workflow_mod.active_name(config)

    # 焦点离开时才重建模型列表的文本字段
    _MODEL_LIST_TEXT_KEYS = frozenset({"provider_id", "model_id"})

    def _settings_snapshot(self, config) -> dict:
        s = self.settings_scratch
        return {
            "active_provider_id": s.get("active_provider_id", config.data.get("active_provider_id")),
            "provider_id": s.get("provider_id", self.settings_provider_id),
            "provider_name": s.get("provider_name"),
            "api_key": s.get("api_key"),
            "base_url": s.get("base_url"),
            "balance_url": s.get("balance_url"),
            "provider_temperature": s.get("provider_temperature"),
            "default_model_id": s.get("default_model_id"),
            "model_id": s.get("model_id"),
            "theme": s.get("theme", getattr(config, "theme", "")),
            "mode": s.get("mode", getattr(config, "mode", "")),
            "log_level": s.get("log_level"),
            "select_model": s.get("select_model"),
            "task_plan": s.get("task_plan"),
            "task_code": s.get("task_code"),
            "task_review": s.get("task_review"),
            "switch_mode": s.get("key_switch_mode"),
        }

    def _settings_is_dirty(self, config) -> bool:
        if not self._settings_baseline:
            return False
        now = self._settings_snapshot(config)
        for key, old in self._settings_baseline.items():
            new = now.get(key)
            if new is None and old is None:
                continue
            if str(new if new is not None else "") != str(old if old is not None else ""):
                return True
        return False

    def _settings_apply_active_provider(self, config) -> str:
        """将「激活提供商」写入配置并生效"""
        s = self.settings_scratch
        active = str(s.get("active_provider_id") or "").strip()
        if not active:
            chosen = str(s.get("switch_provider") or "").strip()
            if chosen and chosen != "(新建)":
                active = chosen
        if not active:
            active = str(s.get("provider_id") or self.settings_provider_id or "").strip()
        if active and config.find_provider(active):
            config.data["active_provider_id"] = active
            s["active_provider_id"] = active
            config.apply_active()
            return active
        return ""

    def _immediate_settings_refresh(self, config) -> None:
        """立刻重建（切标签/保存/切换提供商等）"""
        self._build_settings_fields(config)

    def _build_settings_fields(self, config) -> None:
        """按当前标签生成字段列表"""
        tab = self.settings_tabs[self.settings_tab]
        fields: List[dict] = []
        scratch_keep = dict(self.settings_scratch)
        if tab == "提供商":
            providers = config.providers() or [{}]
            pids = [p.get("provider_id", "") for p in providers]
            pid = self.settings_provider_id or config.data.get("active_provider_id") or (pids[0] if pids else "")
            if pid not in pids and pids:
                pid = pids[0]
            self.settings_provider_id = pid
            provider = config.find_provider(pid) or {}
            # 实时模型列表 = 配置中已有 + 本页/模型页工作副本
            live_models = self._provider_live_models(config, pid)
            live_mids = [m.get("model_id") for m in live_models]
            model_list = ",".join(live_mids) if live_mids else "（暂无，请在模型页添加后保存）"
            default_id = str(
                scratch_keep.get("default_model_id")
                or provider.get("default_model_id")
                or ""
            )
            # 默认模型随实时列表自动对齐
            if live_mids and default_id not in live_mids:
                default_id = live_mids[0]
            if not live_mids:
                default_id = ""
            fields = [
                # —— 切换与激活（置顶，与下方配置隔离）——
                {
                    "key": "switch_provider",
                    "label": "切换提供商",
                    "type": "choice",
                    "options": pids + ["(新建)"],
                    "current": pid if pid in pids else (pid or "(新建)"),
                    "hint": "←→ 选择要编辑的提供商",
                },
                {
                    "key": "active_provider_id",
                    "label": "激活提供商",
                    "type": "choice",
                    "options": pids or ["-"],
                    "current": config.data.get("active_provider_id", pid),
                    "hint": "←→ 运行时使用的提供商",
                },
                {"key": "_sep_switch", "label": "——————————————", "type": "sep", "hint": "以下为提供商配置"},
                # —— 提供商配置 ——
                {
                    "key": "provider_id",
                    "label": "提供商ID",
                    "type": "text",
                    "current": scratch_keep.get("provider_id", provider.get("provider_id", pid)),
                    "hint": "不存在则创建；中文 IME 提交后才会写入",
                },
                {
                    "key": "provider_name",
                    "label": "显示名称",
                    "type": "text",
                    "current": scratch_keep.get("provider_name", provider.get("name", pid)),
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "text",
                    "current": scratch_keep.get("api_key", provider.get("api_key", "")),
                    "hint": "保存时检测是否更新",
                },
                {
                    "key": "base_url",
                    "label": "Base URL",
                    "type": "text",
                    "current": scratch_keep.get("base_url", provider.get("base_url", "")),
                    "hint": "保存时检测是否更改",
                },
                {
                    "key": "balance_url",
                    "label": "余额URL",
                    "type": "text",
                    "current": scratch_keep.get("balance_url", provider.get("balance_url", "")),
                },
                {
                    "key": "provider_temperature",
                    "label": "默认温度",
                    "type": "text",
                    "current": scratch_keep.get("provider_temperature", provider.get("temperature", 1.0)),
                },
                {
                    "key": "default_model_id",
                    "label": "默认模型",
                    "type": "choice",
                    "options": live_mids or ["（无模型）"],
                    "current": default_id or (live_mids[0] if live_mids else "（无模型）"),
                    "hint": "←→ 按实时模型列表选择",
                },
                {
                    "key": "provider_models_list",
                    "label": "实时模型列表",
                    "type": "text",
                    "current": model_list,
                    "hint": "只读 · 与模型页工作列表同步",
                },
                {"key": "_sep_cfg", "label": "——————————————", "type": "sep", "hint": ""},
                {
                    "key": "save_provider",
                    "label": "保存提供商",
                    "type": "action",
                    "hint": "Enter：id 存在则 diff 模型并更新字段，否则创建",
                },
            ]
        elif tab == "模型":
            names = self._settings_model_names(config)
            active = config.model_name
            current_model = self.settings_model_name or active
            if current_model not in names:
                current_model = active if active in names else (names[0] if names else "")
            self.settings_model_name = current_model
            row = config.find_model(current_model) or {}
            pids = [p.get("provider_id", "") for p in config.providers()] or ["-"]
            edit_pid = row.get("provider_id") or self.settings_provider_id or config.data.get("active_provider_id")
            live = self._provider_live_models(config, edit_pid)
            live_names = [make_model_name(edit_pid, m.get("model_id", "")) for m in live]
            if live_names and current_model not in live_names:
                current_model = live_names[0]
                self.settings_model_name = current_model
                row = config.find_model(current_model) or {}
            modalities = row.get("modalities") or ["text"]
            mod_joined = ",".join(modalities)
            mod_choices = [
                "text",
                "text,vision",
                "text,vision,audio",
                "text,audio",
                "text,embedding",
            ]
            if mod_joined not in mod_choices:
                mod_choices = [mod_joined] + mod_choices
            task = config.task_models()
            model_options = names or live_names or ["-"]
            fields = [
                {"key": "select_provider", "label": "提供商", "type": "choice", "options": pids, "current": edit_pid, "hint": "←→ 选择"},
                {"key": "select_model", "label": "模型", "type": "choice", "options": live_names or model_options, "current": current_model, "hint": "实时列表"},
                {"key": "model_id", "label": "模型ID", "type": "text", "current": row.get("model_id", "")},
                {"key": "modalities", "label": "支持模态", "type": "choice", "options": mod_choices, "current": mod_joined, "hint": "←→ 预置组合"},
                {"key": "context_window", "label": "最大上下文", "type": "text", "current": row.get("context_window", 0)},
                {"key": "max_tokens", "label": "最大输出token", "type": "text", "current": row.get("max_tokens", 0)},
                {"key": "model_temperature", "label": "温度", "type": "text", "current": row.get("temperature", 1.0)},
                {"key": "thinking_effort", "label": "思考强度", "type": "choice", "options": THINKING_OPTIONS, "current": row.get("thinking_effort", "none"), "hint": "←→"},
                {"key": "task_plan", "label": "规划模型", "type": "choice", "options": names or model_options, "current": task.get("plan", active), "hint": "任务规划默认模型"},
                {"key": "task_code", "label": "编写模型", "type": "choice", "options": names or model_options, "current": task.get("code", active), "hint": "代码编写默认模型"},
                {"key": "task_review", "label": "审查模型", "type": "choice", "options": names or model_options, "current": task.get("review", active), "hint": "代码审查默认模型"},
                {"key": "add_provider", "label": "添加提供商", "type": "action", "hint": "Enter 后输入 provider_id"},
                {"key": "add_model", "label": "添加模型到提供商", "type": "action", "hint": "写入工作列表，保存提供商时 diff 生效"},
            ]
        elif tab == "快捷键":
            key_defs = [
                ("switch_mode", "切换模式", "Shift+Tab 始终有效；此为额外映射"),
                ("switch_focus", "切换焦点", "会话树 ↔ 输入框"),
                ("send", "发送", "Enter 恒提交；此为额外发送键"),
                ("newline", "换行", "默认 Shift+Enter；Enter 不作换行"),
                ("complete", "命令补全", "有候选时优先补全"),
                ("expand", "树展开", "焦点在会话树时"),
                ("collapse", "树折叠", "焦点在会话树时"),
                ("rollback", "树回退", "会话树 Ctrl+Z 回退到选中节点；再按取消"),
                ("scroll_up", "滚动上", "会话历史向上"),
                ("scroll_down", "滚动下", "会话历史向下"),
            ]
            fields = []
            for key, label, hint in key_defs:
                options = list(self.SHORTCUT_OPTIONS.get(key) or [self.keys.get(key, "")])
                current = str(self.keys.get(key) or options[0]).lower()
                if current not in options:
                    options = [current] + options
                fields.append(
                    {
                        "key": f"key_{key}",
                        "label": label,
                        "type": "choice",
                        "options": options,
                        "current": current,
                        "hint": hint + " · ←→ 选择",
                        "shortcut": key,
                    }
                )
        elif tab == "插件":
            # 插件列表 + 声明式配置（树状缩进）：插件行 ←→ 启停（写盘+重载）、
            # Enter 展开/收起；配置行按声明类型渲染（bool 勾选框 / list 左右切换 /
            # str·int·float 行编辑），所有改动即时写透 config.plugins_config
            from core import plugins as plugins_mod

            rows = plugins_mod.statuses()
            fields = []
            if not rows:
                fields.append(
                    {
                        "key": "_plugin_none",
                        "label": "（未发现插件 · 放置 data/plugins/<id>/ 后 /plugin reload）",
                        "type": "sep",
                    }
                )
            else:
                expanded_ids = getattr(self, "_plugin_expanded", set())
                ftype_map = {"bool": "pbool", "list": "pchoice", "str": "ptext", "int": "ptext", "float": "ptext"}
                for row in rows:
                    pid = row["id"]
                    decls = plugins_mod.declared_configs(pid)
                    expanded = pid in expanded_ids
                    fields.append(
                        {
                            "key": f"plugin_{pid}",
                            "label": f"{row['name']} ({pid})",
                            "type": "plugin",
                            "pid": pid,
                            "status": row["status"],
                            "current": row["status"] == "loaded",
                            "expandable": bool(decls),
                            "expanded": expanded,
                            "hint": row.get("error") or row.get("description") or "←→ 开/关 · Enter 展开/收起配置",
                        }
                    )
                    if not expanded:
                        continue
                    for d in decls:
                        fields.append(
                            {
                                "key": f"pcfg_{pid}__{d['key']}",
                                "label": d.get("label") or d["key"],
                                "type": ftype_map[d["type"]],
                                "pid": pid,
                                "ckey": d["key"],
                                "decl": d,
                                "options": d.get("options"),
                                "current": plugins_mod.get_setting(pid, d["key"], d.get("default"), config),
                                "hint": d.get("hint")
                                or {"list": "←→ 切换选项", "bool": "Enter/←→ 勾选"}.get(d["type"], "Enter 编辑"),
                            }
                        )
        else:
            ui_cfg = (config.data or {}).get("ui") or {}
            mem_cfg = (config.data or {}).get("memory") or {}
            llm_cfg = (config.data or {}).get("llm") or {}
            ctx_cfg = (config.data or {}).get("context") or {}
            wf_cfg = (config.data or {}).get("workflow") or {}
            tools_cfg = (config.data or {}).get("tools") or {}
            log_level = str((config.data or {}).get("log", {}).get("level") or "info").strip().lower()
            if log_level not in ("debug", "info", "warn", "error", "off"):
                log_level = "info"
            themes = ["dark"] + [t for t in config.list_themes() if t != "dark"]
            fields = [
                {"key": "theme", "label": "主题", "type": "choice", "options": themes, "current": config.theme, "hint": "←→ 切换主题"},
                {"key": "mode", "label": "访问模式", "type": "choice", "options": list(policy.MODES), "current": config.mode, "hint": "auto/manual/full"},
                {"key": "busy_send_mode", "label": "等待时新消息", "type": "choice", "options": ["queue", "interrupt"], "current": str(ui_cfg.get("busy_send_mode") or "queue"), "hint": "queue=排队等本轮结束 · interrupt=中断插入"},
                {"key": "logo", "label": "会话区Logo", "type": "bool", "current": bool(ui_cfg.get("logo", True)), "hint": "←→ 开/关"},
                {"key": "font_size", "label": "字体大小", "type": "text", "current": getattr(config, "font_size", 16)},
                {"key": "tip_interval", "label": "提示间隔秒", "type": "text", "current": ui_cfg.get("tip_interval", 5)},
                {"key": "retry_times", "label": "LLM重试次数", "type": "text", "current": llm_cfg.get("retry_times", 3)},
                {"key": "retry_delay", "label": "LLM重试延迟", "type": "text", "current": llm_cfg.get("retry_delay", 1.0)},
                {"key": "scroll_v0", "label": "滚动v0", "type": "text", "current": ui_cfg.get("scroll_v0", 1.0)},
                {"key": "scroll_hold_ms", "label": "长按阈值ms", "type": "text", "current": ui_cfg.get("scroll_hold_ms", 150)},
                {"key": "scroll_max_step", "label": "滚动最大步长", "type": "text", "current": ui_cfg.get("scroll_max_step", 20)},
                {"key": "auto_compress", "label": "自动压缩上下文", "type": "bool", "current": bool(mem_cfg.get("auto_compress", True)), "hint": "←→"},
                {"key": "compress_threshold", "label": "压缩阈值", "type": "text", "current": mem_cfg.get("compress_threshold", 0.8)},
                {"key": "compress_keep_recent_tokens", "label": "压缩保留尾段token", "type": "text", "current": mem_cfg.get("compress_keep_recent_tokens", 16384)},
                {"key": "compress_summary_max_tokens", "label": "摘要上限token", "type": "text", "current": mem_cfg.get("compress_summary_max_tokens", 1024)},
                {"key": "tool_whitelist", "label": "工具白名单", "type": "text", "current": ", ".join(str(x) for x in (ctx_cfg.get("tool_whitelist") or [])), "hint": "逗号分隔 · 白名单工具结果跨回合保留"},
                {"key": "max_tool_rounds", "label": "最大工具轮数", "type": "text", "current": wf_cfg.get("max_rounds", 0), "hint": "0=用工作流文件值 · 超限后仍有进展会自动续期"},
                {"key": "spin_kill_count", "label": "空转计数上限", "type": "text", "current": wf_cfg.get("spin_kill_count", 12), "hint": "重复输出计满即终止回合；未满期间续期不限次"},
                {"key": "max_tool_timeout", "label": "命令超时上限秒", "type": "text", "current": tools_cfg.get("max_timeout", 600), "hint": "execute_command/run_program 传入超时的钳制上限（1-∞）"},
                {"key": "ask_user_timeout", "label": "询问自动超时", "type": "bool", "current": bool(tools_cfg.get("ask_user_timeout", True)), "hint": "←→ 开/关 · 开启后询问框 5 分钟未选择自动跳过"},
                {"key": "workflow_enabled", "label": "启用工作流", "type": "bool", "current": bool(wf_cfg.get("enabled", False)), "hint": "←→ 关=直接对话 · 开=按工作流节点链运行（下一回合生效）"},
                {"key": "workflow_active", "label": "工作流", "type": "choice", "options": self._settings_workflow_names(config), "current": self._settings_workflow_current(config), "hint": "←→ 选择处理管线（仅在启用工作流时生效）"},
                {"key": "log_level", "label": "日志等级", "type": "choice", "options": ["debug", "info", "warn", "error", "关闭"], "current": "关闭" if log_level == "off" else log_level, "hint": "←→ 关闭=不记录任何日志（含写盘）"},
                {"key": "active_model_name", "label": "全局默认模型", "type": "choice", "options": self._settings_model_names(config), "current": config.model_name, "hint": "←→ 切换当前模型"},
            ]
        self.settings_fields = fields
        # 初始化 scratch：保留同 key 已编辑值（插件页字段写透持久化，不经 scratch）
        for field in fields:
            if field.get("type") in ("plugin", "pbool", "pchoice", "ptext"):
                scratch_keep.pop(field["key"], None)
                continue
            if field["key"] not in scratch_keep:
                scratch_keep[field["key"]] = field.get("current")
            # choice 新 options 时校正
            if field.get("type") in ("choice", "bool") and field.get("options"):
                val = scratch_keep[field["key"]]
                if val not in field["options"] and field.get("type") == "choice":
                    scratch_keep[field["key"]] = field.get("current")
        self.settings_scratch = scratch_keep
        if self.settings_index >= len(fields):
            self.settings_index = 0

    def show_settings_form(self, config) -> dict:
        """设置页：↑↓ 移动 · ←→ 修改选项 · Tab 切标签 · Enter 保存并退出 · Esc 放弃"""
        self.settings_mode = True
        self.settings_tab = 0
        self.settings_index = 0
        self._settings_scroll = 0
        self.settings_scratch = {}
        self._plugin_expanded = set()
        self.settings_provider_id = config.data.get("active_provider_id", "")
        self.settings_model_name = config.model_name
        self.settings_notice = "↑↓ 选择 · ←→ 修改 · Enter保存退出 · Esc放弃"
        self.settings_model_work = getattr(self, "settings_model_work", {}) or {}
        self.settings_edit = None
        self._build_settings_fields(config)
        self._settings_baseline = self._settings_snapshot(config)
        self._settings_esc_stage = 0
        try:
            _flush_input()
        except Exception:
            pass
        self.render()
        while True:
            key = _read_key()
            if key is None:
                key = ("tick", "")
            kind, value = key
            if kind == "tick":
                continue
            # 行内编辑态：按键全部交给编辑器（走 pt 管线，中文 IME 上屏可用）
            if self.settings_edit is not None:
                self._settings_edit_key(kind, value, config)
                self.render()
                continue
            if kind == "interrupt":
                if self._settings_is_dirty(config) and self._settings_esc_stage == 0:
                    self._settings_esc_stage = 1
                    self.settings_notice = "有未保存更改 · Enter保存退出 · Esc放弃"
                    self.render()
                    continue
                self.settings_mode = False
                return {}
            if kind == "escape":
                if self._settings_esc_stage == 1:
                    self.settings_mode = False
                    return {}
                if self._settings_is_dirty(config):
                    self._settings_esc_stage = 1
                    self.settings_notice = "有未保存更改 · Enter保存退出 · Esc放弃 · 其它键继续编辑"
                    self.render()
                    continue
                self.settings_mode = False
                return {}
            if self._settings_esc_stage == 1:
                if kind in ("submit", "submit_ctrl"):
                    updates = self._settings_apply(config)
                    self.settings_mode = False
                    self.bind_config(config)
                    return updates
                self._settings_esc_stage = 0
                self.settings_notice = "↑↓ 选择 · ←→ 修改 · Enter保存退出 · Esc放弃"
                # 继续处理当前键
            if kind == "mode_switch":
                continue
            if self._is_mode_switch(kind, value) and kind != "tab":
                continue
            if kind == "tab":
                self.settings_tab = (self.settings_tab + 1) % len(self.settings_tabs)
                self.settings_index = 0
                self._settings_scroll = 0
                self._immediate_settings_refresh(config)
                self._clamp_settings_index()
                self.render()
                continue
            if kind in ("submit", "submit_ctrl"):
                self._clamp_settings_index()
                field = self.settings_fields[self.settings_index] if self.settings_fields else {}
                ftype = field.get("type")
                # 插件页：Enter 展开/收起插件行；勾选 bool；行编辑 str/int/float
                if ftype == "plugin":
                    self._settings_plugin_expand(field)
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                    self.render()
                    continue
                if ftype == "pbool":
                    self._settings_plugin_toggle_bool(config, field)
                    self._immediate_settings_refresh(config)
                    self.render()
                    continue
                if ftype == "ptext":
                    self._settings_plugin_edit_text(config, field)
                    self.render()
                    continue
                # 执行动作 / 文本编辑 / 开关
                if ftype == "action":
                    if field.get("key") == "save_provider":
                        msg = self._settings_save_provider(config)
                        self.settings_notice = msg
                        self._immediate_settings_refresh(config)
                        self._clamp_settings_index()
                        self.render()
                        continue
                    self._settings_run_action(config, field.get("key"))
                    self.render()
                    continue
                if ftype == "text":
                    key_name = field.get("key")
                    label = field.get("label", key_name)
                    cur = self.settings_scratch.get(key_name, field.get("current"))
                    self._settings_begin_edit(
                        kind="text",
                        label=label,
                        initial="" if cur is None else str(cur),
                        key=key_name,
                    )
                    self.render()
                    continue
                if ftype == "bool":
                    cur = self.settings_scratch.get(field["key"], field.get("current"))
                    self.settings_scratch[field["key"]] = not bool(cur in (True, "true", "1", 1))
                    self.render()
                    continue
                # choice / 其它：Enter = 保存并退出设置
                updates = self._settings_apply(config)
                self.settings_mode = False
                self.bind_config(config)
                self.render()
                return updates
            dirn = _key_direction(kind, value)
            if dirn == "up":
                self._settings_move_selection(-1)
                self.render()
            elif dirn == "down":
                self._settings_move_selection(1)
                self.render()
            elif dirn == "left":
                if self._settings_plugin_adjust(config, -1):
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                else:
                    self._settings_cycle_choice(-1, config)
                self.render()
            elif dirn == "right":
                if self._settings_plugin_adjust(config, 1):
                    self._immediate_settings_refresh(config)
                    self._clamp_settings_index()
                else:
                    self._settings_cycle_choice(1, config)
                self.render()
            else:
                # 文本项可直接键入进入行内编辑（与字段尾部 ▌ 提示一致），
                # 也可先 Enter 再输入；其余按键仅重绘
                self._settings_type_to_edit(kind, value, config)
                self.render()

    def _clamp_settings_index(self) -> None:
        n = len(self.settings_fields or [])
        if n <= 0:
            self.settings_index = 0
            return
        if self.settings_index < 0:
            self.settings_index = 0
        if self.settings_index >= n:
            self.settings_index = n - 1

    def _settings_move_selection(self, direction: int) -> None:
        """↑↓ 移动选中项，跳过 sep 分隔行"""
        fields = self.settings_fields or []
        if not fields:
            self.settings_index = 0
            return
        n = len(fields)
        idx = self.settings_index
        for _ in range(n + 1):
            idx += direction
            if idx < 0:
                idx = 0
                break
            if idx >= n:
                idx = n - 1
                break
            if fields[idx].get("type") != "sep":
                break
        self.settings_index = max(0, min(n - 1, idx))

    def _settings_cycle_choice(self, direction: int, config) -> None:
        fields = self.settings_fields or []
        if not fields:
            return
        self._clamp_settings_index()
        idx = max(0, min(len(fields) - 1, self.settings_index))
        field = fields[idx]
        if not isinstance(field, dict) or "key" not in field:
            return
        ftype = field.get("type")
        key = field["key"]
        if ftype == "bool":
            cur = self.settings_scratch.get(key, field.get("current"))
            self.settings_scratch[key] = not bool(cur in (True, "true", "1", 1))
            return
        if ftype != "choice":
            return
        if field.get("type") == "sep":
            return
        options = list(field.get("options") or [])
        if not options:
            return
        cur = self.settings_scratch.get(key, field.get("current"))
        idx = options.index(cur) if cur in options else 0
        self.settings_scratch[key] = options[(idx + direction) % len(options)]
        # 选择提供商/模型后重建字段
        if key == "switch_provider":
            # 切换编辑目标提供商；“(新建)” 清空表单
            chosen = str(self.settings_scratch.get("switch_provider") or "")
            if chosen and chosen != "(新建)":
                self.settings_provider_id = chosen
                provider = config.find_provider(chosen) or {}
                s = self.settings_scratch
                s["provider_id"] = provider.get("provider_id", chosen)
                s["provider_name"] = provider.get("name", chosen)
                s["api_key"] = provider.get("api_key", "")
                s["base_url"] = provider.get("base_url", "")
                s["balance_url"] = provider.get("balance_url", "")
                s["provider_temperature"] = provider.get("temperature", 1.0)
                s["default_model_id"] = provider.get("default_model_id", "")
                s["provider_models_list"] = ",".join(
                    m.get("model_id") for m in provider.get("models") or []
                )
            elif chosen == "(新建)":
                self.settings_provider_id = ""
                s = self.settings_scratch
                s["provider_id"] = ""
                s["provider_name"] = ""
                s["api_key"] = ""
                s["base_url"] = "https://api.openai.com/v1"
                s["balance_url"] = ""
                s["provider_temperature"] = 1.0
                s["default_model_id"] = ""
                s["provider_models_list"] = "（暂无）"
            # 切换提供商属于明确操作，立即刷新列表
            self._immediate_settings_refresh(config)
        elif key == "select_provider":
            pids_models = []
            for row in config.list_models():
                if row["provider_id"] == self.settings_scratch[key]:
                    pids_models.append(row["model_name"])
            if pids_models:
                self.settings_scratch["select_model"] = pids_models[0]
                self.settings_model_name = pids_models[0]
            # 下拉切换提供商：立即刷新模型列表（非逐字输入）
            self._immediate_settings_refresh(config)
        elif key == "select_model":
            self.settings_model_name = self.settings_scratch[key]
            self._immediate_settings_refresh(config)
        elif key == "theme":
            self.load_theme(str(self.settings_scratch[key]))
        elif key == "mode":
            self.mode = str(self.settings_scratch[key])
            # 即时同步策略源（config.mode 读 data.ui.mode）；落盘随设置页"保存"完成
            if self.config is not None:
                self.config.mode = str(self.settings_scratch[key])

    def _provider_live_models(self, config, pid: str) -> List[dict]:
        """提供商实时模型列表：config + 工作副本（模型页增改）"""
        by_id = {}
        provider = config.find_provider(pid) if pid else None
        if provider:
            for m in provider.get("models") or []:
                if m.get("model_id"):
                    by_id[m["model_id"]] = dict(m)
        for m in self.settings_model_work.get(pid, []) or []:
            mid = m.get("model_id")
            if mid:
                by_id[mid] = dict(m)
        return list(by_id.values())

    def _settings_save_provider(self, config) -> str:
        """提供商独立保存：diff 模型列表（无「附带新增」字段）"""
        s = self.settings_scratch
        pid = str(s.get("provider_id") or self.settings_provider_id or "").strip()
        if not pid:
            msg = "提供商ID为空，无法保存"
            self.settings_notice = msg
            return msg
        live_models = self._provider_live_models(config, pid)
        form = {
            "provider_id": pid,
            "name": s.get("provider_name") or pid,
            "api_key": s.get("api_key", ""),
            "base_url": s.get("base_url", ""),
            "balance_url": s.get("balance_url", ""),
            "temperature": s.get("provider_temperature", 1.0),
            "default_model_id": s.get("default_model_id", ""),
        }
        if form["default_model_id"] in ("（无模型）", "-", ""):
            form["default_model_id"] = live_models[0].get("model_id", "") if live_models else ""
        # 与保存前列表 diff
        result = config.save_provider_from_form(form, models_after=live_models)
        # 激活提供商与编辑目标一并写入
        self._settings_apply_active_provider(config)
        if str(s.get("active_provider_id") or ""):
            config.data["active_provider_id"] = str(s["active_provider_id"])
        config.apply_active()
        config.save()
        self.settings_provider_id = pid
        self.settings_model_work.pop(pid, None)
        provider = config.find_provider(pid) or {}
        s["provider_id"] = pid
        s["provider_name"] = provider.get("name", form["name"])
        s["api_key"] = provider.get("api_key", form["api_key"])
        s["base_url"] = provider.get("base_url", form["base_url"])
        s["balance_url"] = provider.get("balance_url", form["balance_url"])
        s["provider_temperature"] = provider.get("temperature", form["temperature"])
        live_ids = [m.get("model_id") for m in self._provider_live_models(config, pid)]
        s["default_model_id"] = provider.get("default_model_id") or (live_ids[0] if live_ids else "")
        s["provider_models_list"] = ",".join(live_ids) if live_ids else "（暂无）"
        s["active_provider_id"] = config.data.get("active_provider_id", s.get("active_provider_id", pid))
        msg = result.get("message") or "已保存提供商"
        act = s.get("active_provider_id")
        if act:
            msg += f" · 激活提供商:{act}"
        self.settings_notice = msg
        self._settings_baseline = self._settings_snapshot(config)
        return msg

    # ----- 设置：插件标签页（改动即时写透 config.plugins_config，不经 scratch）-----

    def _plugin_current_field(self) -> dict:
        fields = self.settings_fields or []
        if not fields:
            return {}
        self._clamp_settings_index()
        return fields[self.settings_index]

    def _settings_plugin_adjust(self, config, direction: int) -> bool:
        """←→：插件行=启停（写盘+重载）、pbool=勾选、pchoice=切换选项。
        处理了插件类字段返回 True（调用方跳过通用 choice 循环并重建字段）。"""
        field = self._plugin_current_field()
        ftype = field.get("type")
        if ftype == "plugin":
            self._settings_plugin_toggle_enable(config, field)
            return True
        if ftype == "pbool":
            self._settings_plugin_toggle_bool(config, field)
            return True
        if ftype == "pchoice":
            self._settings_plugin_cycle_choice(config, field, direction)
            return True
        return False

    def _settings_plugin_toggle_enable(self, config, field) -> None:
        from core import plugins as plugins_mod

        pid = field.get("pid")
        target = field.get("current") is not True
        if not plugins_mod.set_enabled(pid, target, config):
            self.settings_notice = f"插件 {pid} 状态写入失败"
            return
        counts = plugins_mod.reload(config)
        row = next((r for r in plugins_mod.statuses() if r["id"] == pid), {})
        status = row.get("status")
        if target and status != "loaded":
            self.settings_notice = f"插件 {pid} 启用后仍为[{status}]：{row.get('error') or '未知原因'}"
        else:
            self.settings_notice = (
                f"插件 {pid} 已{'启用' if target else '禁用'} · 重载："
                f"装载{counts['loaded']} 禁用{counts['disabled']} 失败{counts['failed']}"
            )

    def _settings_plugin_toggle_bool(self, config, field) -> None:
        from core import plugins as plugins_mod

        pid, key = field.get("pid"), field.get("ckey")
        cur = bool(plugins_mod.get_setting(pid, key, None, config) in (True, "true", "1", 1))
        val = plugins_mod.set_setting(pid, key, not cur, config)
        self.settings_notice = f"{field.get('label', key)} = {'开' if val else '关'}"

    def _settings_plugin_cycle_choice(self, config, field, direction: int) -> None:
        from core import plugins as plugins_mod

        options = [str(o) for o in (field.get("options") or [])]
        if not options:
            return
        pid, key = field.get("pid"), field.get("ckey")
        cur = plugins_mod.get_setting(pid, key, None, config)
        cur_s = "" if cur is None else str(cur)
        idx = options.index(cur_s) if cur_s in options else 0
        nxt = options[(idx + direction) % len(options)]
        plugins_mod.set_setting(pid, key, nxt, config)
        self.settings_notice = f"{field.get('label', key)} = {nxt}"

    def _settings_plugin_expand(self, field) -> None:
        pid = field.get("pid")
        expanded = getattr(self, "_plugin_expanded", set())
        if pid in expanded:
            expanded.discard(pid)
            self.settings_notice = f"已收起 {field.get('label', pid)} 的配置"
        else:
            expanded.add(pid)
            self.settings_notice = f"已展开 {field.get('label', pid)} 的配置"
        self._plugin_expanded = expanded

    def _settings_plugin_edit_text(self, config, field) -> None:
        """str/int/float 配置：Enter 进入行内编辑；提交时按声明类型解析（失败保留原值）"""
        from core import plugins as plugins_mod

        pid, key = field.get("pid"), field.get("ckey")
        label = field.get("label", key)
        cur = plugins_mod.get_setting(pid, key, None, config)
        self._settings_begin_edit(
            kind="plugin",
            label=label,
            initial="" if cur is None else str(cur),
            field=field,
        )

    def _settings_plugin_apply_text(self, config, field, raw: str) -> None:
        """行内编辑提交：按声明类型强制转换并写透；空串视为未修改。"""
        from core import plugins as plugins_mod

        pid, key = field.get("pid"), field.get("ckey")
        decl = field.get("decl") or {}
        label = field.get("label", key)
        cur = plugins_mod.get_setting(pid, key, None, config)
        if raw == "":
            self.settings_notice = f"{label} 未修改"
            return
        ctype = decl.get("type", "str")
        try:
            coerced = int(float(raw)) if ctype == "int" else (float(raw) if ctype == "float" else raw)
        except (TypeError, ValueError):
            self.settings_notice = f"{label} 需要{'整数' if ctype == 'int' else '小数'}，已保留原值 {cur}"
            return
        try:
            val = plugins_mod.set_setting(pid, key, coerced, config)
        except ValueError as e:
            self.settings_notice = str(e)
            return
        self.settings_notice = f"{label} 已保存 = {val}"

    def _settings_run_action(self, config, action: str) -> None:
        """添加提供商/模型：进入行内编辑读入 id（提交在 _settings_edit_commit 完成）"""
        if action == "add_provider":
            self._settings_begin_edit(kind="add_provider", label="新 provider_id", initial="")
        elif action == "add_model":
            pid = (
                self.settings_scratch.get("select_provider")
                or self.settings_provider_id
                or config.data.get("active_provider_id")
            )
            self._settings_begin_edit(
                kind="add_model", label=f"新 model_id ({pid})", initial="", pid=pid
            )

    def _settings_commit_add_provider(self, config, pid: str) -> None:
        pid = pid.strip()
        if not pid:
            self.settings_notice = "provider_id 为空，已取消"
            return
        config.add_or_update_provider(
            {
                "provider_id": pid,
                "name": pid,
                "api_key": "",
                "base_url": "https://api.openai.com/v1",
                "models": [{"model_id": "default", "context_window": 65536, "max_tokens": 8192}],
            }
        )
        self.settings_provider_id = pid
        self.settings_scratch["switch_provider"] = pid
        self.settings_notice = f"提供商 {pid} 已创建 · 保存后生效"

    def _settings_commit_add_model(self, config, pid: str, mid: str) -> None:
        mid = mid.strip()
        if not mid:
            self.settings_notice = "model_id 为空，已取消"
            return
        entry = {
            "model_id": mid,
            "context_window": 65536,
            "max_tokens": 8192,
            "temperature": 1.0,
            "modalities": ["text"],
            "thinking_effort": "none",
        }
        # 写入工作副本，保存提供商时 diff；若提供商已存在也同步 config 便于模型页编辑
        work = self.settings_model_work.setdefault(pid, [])
        work[:] = [m for m in work if m.get("model_id") != mid]
        work.append(entry)
        if config.find_provider(pid):
            config.add_model(pid, entry)
        self.settings_model_name = make_model_name(pid, mid)
        self.settings_scratch["select_model"] = self.settings_model_name
        self.settings_notice = f"模型 {mid} 已加入工作列表，保存提供商时生效"

    def _settings_apply(self, config) -> dict:
        """将三个标签页 scratch 写入 config 并 save"""
        s = self.settings_scratch
        # 提供商
        pid = s.get("provider_id") or self.settings_provider_id or config.data.get("active_provider_id")
        provider = {
            "provider_id": str(pid),
            "name": str(s.get("provider_name") or pid),
            "api_key": str(s.get("api_key", "")),
            "base_url": str(s.get("base_url", "")),
            "balance_url": str(s.get("balance_url", "")),
        }
        try:
            provider["temperature"] = float(s.get("provider_temperature", 1.0))
        except (TypeError, ValueError):
            provider["temperature"] = 1.0
        existing = config.find_provider(str(pid))
        if existing:
            provider["models"] = existing.get("models") or []
            provider["default_model_id"] = s.get("default_model_id") or existing.get("default_model_id")
        config.add_or_update_provider(provider)
        # 激活提供商
        self._settings_apply_active_provider(config)
        config.apply_active()
        # 模型
        select_model = s.get("select_model") or self.settings_model_name
        row = config.find_model(select_model) if select_model else None
        if row:
            model_updates = {
                "model_id": str(s.get("model_id") or row.get("model_id")),
                "modalities": [x.strip() for x in str(s.get("modalities") or "text").split(",") if x.strip()],
                "thinking_effort": str(s.get("thinking_effort") or "none"),
            }
            try:
                model_updates["context_window"] = int(float(s.get("context_window", row.get("context_window", 0))))
                model_updates["max_tokens"] = int(float(s.get("max_tokens", row.get("max_tokens", 0))))
                model_updates["temperature"] = float(s.get("model_temperature", row.get("temperature", 1.0)))
            except (TypeError, ValueError):
                pass
            config.update_model(row["provider_id"], row["model_id"], model_updates)
            config.data["active_provider_id"] = row["provider_id"]
            config.data["active_model_id"] = model_updates.get("model_id", row["model_id"])
        # 任务模型
        if s.get("task_plan"):
            config.set_task_model("plan", s["task_plan"])
        if s.get("task_code"):
            config.set_task_model("code", s["task_code"])
        if s.get("task_review"):
            config.set_task_model("review", s["task_review"])
        # 系统
        updates = {}
        if s.get("active_model_name"):
            config.switch_model(s["active_model_name"])
        ui_cfg = config.data.setdefault("ui", {})
        # 快捷键：scratch 中 key_* 写入 ui 与 self.keys
        for scratch_key, val in s.items():
            if not str(scratch_key).startswith("key_"):
                continue
            shortcut = str(scratch_key)[4:]
            if val:
                ui_cfg[shortcut] = str(val).lower()
                self.keys[shortcut] = str(val).lower()
        if "theme" in s:
            ui_cfg["theme"] = str(s["theme"])
        if "mode" in s:
            ui_cfg["mode"] = str(s["mode"])
        if "busy_send_mode" in s and str(s["busy_send_mode"]) in ("queue", "interrupt"):
            ui_cfg["busy_send_mode"] = str(s["busy_send_mode"])
        if "logo" in s:
            ui_cfg["logo"] = bool(s["logo"] in (True, "true", "1", 1) if isinstance(s["logo"], str) else bool(s["logo"]))
        for key, cast in (
            ("font_size", int),
            ("tip_interval", float),
            ("scroll_v0", float),
            ("scroll_hold_ms", int),
            ("scroll_max_step", int),
        ):
            if key in s and s[key] is not None and str(s[key]) != "":
                try:
                    ui_cfg[key] = cast(float(s[key]))
                except (TypeError, ValueError):
                    pass
        if "font_size" in ui_cfg:
            config.data.setdefault("system", {})["font_size"] = ui_cfg["font_size"]
        llm_cfg = config.data.setdefault("llm", {})
        if "retry_times" in s:
            try:
                llm_cfg["retry_times"] = int(float(s["retry_times"]))
            except (TypeError, ValueError):
                pass
        if "retry_delay" in s:
            try:
                llm_cfg["retry_delay"] = float(s["retry_delay"])
            except (TypeError, ValueError):
                pass
        mem_cfg = config.data.setdefault("memory", {})
        if "auto_compress" in s:
            mem_cfg["auto_compress"] = bool(s["auto_compress"] in (True, "true", "1", 1) if isinstance(s["auto_compress"], str) else bool(s["auto_compress"]))
        if "compress_threshold" in s:
            try:
                mem_cfg["compress_threshold"] = float(s["compress_threshold"])
            except (TypeError, ValueError):
                pass
        if "compress_keep_recent_tokens" in s:
            try:
                mem_cfg["compress_keep_recent_tokens"] = max(0, int(float(s["compress_keep_recent_tokens"])))
            except (TypeError, ValueError):
                pass
        if "compress_summary_max_tokens" in s:
            try:
                mem_cfg["compress_summary_max_tokens"] = max(100, int(float(s["compress_summary_max_tokens"])))
            except (TypeError, ValueError):
                pass
        if "tool_whitelist" in s:
            ctx_cfg = config.data.setdefault("context", {})
            names = [p.strip() for p in str(s["tool_whitelist"] or "").split(",") if p.strip()]
            ctx_cfg["tool_whitelist"] = names
        wf_cfg = config.data.setdefault("workflow", {})
        if "workflow_enabled" in s:
            raw = s["workflow_enabled"]
            wf_cfg["enabled"] = bool(raw in (True, "true", "1", 1) if isinstance(raw, str) else bool(raw))
        if "workflow_active" in s and s["workflow_active"]:
            from core import workflow as workflow_mod

            workflow_mod.set_active(config, str(s["workflow_active"]))
        if "max_tool_rounds" in s:
            try:
                wf_cfg["max_rounds"] = max(0, int(float(s["max_tool_rounds"])))
            except (TypeError, ValueError):
                pass
        if "spin_kill_count" in s:
            try:
                wf_cfg["spin_kill_count"] = max(1, int(float(s["spin_kill_count"])))
            except (TypeError, ValueError):
                pass
        if "max_tool_timeout" in s:
            tools_cfg = config.data.setdefault("tools", {})
            try:
                tools_cfg["max_timeout"] = max(1, int(float(s["max_tool_timeout"])))
            except (TypeError, ValueError):
                pass
        if "ask_user_timeout" in s:
            tools_cfg = config.data.setdefault("tools", {})
            raw = s["ask_user_timeout"]
            tools_cfg["ask_user_timeout"] = bool(raw in (True, "true", "1", 1) if isinstance(raw, str) else bool(raw))
        if "log_level" in s and s["log_level"]:
            level = str(s["log_level"]).strip()
            level = {"关闭": "off"}.get(level, level.lower())
            if level in ("debug", "info", "warn", "error", "off"):
                log_cfg = config.data.setdefault("log", {})
                if log_cfg.get("level") != level:
                    log_cfg["level"] = level
                    init_logging(config.data.get("log") or {})
        config.apply_active()
        config.save()
        updates["model_name"] = config.model_name
        updates["task_models"] = config.task_models()
        return updates

    # ----- 行内编辑（文本字段 / 插件 ptext / 新增 provider·model）-----
    # 走 core.keyinput（prompt_toolkit）逐键读取：中文 IME 上屏以 char 事件到达，
    # 与主输入框同一管线；不再借道内建 input()（raw_mode 下无行缓冲/回显，且泵
    # 线程会抽干控制台输入，中文 IME 无法工作）。

    def _settings_begin_edit(
        self, *, kind: str, label: str, initial: str = "", field=None, key=None, pid=None
    ) -> None:
        buf = TextBuffer(initial)
        buf.set_cursor(len(initial))
        self.settings_edit = {
            "kind": kind,
            "label": label,
            "field": field,
            "key": key,
            "pid": pid,
            "buf": buf,
        }
        self.settings_notice = ""

    # 直接键入即进入编辑的按键（选中文本项后不必先按 Enter）
    _TYPE_TO_EDIT_KINDS = frozenset({"char", "paste", "backspace", "delete"})

    def _settings_type_to_edit(self, kind: str, value: Any, config) -> bool:
        """选中 text/ptext 项时直接键入 → 进入行内编辑并应用该按键。"""
        if kind not in self._TYPE_TO_EDIT_KINDS:
            return False
        fields = self.settings_fields or []
        if not (0 <= self.settings_index < len(fields)):
            return False
        field = fields[self.settings_index]
        ftype = field.get("type")
        if ftype == "text":
            key_name = field.get("key")
            cur = self.settings_scratch.get(key_name, field.get("current"))
            self._settings_begin_edit(
                kind="text",
                label=field.get("label", key_name),
                initial="" if cur is None else str(cur),
                key=key_name,
            )
        elif ftype == "ptext":
            self._settings_plugin_edit_text(config, field)
        else:
            return False
        self._settings_edit_key(kind, value, config)
        return True

    def _settings_edit_key(self, kind: str, value: Any, config) -> None:
        """行内编辑按键：单行；Enter 提交 · Esc 取消 · 方向/Home/End 移动光标。"""
        edit = self.settings_edit
        if edit is None:
            return
        buf: TextBuffer = edit["buf"]
        if kind in ("submit", "submit_ctrl"):
            self._settings_edit_commit(config)
            return
        if kind in ("escape", "interrupt"):
            self.settings_edit = None
            self.settings_notice = "已取消编辑"
            return
        if kind == "backspace":
            buf.backspace()
        elif kind == "delete":
            buf.delete()
        elif kind == "clear":
            buf.set_text("")
        elif kind == "char":
            buf.insert(str(value))
        elif kind == "paste":
            # 单行字段：粘贴内容压平为一行
            flat = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            buf.insert(flat)
        elif kind == "home":
            buf.move_home()
        elif kind == "end":
            buf.move_end()
        else:
            dirn = _key_direction(kind, value)
            if dirn == "left":
                buf.move_left()
            elif dirn == "right":
                buf.move_right()

    def _settings_edit_commit(self, config) -> None:
        """提交行内编辑：按 kind 分派到对应落点，并刷新字段列表。"""
        edit = self.settings_edit
        if edit is None:
            return
        raw = edit["buf"].to_text()
        kind = edit["kind"]
        self.settings_edit = None
        if kind == "text":
            key_name = edit.get("key")
            label = edit["label"]
            if raw != "":
                self.settings_scratch[key_name] = raw
            if key_name in self._MODEL_LIST_TEXT_KEYS:
                self._immediate_settings_refresh(config)
            else:
                self._build_settings_fields(config)
            self.settings_notice = f"{label} 已更新 · Enter 保存退出"
        elif kind == "plugin":
            self._settings_plugin_apply_text(config, edit.get("field") or {}, raw)
            self._immediate_settings_refresh(config)
        elif kind == "add_provider":
            self._settings_commit_add_provider(config, raw)
            self._immediate_settings_refresh(config)
        elif kind == "add_model":
            self._settings_commit_add_model(config, edit.get("pid") or "", raw)
            self._immediate_settings_refresh(config)
        self._clamp_settings_index()

    @staticmethod
    def _edit_caret_text(edit) -> str:
        """编辑缓冲的可视文本：在光标处插入 ▌（edit 为空则返回空串）。"""
        buf = (edit or {}).get("buf")
        if buf is None:
            return ""
        text = buf.to_text()
        cur = buf.cursor
        return f"{text[:cur]}▌{text[cur:]}"

    def _compose_edit_line(self, w: int) -> str:
        edit = self.settings_edit or {}
        label = edit.get("label", "")
        body = f" {label} [{self._edit_caret_text(edit)}]  Enter 提交 · Esc 取消"
        return self.c("warn") + _clip(body, w) + self.RESET
