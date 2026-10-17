"""系统托盘。

托盘不可用时（远程桌面、精简系统）整个对象退化成空壳：``available`` 为 False，
``notify`` / ``hide`` 什么也不做。这样调用方不需要在每处都判空，MainWindow 的
``closeEvent`` 也只需要问一句「有托盘吗」。

托盘菜单只负责把动作**转成信号**抛出去，具体做什么由 MainWindow 决定——
菜单项和热键因此收敛到同一批入口，不会出现两套行为。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .icon import app_icon

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "AutoClicker"
DEFAULT_MSEC = 3000


class TrayIcon(QObject):
    """托盘图标 + 右键菜单。"""

    restore_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    pick_requested = Signal()
    record_toggle_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._tray: QSystemTrayIcon | None = None
        # setContextMenu **不接管** menu 的所有权，必须自己持有引用，
        # 否则菜单会被 Python 回收，托盘右键变成空白。
        self._menu: QMenu | None = None
        self._notified: set[str] = set()

        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("系统托盘不可用，关闭窗口将直接退出")
            return

        tray = QSystemTrayIcon(app_icon(), parent)
        tray.setToolTip("AutoClicker · 自动点击器")

        menu = QMenu(parent)
        for text, signal in (
            ("显示主窗口", self.restore_requested),
            (None, None),
            ("开始点击 (F9)", self.start_requested),
            ("停止 (F10)", self.stop_requested),
            ("拾取坐标 (F8)", self.pick_requested),
            ("开始/结束录制 (F11)", self.record_toggle_requested),
            (None, None),
            ("退出", self.quit_requested),
        ):
            if text is None:
                menu.addSeparator()
                continue
            action = QAction(text, menu)
            action.triggered.connect(self._relay(signal))
            menu.addAction(action)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_activated)
        tray.show()
        self._tray = tray
        self._menu = menu

    def actions(self) -> list[QAction]:
        """右键菜单项，跳过分隔线。给测试用。"""
        return [] if self._menu is None else [a for a in self._menu.actions() if not a.isSeparator()]

    # ---------------------------------------------------------------- 状态
    @property
    def available(self) -> bool:
        return self._tray is not None

    def hide(self) -> None:
        if self._tray is not None:
            self._tray.hide()

    # ---------------------------------------------------------------- 提示
    def notify(self, message: str, title: str = DEFAULT_TITLE, msec: int = DEFAULT_MSEC) -> None:
        """弹一个托盘气泡。没有托盘时静默忽略。"""
        if self._tray is None:
            return
        self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, msec)

    def notify_once(
        self,
        key: str,
        message: str,
        title: str = DEFAULT_TITLE,
        msec: int = 4000,
    ) -> None:
        """同一条提示只弹一次（例如「已最小化到托盘」）。``key`` 是去重标识。"""
        if key in self._notified:
            return
        self._notified.add(key)
        self.notify(message, title, msec)

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _relay(signal):
        """``QAction.triggered`` 会捎带一个 checked 参数，这里丢掉它再转发。"""
        return lambda *_args: signal.emit()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.restore_requested.emit()
