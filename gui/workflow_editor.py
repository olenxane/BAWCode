# -*- coding: utf-8 -*-
"""工作流编辑器（WebUI 版）。

用法: python -m gui.workflow_editor [工作流名] --port N

启动本地 HTTP 服务并自动打开浏览器；页面为 gui/web/workflow_editor.html。
端口缺省系统分配；主程序 /workflow edit 会预探测空闲端口经 --port 传入
（存在竞态，被占时自动回落系统分配）。

交互（与原 PySide6 版对齐，详见页面）：
- 左侧节点面板双击（或拖拽到画布）添加节点
- 悬停节点出现右侧输出端口，从端口拖出连线、落到目标节点松开即建立 A→B；
  连线决定执行顺序（必须构成一条单链）
- 单击选中节点/连线；Delete 键/工具栏删除选中；双击节点聚焦右侧参数面板
- 保存为 data/workflows/<名称>.json（节点按链顺序写出，主程序按此顺序执行）

服务边界：
- 仅监听 127.0.0.1；页面静态放行，全部 /api/* 校验启动时生成的随机密钥
  （URL 携带 ?key=…，防本机其它进程/页面 CSRF 与 DNS rebinding 触碰文件接口）
- 单链校验（compute_chain）纯客户端；服务端保存/校验走 workflow.validate_workflow
  结构校验 + 原子写盘（临时文件 + os.replace，保存中途崩溃不留半截文件）
- 页面每 30s 心跳；超过 180s 无任何请求（关页/浏览器退出）服务自动退出，
  不留僵尸进程

原型边界：仅线性链（无条件分支边/并行）、无撤销、无缩略图。
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))  # 兼容 python gui/workflow_editor.py 直接运行

from core import workflow as workflow_mod  # noqa: E402

PAGE_PATH = Path(__file__).resolve().parent / "web" / "workflow_editor.html"

# 心跳与空闲自毁（与页面 PING_INTERVAL 保持一致；阈值给足重试余量）
PING_INTERVAL = 30
IDLE_EXIT_SECONDS = 180
# 保存/校验请求体上限（工作流 JSON 远小于此，防误传大包）
BODY_MAX_BYTES = 2 * 1024 * 1024


def _list_model_names() -> List[str]:
    """从主配置读取可用模型（model_name = provider_id-model_id）；配置损坏时
    与主程序一致地抛错退出，不静默降级为空列表"""
    from core.config import Config

    return [
        str(r.get("model_name"))
        for r in Config().list_models()
        if r.get("model_name")
    ]


class _EditorState:
    """服务运行时状态：当前工作流名、访问密钥、模型清单、最近请求时间"""

    def __init__(self, name: str, models: List[str]) -> None:
        self.name = name
        self.models = models
        self.key = secrets.token_urlsafe(24)
        self.last_seen = time.monotonic()


class _EditorServer(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 允许绑定"正在被占用"的端口，会使被占回落永远不触发；
    # 关掉后绑定冲突立即抛 OSError → 回落系统分配（本服务端口随机，无固定端口复用需求）
    allow_reuse_address = False


def _make_server(preferred: Optional[int], handler: Any) -> ThreadingHTTPServer:
    """优先绑定 preferred（主进程预探测端口，竞态被占直接回落系统分配），无探测-绑定间隙"""
    last: OSError = OSError("无可用本地端口")
    for port in ([preferred] if preferred else []) + [0]:
        try:
            return _EditorServer(("127.0.0.1", port), handler)
        except OSError as e:
            last = e
    raise last


def _atomic_write_json(path: Path, data: dict) -> None:
    """先写临时文件再替换；tmp 名带线程 id（并发保存不互踩），中途失败不残留 tmp"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _workflow_payload(name: str) -> Dict[str, Any]:
    """读取工作流文件 → {ok, data|errors|message}（缺失/损坏/不合法均不抛出）"""
    path = workflow_mod.workflow_path(None, name)
    if not path.exists():
        return {"ok": False, "message": f"工作流文件不存在: {path.name}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "message": f"加载失败\n{path}\n{e}"}
    errors = workflow_mod.validate_workflow(data)
    if errors:
        return {"ok": False, "message": "加载失败", "errors": errors}
    return {"ok": True, "data": data}


def _validate_payload(data: Any) -> List[str]:
    """结构校验（类型/id/非空），返回错误列表（空=通过）"""
    if not isinstance(data, dict):
        return ["工作流数据必须是对象"]
    return workflow_mod.validate_workflow(data)


def _make_handler(state: "_EditorState"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # 静默：编辑器请求不刷终端
            pass

        # --- 基础响应 ---------------------------------------------------

        def _json(self, code: int, obj: dict, extra_headers: Optional[List[Tuple[str, str]]] = None) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra_headers or ()):
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                pass  # 客户端中途断开（刷新/关页）：响应无处可送，静默丢弃

        def _page(self) -> None:
            try:
                html = PAGE_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                pass  # 客户端中途断开（刷新/关页）：响应无处可送，静默丢弃
            except OSError:
                self._json(500, {"ok": False, "message": "编辑器页面文件缺失: gui/web/workflow_editor.html"})

        def _body(self) -> Optional[dict]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > BODY_MAX_BYTES:
                return None
            try:
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None

        def _authed(self) -> bool:
            """全部 API 强制校验随机密钥（URL ?key= 携带，与 remote-control 同思路）"""
            params = parse_qs(urlparse(self.path).query)
            given = [v for v in params.get("key", []) if v]
            return any(hmac.compare_digest(state.key.encode("utf-8"), g.encode("utf-8")) for g in given)

        def _touch(self) -> None:
            state.last_seen = time.monotonic()  # 任意到达的请求都算存活证据

        # --- 路由 -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802  http.server 命名约定
            path = urlparse(self.path).path
            if path == "/favicon.ico":
                self._json(404, {"ok": False})
                return
            if path in ("/", "/index.html"):
                self._touch()
                # 页面静态放行（地址栏清洗/刷新后 URL 无密钥）：密钥门禁在 JS 启动时
                # 经 /api/state 校验，无效则页内提示；全部 API 仍强制校验
                self._page()
                return
            if not self._authed():
                self._json(403, {"ok": False, "message": "缺少或错误的访问密钥，请从启动命令/主程序打开编辑器"})
                return
            self._touch()
            if path == "/api/state":
                names = workflow_mod.list_workflows(None)
                has_current = bool(state.name) and workflow_mod.workflow_path(None, state.name).exists()
                self._json(200, {
                    "ok": True,
                    "name": state.name,
                    "has_current": has_current,
                    "workflows": names,
                    "models": state.models,
                })
            elif path == "/api/workflow":
                name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0].strip()
                if not name:
                    self._json(400, {"ok": False, "message": "缺少 name 参数"})
                else:
                    self._json(200, _workflow_payload(name))
            elif path == "/api/ping":
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "message": f"未知路径: {path}"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not self._authed():
                self._json(403, {"ok": False, "message": "缺少或错误的访问密钥"})
                return
            self._touch()
            body = self._body()
            if body is None:
                self._json(400, {"ok": False, "message": "请求体不是合法 JSON 或超过大小限制"})
                return
            if path == "/api/save":
                name = str(body.get("name") or "").strip()
                data = body.get("data")
                if not name:
                    self._json(400, {"ok": False, "message": "缺少工作流名称"})
                    return
                if not isinstance(data, dict):
                    self._json(400, {"ok": False, "message": "工作流数据必须是对象"})
                    return
                errors = _validate_payload(data)
                if errors:
                    self._json(400, {"ok": False, "message": "校验未通过", "errors": errors})
                    return
                data["name"] = name  # 文件名与内容 name 保持一致（workflow_path 内做同名净化）
                path_ = workflow_mod.workflow_path(None, name)
                try:
                    _atomic_write_json(path_, data)
                except OSError as e:
                    self._json(500, {"ok": False, "message": f"保存失败: {e}"})
                    return
                self._json(200, {"ok": True, "name": name, "path": str(path_)})
            elif path == "/api/validate":
                errors = _validate_payload(body.get("data"))
                self._json(200, {"ok": not errors, "errors": errors})
            else:
                self._json(404, {"ok": False, "message": f"未知路径: {path}"})

    return Handler


