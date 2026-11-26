"""依赖库自动安装与检查

同步路径（无头模式）沿用 subprocess.run；TUI 走异步作业：
后台线程执行 pip 并把阶段写进共享作业表，底部状态栏读取 progress_text()
展示进度，插件装载由主线程 tick() 在装载完成后补做，避免跨线程注册命令/工具。
"""
import importlib
import importlib.metadata
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from core.log import get_logger

log = get_logger("plugins.dependencies")
try:
    from packaging.requirements import Requirement as _Requirement
except ImportError:
    _Requirement = None

_REQ_FILE = "requirements.txt"
# pip超时时间
_PIP_TIMEOUT = 900
_NO_DEPS: Dict[str, Any] = {"status": "none", "missing": [], "error": ""}

# 异步安装作业：pid -> job。done 的作业由 take_completed() 取走并移除。
_JOBS: Dict[str, dict] = {}
_JOBS_LOCK = threading.RLock()

_PHASE_LABELS = {
    "starting": "准备中",
    "downloading": "下载中",
    "installing": "安装中",
    "verifying": "校验中",
}


def _auto_install_enabled(config) -> bool:
    """config.plugins.auto_install_deps，缺省 true；是否允许自动安装插件依赖"""
    cfg = (getattr(config, "data", None) or {}).get("plugins") or {}
    return bool(cfg.get("auto_install_deps", True))


def _read_requirements(dir_path: Path) -> List[str]:
    """读取 requirements.txt 的有效依赖行；跳过空行与注释，无文件返回 []"""
    req_file = dir_path / _REQ_FILE
    if not req_file.is_file():
        return []
    try:
        text = req_file.read_text(encoding="utf-8-sig")
    except OSError as e:
        log.warn("插件依赖文件读取失败 %s: %s", req_file, e)
        return []
    return [line for line in (raw.strip() for raw in text.splitlines()) if line and not line.startswith("#")]


def _split_requirement(line: str) -> tuple:
    """解析依赖行为 发行名与版本约束；非 PEP 508 行返回 None"""
    if _Requirement is not None:
        try:
            req = _Requirement(line)
        except Exception:
            return None, None
        if req.marker is not None:
            try:
                if not req.marker.evaluate():
                    return None, None
            except Exception:
                return None, None
        return req.name, req.specifier
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
    return (m.group(1), None) if m else (None, None)


def _missing_requirements(reqs: List[str]) -> List[str]:
    """返回当前环境未满足的依赖行，含未安装与版本不符"""
    missing: List[str] = []
    for line in reqs:
        name, spec = _split_requirement(line)
        if not name:
            continue  # 非标准依赖行不参与检查，交由 pip 安装时处理
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(line)
            continue
        except Exception:
            missing.append(line)
            continue
        if spec is not None and not spec.contains(dist.version, prereleases=True):
            missing.append(line)
    return missing


def inspect_requirements(dir_path: Path) -> dict:
    """只检查不安装：返回 {"status":"none"|"ok"|"missing", "missing":[...], "error":""}"""
    if not (dir_path / _REQ_FILE).is_file():
        return dict(_NO_DEPS)
    reqs = _read_requirements(dir_path)
    if not reqs:
        return {"status": "ok", "missing": [], "error": ""}
    missing = _missing_requirements(reqs)
    if not missing:
        return {"status": "ok", "missing": [], "error": ""}
    return {"status": "missing", "missing": missing, "error": ""}


def _pip_install(pid: str, req_file: Path) -> tuple:
    """经当前解释器的 pip 安装插件依赖文件；返回成功标志与输出尾部摘要"""
    cmd = [
        sys.executable, "-m", "pip", "install", "-r", str(req_file),
        "--disable-pip-version-check", "--no-input",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=_PIP_TIMEOUT)
    except FileNotFoundError as e:
        return False, f"无法启动 pip: {e}"
    except subprocess.TimeoutExpired:
        return False, f"pip 安装超时（>{_PIP_TIMEOUT}s）"
    except OSError as e:
        return False, f"pip 执行失败: {e}"
    out = "\n".join(x for x in ((proc.stdout or "") + (proc.stderr or "")).splitlines() if x.strip())
    log.debug("插件 %s pip 输出:\n%s", pid, out or "(空)")
    tail = "\n".join(out.splitlines()[-15:])
    if proc.returncode == 0:
        return True, tail
    return False, tail or f"pip 退出码 {proc.returncode}"


