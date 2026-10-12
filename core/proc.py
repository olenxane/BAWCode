#进程执行底层：输出落临时文件并轮询等待，超时/取消时按进程树整体终止（Windows 走 Job Object）
import ctypes
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

from core.log import get_logger

log = get_logger("proc")

# 轮询间隔：取消响应速度与唤醒开销的折中
POLL_INTERVAL = 0.05
# 终止命令树后回收进程的有界期限
KILL_WAIT = 3.0

_cancel_state = threading.local()


def set_cancel(event) -> None:
    """下发本线程正在执行的回合取消事件（llm 在工具执行期设置，None 表示脱离回合）"""
    _cancel_state.event = event


def clear_cancel() -> None:
    _cancel_state.event = None


def cancel_event():
    """当前线程的取消事件；工具被独立调用时无事件，按不可取消处理"""
    return getattr(_cancel_state, "event", None)


if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _k32.CreateJobObjectW.restype = ctypes.c_void_p
    _k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k32.AssignProcessToJobObject.restype = ctypes.c_int
    _k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _k32.TerminateJobObject.restype = ctypes.c_int
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = ctypes.c_int


def _job_attach(proc: subprocess.Popen):
    """把命令进程加入 Job Object：后代在创建时默认归属同一 Job，终止时可整体清理

    只按根 PID 记 taskkill /T 时，根先退出就再也收不到后代（管道残留与进程泄漏的来源）。"""
    if sys.platform != "win32":
        return None
    handle = getattr(proc, "_handle", None)
    if not handle:
        return None
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        log.warn("Job Object 创建失败: %s", ctypes.get_last_error())
        return None
    if not _k32.AssignProcessToJobObject(job, ctypes.c_void_p(int(handle))):
        log.warn("进程加入 Job Object 失败 pid=%s: %s", proc.pid, ctypes.get_last_error())
        _k32.CloseHandle(job)
        return None
    return job


def _job_terminate(job) -> bool:
    if not job:
        return False
    if not _k32.TerminateJobObject(job, 1):
        log.warn("Job Object 终止失败: %s", ctypes.get_last_error())
        return False
    return True


def _job_close(job) -> None:
    if job:
        _k32.CloseHandle(job)


def _terminate_tree(proc: subprocess.Popen, job) -> None:
    """终止整棵命令进程树并做有界回收；Job 不可用时退回按平台兜底"""
    if not _job_terminate(job):
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warn("命令进程树终止失败 pid=%s: %s", proc.pid, exc)
    try:
        proc.wait(timeout=KILL_WAIT)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            log.warn("命令进程强杀后仍未回收 pid=%s", proc.pid)


def _wait(proc: subprocess.Popen, timeout: Optional[float], cancel) -> str:
    """轮询等待命令结束，返回 ok/timeout/cancelled；不阻塞读管道，取消最迟一个间隔生效"""
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        if proc.poll() is not None:
            return "ok"
        if cancel is not None:
            if cancel.wait(POLL_INTERVAL):
                return "cancelled"
        else:
            time.sleep(POLL_INTERVAL)
        if deadline is not None and time.monotonic() >= deadline:
            return "timeout"


def _read_output(out, err, encoding: str) -> str:
    """读回输出文件；合并模式 err 即 out，只读一份"""
    out.seek(0)
    text = out.read().decode(encoding, errors="replace")
    if err is not out:
        err.seek(0)
        text += err.read().decode(encoding, errors="replace")
    return text


def _temp_path(suffix: str) -> str:
    """输出与脚本临时文件统一落系统临时目录，不污染工作区"""
    fd, path = tempfile.mkstemp(prefix="baw-command-", suffix=suffix)
    os.close(fd)
    return path


def _open_output(path: str):
    """输出文件句柄：Windows 走 O_TEMPORARY，全部句柄关闭时由系统删除，遗留后代的输出不留残件"""
    if sys.platform == "win32":
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_TEMPORARY)
            return os.fdopen(fd, "w+b")
        except OSError as exc:
            log.warn("临时输出文件按 O_TEMPORARY 打开失败，回落普通模式: %s", exc)
    return open(path, "w+b")


def _remove(paths) -> None:
    """删除临时文件，重复路径只删一次；被杀后代收尾期间仍持有句柄，短暂重试后放弃"""
    for path in dict.fromkeys(p for p in paths if p):
        for _ in range(3):
            try:
                os.unlink(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.05)


def run_capture(
    argv,
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    use_shell: bool = False,
    stdin_text: str = "",
    timeout: Optional[float] = None,
    cancel=None,
    merge_stderr: bool = True,
    encoding: str = "utf-8",
):
    """执行命令并收集输出，返回 (status, returncode, output)，status 为 ok/timeout/cancelled

    输出落临时文件而非管道：后代继承输出句柄不会挂住调用方，也没有读线程与关流互锁；
    stdin_text 写临时脚本文件送入（多行命令走 stdin 的解释器）。"""
    out_path = _temp_path(".out")
    err_path = out_path if merge_stderr else _temp_path(".err")
    script_path = None
    stdin_fh = out = err = None
    job = None
    try:
        out = _open_output(out_path)
        err = out if merge_stderr else _open_output(err_path)
        if stdin_text:
            script_path = _temp_path(".script")
            with open(script_path, "wb") as script:
                script.write(str(stdin_text).encode(encoding, errors="replace"))
            stdin_fh = open(script_path, "rb")
        proc = subprocess.Popen(
            argv,
            shell=use_shell,
            cwd=cwd,
            stdin=stdin_fh or subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=env,
            start_new_session=sys.platform != "win32",
        )
        job = _job_attach(proc)
        status = _wait(proc, timeout, cancel)
        if status != "ok":
            _terminate_tree(proc, job)
        return status, proc.returncode, _read_output(out, err, encoding)
    finally:
        for fh in (stdin_fh, out, None if err is out else err):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        _job_close(job)
        _remove((script_path, out_path, err_path))