def _watchdog(server: ThreadingHTTPServer, state: _EditorState) -> None:
    """空闲自毁：页面心跳停止（关页/浏览器退出）后延迟退出，不留僵尸进程"""
    while True:
        time.sleep(15)
        if time.monotonic() - state.last_seen > IDLE_EXIT_SECONDS:
            server.shutdown()
            return


def _parse_args(argv: List[str]) -> Tuple[str, Optional[int]]:
    """解析 [工作流名] --port N：位置参数为工作流名（可空），端口一律走 --port 传参
    （主程序 /workflow edit 不带名称时不再有"端口被当工作流名"的歧义）"""
    name = ""
    port_hint: Optional[int] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--port" and i + 1 < len(argv) and argv[i + 1].isdigit():
            port_hint = int(argv[i + 1])
            i += 2
        elif arg.startswith("--"):
            i += 1
        else:
            name = arg.strip()
            i += 1
    return name, port_hint


def main() -> None:
    name, port_hint = _parse_args(sys.argv[1:])
    models = _list_model_names()
    state = _EditorState(name, models)
    server = _make_server(port_hint, _make_handler(state))
    server.daemon_threads = True
    port = int(server.server_address[1])

    url = f"http://127.0.0.1:{port}/?key={state.key}"
    threading.Thread(target=_watchdog, args=(server, state), daemon=True).start()
    print(f"工作流编辑器已就绪: {url}")
    # webbrowser.open 在部分环境可长时间阻塞；挪守护线程，服务就绪不依赖浏览器启动
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("编辑器服务已退出")


if __name__ == "__main__":
    main()
