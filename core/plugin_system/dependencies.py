"""依赖库自动安装与检查"""
import importlib
import importlib.metadata
import re
import subprocess
import sys
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
    """检查并静默补齐插件目录 requirements.txt 声明的依赖。

    已满足则零开销跳过，只在缺失时调用一次 pip。返回记录：
      {"status": "none"|"ok"|"installed"|"manual"|"failed", "missing": [...], "error": str}
      none=无 requirements.txt；ok=依赖齐备；installed=已补装成功；
      manual=缺依赖但已关闭自动安装；failed=补装失败，不阻断装载，插件自行降级
    """
    none = {"status": "none", "missing": [], "error": ""}
    if not (dir_path / _REQ_FILE).is_file():
        return none
    reqs = _read_requirements(dir_path)
    if not reqs:
        return {"status": "ok", "missing": [], "error": ""}
    missing = _missing_requirements(reqs)
    if not missing:
        log.debug("插件 %s 依赖已满足（%d 项）", pid, len(reqs))
        return {"status": "ok", "missing": [], "error": ""}
    if not _auto_install_enabled(config):
        log.info("插件 %s 缺依赖 %d 项（已关闭自动安装）: %s", pid, len(missing), ", ".join(missing))
        return {"status": "manual", "missing": missing, "error": ""}
    log.info("插件 %s 缺依赖 %d 项，静默安装: %s", pid, len(missing), ", ".join(missing))
    ok, detail = _pip_install(pid, dir_path / _REQ_FILE)
    if not ok:
        log.warn("插件 %s 依赖安装失败: %s", pid, detail or "未知错误")
        return {"status": "failed", "missing": missing, "error": detail}
    importlib.invalidate_caches()  # 让本轮新装的发行版可见，复核是否真的补齐
    still = _missing_requirements(reqs)
    if still:
        log.warn("插件 %s 依赖安装后仍未满足: %s", pid, ", ".join(still))
        return {"status": "failed", "missing": still, "error": f"安装后仍未满足: {', '.join(still)}"}
    log.info("插件 %s 依赖已补齐（%d 项）", pid, len(missing))
    return {"status": "installed", "missing": missing, "error": ""}
