"""应用图标。

代码绘制、零资源文件，避免打包时漏带 .ico 或路径解析问题。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

_cache: QIcon | None = None


def app_icon() -> QIcon:
    """返回应用图标（首次调用时生成并缓存）。"""
    global _cache
    if _cache is not None:
        return _cache

    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)

    painter.setBrush(QColor("#2d7ff9"))
    painter.drawRoundedRect(QRectF(3, 3, 58, 58), 14, 14)

    # 白色鼠标指针
    arrow = QPainterPath()
    arrow.moveTo(23, 16)
    arrow.lineTo(23, 47)
    arrow.lineTo(30, 40)
    arrow.lineTo(35, 50)
    arrow.lineTo(40, 47)
    arrow.lineTo(35, 37)
    arrow.lineTo(44, 37)
    arrow.closeSubpath()
    painter.setBrush(QColor("#ffffff"))
    painter.drawPath(arrow)

    painter.end()
    _cache = QIcon(pixmap)
    return _cache
