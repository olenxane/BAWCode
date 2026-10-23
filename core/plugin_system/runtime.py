"""插件运行时载体（app/runner）注入与访问"""
from typing import Any, Dict
from core.log import get_logger

log = get_logger("plugins.runtime")
_runtime: Dict[str, Any] = {"app": None, "runner": None}


def bind_runtime(app=None, runner=None) -> None:
    """main 注入运行时载体（app/runner）；不传的项保留原值。

    插件的类用户操作 API（ctx.submit_turn / ctx.notify / ctx.ui）依赖此注入；
    注入前调用这些 API 会得到失败提示/None，不会抛异常。"""
    if app is not None:
        _runtime["app"] = app
    if runner is not None:
        _runtime["runner"] = runner
    log.debug("插件运行时已注入: app=%s runner=%s", "有" if app else "-", "有" if runner else "-")


def runtime() -> Dict[str, Any]:
    """只读访问运行时载体 {app, runner}（bind_runtime 注入的对象引用）。

    插件可读取 app 的实时状态（busy / streaming_msg / status / phase_hint）与
    runner（busy 判定、llm.cancel() 中断在途回合）；未注入时对应值为 None。"""
    return dict(_runtime)


def reset_runtime() -> None:
    _runtime.update(app=None, runner=None)


def interrupt() -> bool:
    fn = getattr(_runtime.get("runner"), "interrupt_turn", None)
    return bool(fn()) if callable(fn) else False


def notify(text: str) -> None:
    from core.memory import get_session
    try:
        session = get_session()
        if session is not None:
            session.add_message("system", text, type="help")
            app = _runtime.get("app")
            if app is not None:
                app.refresh_from_session(session, render=False)
    except Exception as exc:
        log.warn("notify 失败: %s", exc)