def _ensure_requirements(pid: str, dir_path: Path, config) -> dict:
    """检查并静默补齐插件目录 requirements.txt 声明的依赖（同步，供无头模式）。

    已满足则零开销跳过，只在缺失时调用一次 pip。返回记录：
      {"status": "none"|"ok"|"installed"|"manual"|"failed", "missing": [...], "error": str}
    """
    info = inspect_requirements(dir_path)
    if info["status"] != "missing":
        if info["status"] == "ok":
            log.debug("插件 %s 依赖已满足", pid)
        return info
    missing = info["missing"]
    if not _auto_install_enabled(config):
        log.info("插件 %s 缺依赖 %d 项（已关闭自动安装）: %s", pid, len(missing), ", ".join(missing))
        return {"status": "manual", "missing": missing, "error": ""}
    log.info("插件 %s 缺依赖 %d 项，静默安装: %s", pid, len(missing), ", ".join(missing))
    ok, detail = _pip_install(pid, dir_path / _REQ_FILE)
    if not ok:
        log.warn("插件 %s 依赖安装失败: %s", pid, detail or "未知错误")
        return {"status": "failed", "missing": missing, "error": detail}
    importlib.invalidate_caches()  # 让本轮新装的发行版可见，复核是否真的补齐
    still = _missing_requirements(_read_requirements(dir_path))
    if still:
        log.warn("插件 %s 依赖安装后仍未满足: %s", pid, ", ".join(still))
        return {"status": "failed", "missing": still, "error": f"安装后仍未满足: {', '.join(still)}"}
    log.info("插件 %s 依赖已补齐（%d 项）", pid, len(missing))
    return {"status": "installed", "missing": missing, "error": ""}


# ----- 异步安装作业 -----

def _phase_from_line(job: dict, line: str) -> None:
    """从 pip 输出行推断当前阶段；只影响底部提示，不参与成功判定"""
    text = line.strip()
    if not text:
        return
    low = text.lower()
    if low.startswith("collecting "):
        job["phase"] = "downloading"
        job["detail"] = text[len("Collecting "):][:60]
    elif low.startswith("downloading "):
        job["phase"] = "downloading"
        job["detail"] = text[len("Downloading "):][:60]
    elif "installing collected packages" in low:
        job["phase"] = "installing"
        job["detail"] = text.split(":", 1)[-1].strip()[:60]
    elif low.startswith("successfully installed"):
        job["phase"] = "verifying"
        job["detail"] = text[len("Successfully installed "):][:60]
    elif "error:" in low or "no matching distribution" in low:
        job["phase"] = "installing"
        job["detail"] = "安装报错"
        job["error"] = text


