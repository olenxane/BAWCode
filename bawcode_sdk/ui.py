"""Thread-aware user interaction bridge."""
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import PluginContext


class PluginUI:
    """app.request_ui/wait_ui 交互桥的插件包装：confirm/choose/line 三个用户动作原语。

    仅可在 agent 线程（hook / 工具执行内）调用——桥的应答由主线程帧循环完成；主线程请直接用 app 的阻塞表单（app.read_line / app.show_sessions_form 等，见 /run /resume 的做法）。
    用户取消或运行时未注入（bind_runtime 之前）返回 None，不抛异常。
    """

    _YES = ("y", "yes", "是", "1")

    def __init__(self, ctx: "PluginContext"):
        self._ctx = ctx

    def tree_node(self, id: str, label: str, kind: str = "text", **kwargs):
        """Create a host-compatible tree node without importing terminal UI internals."""
        return self._ctx.host.runtime.tree_node(id, label, kind, **kwargs)

    @property
    def available(self) -> bool:
        """交互通道是否就绪（运行时载体已注入）；False 时面板调用直接返回 None"""
        return self._ctx.runtime.get("app") is not None

    def _auto_cancelled(self, cancelled):
        """cancelled 未显式传入时自动接当前线程回合取消令牌（callable 形态）"""
        if cancelled is not None:
            return cancelled
        ev = self._ctx.cancelled
        return ev.is_set if ev is not None else None

    def _bridge(self, kind: str, payload: dict, cancelled) -> Any:
        CANCELLED = self._ctx.host.runtime.CANCELLED

        app = self._ctx.runtime.get("app")
        if app is None:
            self._ctx.log.warn("ctx.ui.%s 失败：app 未注入（运行时未就绪）", kind)
            return None
        # 走宿主统一请求槽通道：弹窗串行、槽位顶掉检测、主线程防护一应俱全
        result = self._ctx.host.ui.request_wait(app, kind, payload, self._auto_cancelled(cancelled))
        return None if result == CANCELLED else result

    def ask(self, question: str, options=(), timeout: int = 0, multiline: bool = False, cancelled=None) -> Optional[dict]:
        """任务卡片面板：自由作答/选项作答，支持多行输入与超时。

        返回 {status: answer|timeout|rejected, answer?: str}；用户取消（Esc/回合中断/交互通道未就绪）统一返回 None。仅可在 agent/后台线程调用（主线程请直调 app.show_ask_form）。"""
        question = str(question or "").strip()
        if not question:
            return None
        result = self._bridge("ask", {
            "question": question,
            "options": [str(o) for o in (options or [])],
            "timeout": max(0, int(timeout or 0)),
            "multiline": bool(multiline),
        }, cancelled)
        return result if isinstance(result, dict) else None

    def confirm(self, prompt: str = "确认？", cancelled=None) -> Optional[bool]:
        """是/否确认面板。True/False；用户取消返回 None"""
        result = self.choose(["是", "否"], prompt=prompt, cancelled=cancelled)
        if result is None:
            return None
        return str(result).strip().lower() in self._YES

    def choose(self, options, prompt: str = "请选择", cancelled=None) -> Optional[str]:
        """选择菜单：返回所选项文本（按序号/文本匹配）；用户输入自由文本时原样返回；
        取消/未注入返回 None"""
        labels = [str(o) for o in (options or [])]
        if not labels:
            return None
        pairs = [(str(i + 1), label) for i, label in enumerate(labels)]
        result = self._bridge("choose", {"options": pairs, "prompt": prompt}, cancelled)
        if result is None:
            return None
        text = str(result).strip()
        if text.isdigit() and 1 <= int(text) <= len(labels):
            return labels[int(text) - 1]
        for label in labels:
            if text.lower() == label.lower():
                return label
        return text

    def line(self, prompt: str = "请输入", default: str = "", cancelled=None) -> Optional[str]:
        """单行输入面板：返回输入文本（空输入返回 default）；取消/未注入返回 None"""
        result = self._bridge("line", {"prompt": prompt, "config": self._ctx.config}, cancelled)
        if result is None:
            return None
        text = str(result).strip()
        return text or default
