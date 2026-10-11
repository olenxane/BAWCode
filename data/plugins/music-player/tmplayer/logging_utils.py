"""日志工具：将错误日志写入 log/ 文件夹，不在终端输出"""
from __future__ import annotations
import logging
import os
import traceback
from datetime import datetime

# log 文件夹位于项目根目录下
_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "log")
_LOG_FILE = os.path.join(_LOG_DIR, "error.log")

_root_logger = None


def _get_logger() -> logging.Logger:
    global _root_logger
    if _root_logger is not None:
        return _root_logger
    os.makedirs(_LOG_DIR, exist_ok=True)
    logger = logging.getLogger("terminal_music_player")
    logger.setLevel(logging.WARNING)
    # 清除已存在的 handler（防止多次初始化）
    logger.handlers.clear()
    handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    # 禁止向父级传播（避免 root logger 把日志打到 stderr）
    logger.propagate = False
    _root_logger = logger
    return logger


def log_error(message: str) -> None:
    """记录一条错误消息"""
    _get_logger().error(message)


def log_warning(message: str) -> None:
    """记录一条警告消息"""
    _get_logger().warning(message)


def log_exception(message: str) -> None:
    """记录一条带完整 traceback 的异常"""
    tb = traceback.format_exc()
    log_error(f"{message}\n{tb}")


def attach_external_logger(name: str) -> None:
    """把第三方库的 logger 挂到同一个日志文件，防止其警告输出到终端

    第三方库（如 qqmusicbox）未配置 handler 时，WARNING 及以上级别
    会经 logging.lastResort 机制直接打到 stderr，污染终端界面。
    """
    file_logger = _get_logger()
    target = logging.getLogger(name)
    for handler in file_logger.handlers:
        if handler not in target.handlers:
            target.addHandler(handler)
    target.setLevel(logging.WARNING)
    target.propagate = False


def get_log_path() -> str:
    """返回当前错误日志文件路径"""
    return _LOG_FILE


def get_log_dir() -> str:
    """返回 log 文件夹路径"""
    return _LOG_DIR
