"""日志初始化。

两路输出：
- 文件：``%LOCALAPPDATA%\\AutoClicker\\logs\\autoclicker.log``，滚动保留；
- UI：通过 ``SignalLogHandler`` 转发到主窗口的日志面板。

``SignalLogHandler`` 可能在任意线程被调用，依赖 Qt 跨线程信号自动排队（queued connection）机制，
因此是线程安全的。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
UI_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%H:%M:%S"

_configured = False


def app_data_dir() -> Path:
    """返回可写的应用数据目录。"""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "AutoClicker"


def log_dir() -> Path:
    return app_data_dir() / "logs"


class SignalLogHandler(logging.Handler):
    """把日志记录转发给一个可调用对象（通常绑定到 Qt 信号的 ``emit``）。"""

    def __init__(self, sink: Callable[[str], None], level: int = logging.INFO) -> None:
        super().__init__(level)
        self._sink = sink
        self.setFormatter(logging.Formatter(UI_LOG_FORMAT, DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(self.format(record))
        except Exception:  # pragma: no cover - 日志本身绝不能再抛异常
            self.handleError(record)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置根日志器，返回应用日志器。重复调用是幂等的。"""
    global _configured

    root = logging.getLogger()
    if _configured:
        return logging.getLogger("autoclicker")

    root.setLevel(logging.DEBUG)

    # 文件输出
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / "autoclicker.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - 目录不可写时降级
        print(f"警告: 无法创建日志文件 ({exc})", file=sys.stderr)

    # 控制台输出（源码运行时方便排查）
    if not getattr(sys, "frozen", False):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        root.addHandler(console)

    _configured = True
    return logging.getLogger("autoclicker")