def _run_install(pid: str, dir_path: Path, job: dict) -> None:
    cmd = [
        sys.executable, "-m", "pip", "install", "-r", str(dir_path / _REQ_FILE),
        "--disable-pip-version-check", "--no-input",
    ]
    proc = None
    watchdog = None
    try:
        job["phase"] = "downloading"
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
        )
        job["proc"] = proc
        watchdog = threading.Timer(_PIP_TIMEOUT, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        assert proc.stdout is not None
        for raw in proc.stdout:
            if job.get("cancelled"):
                break
            line = raw.strip()
            if not line:
                continue
            _phase_from_line(job, line)
            job["tail"] = (job["tail"] + [line])[-20:]
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
        if job.get("cancelled"):
            job["error"] = "已取消"
            return
        if code != 0:
            tail = "\n".join(job["tail"][-8:])
            job["error"] = tail or f"pip 退出码 {code}"
            return
        job["phase"] = "verifying"
        importlib.invalidate_caches()
        still = _missing_requirements(_read_requirements(dir_path))
        if still:
            job["error"] = f"安装后仍未满足: {', '.join(still)}"
            return
        job["ok"] = True
        log.info("插件 %s 依赖已补齐（%s）", pid, ", ".join(job.get("missing") or []))
    except FileNotFoundError as e:
        job["error"] = f"无法启动 pip: {e}"
    except Exception as e:  # noqa: BLE001 - 后台线程兜底
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if not job.get("ok") and not job.get("error"):
            job["error"] = "未知错误"
        if job.get("error"):
            log.warn("插件 %s 依赖安装失败: %s", pid, job["error"].splitlines()[-1])
        with _JOBS_LOCK:
            job["done"] = True


def start_install(pid: str, dir_path: Path, config) -> dict:
    """启动异步补装；返回即时状态：
    none/ok=无需安装，manual=关闭自动安装，installing=已在安装，started=已启动
    """
    info = inspect_requirements(dir_path)
    if info["status"] != "missing":
        return info
    missing = info["missing"]
    if not _auto_install_enabled(config):
        return {"status": "manual", "missing": missing, "error": ""}
    with _JOBS_LOCK:
        current = _JOBS.get(pid)
        if current is not None and not current.get("done"):
            return {"status": "installing", "missing": current.get("missing") or missing, "error": ""}
        job = {
            "pid": pid,
            "dir": dir_path,
            "missing": missing,
            "phase": "starting",
            "detail": "",
            "tail": [],
            "error": "",
            "ok": False,
            "cancelled": False,
            "done": False,
            "started": time.monotonic(),
            "proc": None,
        }
        _JOBS[pid] = job
    log.info("插件 %s 缺依赖 %d 项，后台安装: %s", pid, len(missing), ", ".join(missing))
    threading.Thread(
        target=_run_install,
        args=(pid, dir_path, job),
        name=f"bawcode-deps-{pid}",
        daemon=True,
    ).start()
    return {"status": "started", "missing": missing, "error": ""}


def cancel_install(pid: str) -> None:
    """取消某插件的安装（终止 pip 子进程）；已完成的作业一并丢弃"""
    with _JOBS_LOCK:
        job = _JOBS.pop(pid, None)
    if job is None:
        return
    job["cancelled"] = True
    proc = job.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    log.info("插件 %s 依赖安装已取消", pid)


def cancel_all() -> None:
    with _JOBS_LOCK:
        pids = list(_JOBS.keys())
    for pid in pids:
        cancel_install(pid)


def take_completed() -> List[dict]:
    """取走已结束的作业（主线程 tick 调用），返回作业快照"""
    with _JOBS_LOCK:
        done = [job for job in _JOBS.values() if job.get("done") or job.get("cancelled")]
        for job in done:
            _JOBS.pop(job["pid"], None)
        return [dict(job) for job in done]


def job_state(pid: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(pid)
        return dict(job) if job else {}


def progress_text() -> str:
    """底部状态栏的单行进度文案；无进行中作业返回空串"""
    now = time.monotonic()
    with _JOBS_LOCK:
        jobs = [job for job in _JOBS.values() if not job.get("done") and not job.get("cancelled")]
    if not jobs:
        return ""
    parts = []
    for job in jobs[:2]:
        elapsed = int(max(0.0, now - float(job.get("started") or now)))
        phase = _PHASE_LABELS.get(str(job.get("phase")), str(job.get("phase") or "安装中"))
        detail = str(job.get("detail") or "").strip()
        if len(detail) > 28:
            detail = detail[:27] + "…"
        body = f"{job.get('pid')} {phase}"
        if detail:
            body += f" {detail}"
        parts.append(f"{body} {elapsed}s")
    extra = f" 等{len(jobs)}项" if len(jobs) > 2 else ""
    frame = "|/-\\"[int(now * 4) % 4]
    return f"{frame} 插件依赖安装中: " + " · ".join(parts) + extra


def scan_conflicts(items: List[dict]) -> Dict[str, List[str]]:
    """跨插件依赖冲突扫描（纯检测不干预）：返回 {pid: [冲突描述]}。

    仅当 ≥2 个不同插件约束同一发行名时检查：
      - 已安装版本同时不满足两方 → 冲突（确定有一方已坏）
      - 满足其一不满足另一方 → 警示（后装一方会破坏另一方）
      - 未安装且纯下界/纯上界互斥（>=X 与 <Y 且 Y<=X）→ 潜在冲突
    复杂多段约束/环境标记/extras 跳过，宁缺勿滥。
    """
    if _Requirement is None:
        return {}
    by_name: Dict[str, List[tuple]] = {}
    for item in items:
        pid = item.get("id") or ""
        for line in _read_requirements(item["dir"]):
            name, spec = _split_requirement(line)
            if name is not None and spec is not None:
                by_name.setdefault(name.lower(), []).append((pid, line, spec))
    out: Dict[str, List[str]] = {}
    for name, entries in by_name.items():
        owners = {pid for pid, _, _ in entries}
        if len(owners) < 2:
            continue
        try:
            dist = importlib.metadata.distribution(name)
            installed = str(dist.version)
        except Exception:
            installed = ""
        if installed:
            unsat = [(pid, line) for pid, line, spec in entries if not spec.contains(installed, prereleases=True)]
            sat = [(pid, line) for pid, line, spec in entries if spec.contains(installed, prereleases=True)]
            if unsat and sat:
                for pid, line, _spec in entries:
                    others = "；".join(f"{p2} 要求 {l2}" for p2, l2, _ in entries if p2 != pid)
                    level = "冲突" if (pid, line) in unsat else "警示"
                    msg = f"{level}: {name} 已装 {installed}，本插件要求 {line}（{others}）"
                    out.setdefault(pid, []).append(msg)
            elif len(unsat) >= 2:
                for pid, line, _spec in entries:
                    others = "；".join(f"{p2} 要求 {l2}" for p2, l2, _ in entries if p2 != pid)
                    out.setdefault(pid, []).append(f"警示: {name} 已装 {installed} 不满足任何一方，本插件要求 {line}（{others}）")
            continue
        # 未安装：纯下界与纯上界互斥粗判
        lowers, uppers = [], []
        for pid, line, spec in entries:
            specs = list(spec)
            if len(specs) != 1:
                continue
            op, ver = specs[0].operator, specs[0].version
            if op in (">=", ">", "=="):
                lowers.append((pid, line, ver))
            elif op in ("<", "<="):
                uppers.append((pid, line, ver))
        try:
            from packaging.version import Version as _V
        except ImportError:
            _V = None

        def _ver_leq(a: str, b: str) -> bool:
            if _V is not None:
                try:
                    return _V(a) <= _V(b)
                except Exception:
                    return False
            return a == b

        for lpid, lline, lver in lowers:
            for upid, uline, uver in uppers:
                if lpid != upid and _ver_leq(uver, lver):
                    out.setdefault(lpid, []).append(f"潜在冲突: {name} 本插件要求 {lline}，{upid} 要求 {uline}（无共存版本）")
                    out.setdefault(upid, []).append(f"潜在冲突: {name} 本插件要求 {uline}，{lpid} 要求 {lline}（无共存版本）")
    return out
