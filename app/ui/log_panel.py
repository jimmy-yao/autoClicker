"""日志面板。

标准 ``logging`` 的 handler 是**全局**的，必须和窗口同生共死：窗口销毁后 handler 还挂在
root logger 上，程序就会继续往一块已经释放的控件里写字。把「挂上」和「摘掉」收在同一个
对象里配套使用，避免只做了一半。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from ..utils.logger import SignalLogHandler

# 长时间挂机时日志不能把内存吃满，超过上限从最早的开始丢
MAX_BLOCKS = 2000


class LogPanel(QPlainTextEdit):
    """只读日志视图。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(MAX_BLOCKS)
        self.setFont(QFont("Consolas", 9))
        self._handler: SignalLogHandler | None = None

    def attach(self, sink: Callable[[str], None]) -> None:
        """把 root logger 的输出接到 ``sink``（通常是 ``Signal(str).emit``）。"""
        if self._handler is not None:
            return
        self._handler = SignalLogHandler(sink)
        logging.getLogger().addHandler(self._handler)

    def detach(self) -> None:
        if self._handler is None:
            return
        logging.getLogger().removeHandler(self._handler)
        self._handler = None
