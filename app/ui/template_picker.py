"""找图用的全屏拖框遮罩。

和 ``position_picker`` 一样，交互走 **Qt 逻辑坐标**（画框、判位置），
实际取值走 **Win32 物理坐标**（``GetCursorPos``），
这样多显示器混合缩放下画得贴合、落库的又是 ``SendInput`` 要的物理像素。

拖框期间遮罩是**半透明压暗**的，为了让用户看清要抠的位置；因此截取不能发生在
松手的那一刻（会把遮罩自己也拍进去）。松手后由 ``PickCoordinator`` 负责
「先彻底隐藏 → 等合成器重绘 → 再抓屏」，见 ``HIDE_SETTLE_MS``。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QWidget

from ..win.geometry import ScreenRect, get_cursor_pos

logger = logging.getLogger(__name__)

_OVERLAY_COLOR = QColor(0, 0, 0, 110)
_SELECT_COLOR = QColor(0, 230, 150)
_HINT_BG = QColor(20, 24, 32, 235)
_HINT_TEXT = QColor(240, 245, 255)

# 太小的框多半是误触，不当作有效模板
MIN_TEMPLATE_PX = 8

_HINT = "按住左键拖出一个框 · 右键/Esc 取消"


class TemplatePicker(QWidget):
    """全屏拖框遮罩。松手后给出**物理像素**矩形。"""

    selected = Signal(object)  # ScreenRect
    canceled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._active = False
        self._resolved = False
        self._dragging = False
        self._start_local: QPoint | None = None
        self._local: QPoint | None = None
        self._start_physical: tuple[int, int] | None = None
        self._end_physical: tuple[int, int] | None = None
        self._too_small = False

    # ------------------------------------------------------------------ 接口
    def start(self) -> None:
        self._active = True
        self._resolved = False
        self._dragging = False
        self._start_local = None
        self._local = None
        self._start_physical = None
        self._end_physical = None
        self._too_small = False

        self._apply_geometry()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.grabKeyboard()
        self.update()

    def cancel(self) -> None:
        """外部请求取消，等价于按 Esc。"""
        if self._active:
            self._resolve(self.canceled.emit)

    # ------------------------------------------------------------------ 内部
    def _apply_geometry(self) -> None:
        desktop = QRect()
        for screen in QGuiApplication.screens():
            desktop = desktop.united(screen.geometry())
        if desktop.isValid() and not desktop.isEmpty():
            self.setGeometry(desktop)
            return

        primary = QGuiApplication.primaryScreen()  # pragma: no cover - 兜底
        if primary is not None:
            self.setGeometry(primary.virtualGeometry())

    def _resolve(self, emit_action) -> None:
        if self._resolved:
            return
        self._resolved = True
        self._active = False
        self._dragging = False
        self.releaseKeyboard()
        self.hide()
        emit_action()

    def selection(self) -> ScreenRect | None:
        """当前框在**物理像素**下的矩形，无效时返回 ``None``。"""
        if self._start_physical is None or self._end_physical is None:
            return None
        (ax, ay), (bx, by) = self._start_physical, self._end_physical
        left, top = min(ax, bx), min(ay, by)
        width, height = abs(bx - ax), abs(by - ay)
        if width < MIN_TEMPLATE_PX or height < MIN_TEMPLATE_PX:
            return None
        return ScreenRect(left, top, width, height)

    def _local_box(self) -> QRect | None:
        if self._start_local is None or self._local is None:
            return None
        return QRect(self._start_local, self._local).normalized()

    # ------------------------------------------------------------ Qt 事件
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._too_small = False
            self._start_local = event.position().toPoint()
            self._local = self._start_local
            self._start_physical = get_cursor_pos()
            self._end_physical = self._start_physical
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self._resolve(self.canceled.emit)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._local = event.position().toPoint()
        if self._dragging:
            self._end_physical = get_cursor_pos()
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._end_physical = get_cursor_pos()
        self._local = event.position().toPoint()

        rect = self.selection()
        if rect is None:
            # 太小/没拖动：不清场，让用户直接重来一次
            self._too_small = True
            self._start_local = None
            self._local = None
            self._start_physical = None
            self._end_physical = None
            self.update()
            return

        logger.debug("模板选区（物理像素）: %s", rect)
        self._resolve(lambda: self.selected.emit(rect))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._resolve(self.canceled.emit)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._active:
            self._resolve(self.canceled.emit)
        super().closeEvent(event)

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), _OVERLAY_COLOR)

        box = self._local_box()
        if box is not None and box.isValid():
            # 把选区挖成完全透明，用户看到的就是真实像素
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(box, Qt.GlobalColor.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            painter.setPen(QPen(_SELECT_COLOR, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(box.adjusted(1, 1, -1, -1))
            self._paint_size(painter, box)

        self._paint_hint(painter)
        painter.end()

    def _paint_size(self, painter: QPainter, box: QRect) -> None:
        rect = self.selection()
        text = f"{rect.width} × {rect.height}" if rect else "太小了"
        font = QFont("Microsoft YaHei UI", 10)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(text) + 16
        height = metrics.height() + 8

        bx = box.left()
        by = box.bottom() + 6
        if by + height > self.height():
            by = box.top() - height - 6
        bx = max(0, min(bx, self.width() - width))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_HINT_BG)
        painter.drawRoundedRect(bx, by, width, height, 6, 6)
        painter.setPen(_SELECT_COLOR)
        painter.drawText(
            bx, by, width, height, int(Qt.AlignmentFlag.AlignCenter), text
        )

    def _paint_hint(self, painter: QPainter) -> None:
        text = f"{_HINT}\n框太小会重来（至少 {MIN_TEMPLATE_PX}×{MIN_TEMPLATE_PX} 像素）"
        if self._too_small:
            text = f"框太小了，请重新拖一个至少 {MIN_TEMPLATE_PX}×{MIN_TEMPLATE_PX} 的框\n{_HINT}"
        font = QFont("Microsoft YaHei UI", 11)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        lines = text.split("\n")
        width = max(metrics.horizontalAdvance(line) for line in lines) + 28
        height = metrics.height() * len(lines) + 18

        anchor = self._local or QPoint(0, 0)
        bx, by = anchor.x() + 26, anchor.y() + 26
        if bx + width > self.width():
            bx = anchor.x() - width - 26
        if by + height > self.height():
            by = anchor.y() - height - 26
        bx, by = max(0, bx), max(0, by)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_HINT_BG)
        painter.drawRoundedRect(bx, by, width, height, 8, 8)
        painter.setPen(_HINT_TEXT)
        painter.drawText(
            bx + 14,
            by + 9,
            width - 28,
            height - 18,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )
