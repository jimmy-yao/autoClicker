"""录制中的悬浮指示条。

录制时主窗口会被最小化（否则会挡住用户要操作的目标），所以必须有一个独立的
视觉反馈告诉用户「正在录」。

两个关键属性：

- ``WindowTransparentForInput``：鼠标点击直接穿透，不会挡住/被点到；
- ``WindowDoesNotAcceptFocus``：不抢焦点，避免打乱正在录制的目标窗口焦点状态。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

_BG = QColor(24, 28, 38, 236)
_BORDER = QColor(220, 70, 70, 200)
_TEXT = QColor(240, 245, 255)
_DIM = QColor(160, 172, 190)
_DOT_ON = QColor(230, 70, 70)
_DOT_OFF = QColor(120, 90, 90)

WIDTH = 460
HEIGHT = 74
TOP_MARGIN = 24
BLINK_MS = 500


class RecordOverlay(QWidget):
    """常驻顶部的「● 录制中」指示条。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(WIDTH, HEIGHT)

        self._actions = 0
        self._moves = 0
        self._swallow = True
        self._blink_on = True

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(BLINK_MS)
        self._blink_timer.timeout.connect(self._toggle_blink)

    # ------------------------------------------------------------------ 接口
    def start(self, swallow: bool) -> None:
        self._actions = 0
        self._moves = 0
        self._swallow = swallow
        self._blink_on = True
        self._move_to_default_corner()
        self.show()
        self.raise_()
        self._blink_timer.start()
        self.update()

    def set_count(self, actions: int, moves: int) -> None:
        if (actions, moves) == (self._actions, self._moves):
            return
        self._actions = actions
        self._moves = moves
        self.update()

    def finish(self) -> None:
        self._blink_timer.stop()
        self.hide()

    # ------------------------------------------------------------------ 内部
    def _move_to_default_corner(self) -> None:
        """摆在主显示器顶部居中。用主显示器而不是虚拟桌面，避免多屏时间隙错位。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:  # pragma: no cover - 无显示器环境
            return
        area = screen.availableGeometry()
        self.move(area.x() + (area.width() - self.width()) // 2, area.y() + TOP_MARGIN)

    def _toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        self.update()

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        bounds = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(_BORDER)
        painter.setBrush(_BG)
        painter.drawRoundedRect(bounds, 12, 12)

        # 闪烁的红点
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_DOT_ON if self._blink_on else _DOT_OFF)
        painter.drawEllipse(18, 26, 14, 14)

        painter.setPen(_TEXT)
        painter.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.Bold))
        painter.drawText(44, 24, 200, 20, int(Qt.AlignmentFlag.AlignLeft), "录制中")

        painter.setPen(_DIM)
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(44, 44, 400, 18, int(Qt.AlignmentFlag.AlignLeft), self._detail())

        painter.end()

    def _detail(self) -> str:
        hint = "按 F10 结束录制"
        if not self._swallow:
            hint += "（未屏蔽输入）"
        detail = f"{hint}   ·   点击/按键 {self._actions}"
        if self._moves:
            detail += f"   ·   移动采样 {self._moves}"
        metrics = QFontMetrics(QFont("Microsoft YaHei UI", 9))
        return metrics.elidedText(detail, Qt.TextElideMode.ElideRight, WIDTH - 60)
