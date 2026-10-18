"""坐标拾取与模板截取：覆盖全部显示器的半透明遮罩。

设计要点
--------
- **可视布局用 Qt 逻辑坐标**：几何取所有 ``QScreen.geometry()`` 的并集，
  这样在多显示器 + 混合缩放时视觉上是贴合的。
- **实际捕获用 Win32 物理坐标**：通过 ``GetCursorPos()`` 取值，
  因此无论 Qt 侧如何缩放，落库的坐标始终是 SendInput 需要的物理像素。
- 实例可复用：``start()`` 会重置状态，拾取或取消后仅 ``hide()`` 不销毁。

两种取法共用一个协调器（``PickCoordinator``）：

- ``request()``          —— 取一个坐标点（``PositionPicker``）
- ``request_template()`` —— 拖框截一张模板图（``TemplatePicker``）

两者都必须先让开启流程的那个窗口让开：模态对话框会拦住遮罩的鼠标事件，
而且它还正好挡在要拾取的位置上。截取模板时还多一步「等合成器重绘」，
否则会把还没消失的遮罩一起拍进模板里（见 ``HIDE_SETTLE_MS``）。
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QWidget

from ..core.matching import Template
from ..win.geometry import ScreenRect, get_cursor_pos
from ..win.screen import capture_screen
from .template_picker import TemplatePicker

logger = logging.getLogger(__name__)

# 隐藏请求方窗口后等合成器重绘的时间（毫秒）。
# 不等的话截到的还是「遮罩或对话框盖在上面」的那一帧。
HIDE_SETTLE_MS = 150

_OVERLAY_COLOR = QColor(0, 0, 0, 70)
_CROSS_COLOR = QColor(0, 255, 140)
_BOX_COLOR = QColor(20, 24, 32, 235)
_TEXT_COLOR = QColor(240, 245, 255)

PickCallback = Callable[[int, int], None]
CancelCallback = Callable[[], None]
TemplateCallback = Callable[[Template], None]


class PositionPicker(QWidget):
    """全屏取点遮罩。左键拾取，右键或 Esc 取消。"""

    picked = Signal(int, int)
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
        self._cursor: tuple[int, int] = (0, 0)
        self._local: QPoint | None = None

    # ------------------------------------------------------------------ 接口
    def start(self) -> None:
        """显示遮罩并开始取点。"""
        self._active = True
        self._resolved = False
        self._apply_geometry()
        self._cursor = get_cursor_pos()
        self._local = None
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.grabKeyboard()
        self.update()
        logger.debug("取点器已启动，当前光标 %s", self._cursor)

    def cancel(self) -> None:
        """外部请求取消取点，等价于按 Esc。"""
        if not self._active:
            return
        self._resolve(self.canceled.emit)

    # ------------------------------------------------------------------ 内部
    def _apply_geometry(self) -> None:
        """把遮罩铺满所有显示器（Qt 逻辑坐标下的并集）。"""
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
        self.releaseKeyboard()
        self.hide()
        emit_action()

    # ------------------------------------------------------------ Qt 事件
    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._local = event.position().toPoint()
        self._cursor = get_cursor_pos()
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            x, y = get_cursor_pos()
            self._resolve(lambda: self.picked.emit(x, y))
        elif event.button() == Qt.MouseButton.RightButton:
            self._resolve(self.canceled.emit)

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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.fillRect(self.rect(), _OVERLAY_COLOR)

        local = self._local
        if local is not None:
            painter.setPen(QPen(_CROSS_COLOR, 1))
            painter.drawLine(0, local.y(), self.width(), local.y())
            painter.drawLine(local.x(), 0, local.x(), self.height())

        self._paint_hint(painter, local)
        painter.end()

    def _paint_hint(self, painter: QPainter, local: QPoint | None) -> None:
        text = f"X: {self._cursor[0]}    Y: {self._cursor[1]}\n左键拾取 · 右键/Esc 取消"
        font = QFont("Microsoft YaHei UI", 11)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        lines = text.split("\n")
        width = max(metrics.horizontalAdvance(line) for line in lines) + 28
        height = metrics.height() * len(lines) + 18

        anchor = local if local is not None else QPoint(0, 0)
        bx = anchor.x() + 26
        by = anchor.y() + 26
        # 贴近屏幕边缘时自动翻到另一侧
        if bx + width > self.width():
            bx = anchor.x() - width - 26
        if by + height > self.height():
            by = anchor.y() - height - 26
        bx = max(0, bx)
        by = max(0, by)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_BOX_COLOR)
        painter.drawRoundedRect(bx, by, width, height, 8, 8)

        painter.setPen(_TEXT_COLOR)
        painter.drawText(
            bx + 14,
            by + 9,
            width - 28,
            height - 18,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )


class PickCoordinator(QObject):
    """集中管理取点 / 截模板的整套流程。

    取点经常发生在「某个窗口正开着」的时候（主窗口、步骤编辑对话框），
    而这些窗口往往正好挡住要拾取的位置。因此把「先隐藏请求方 → 显示遮罩 →
    回调 → 再恢复请求方」这套流程收在一个地方，调用方只需要传 ``hide_widget``。

    为什么要隐藏请求方而不是仅靠遮罩盖在上面：
    Qt 的**模态**对话框会拦截其它窗口的输入。把模态对话框临时 ``hide()`` 掉
    （隐藏的模态窗口不参与输入拦截），遮罩才能正常接到鼠标点击。
    """

    failed = Signal(str)  # 截取模板失败时的原因，供日志面板显示

    POINT = "point"
    TEMPLATE = "template"
    CAPTURE = "capture"

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # 刻意不给遮罩设 parent：带 parent 的 Qt.Tool 窗口会随 parent 一起隐藏，
        # 而取点流程恰恰需要先隐藏请求方窗口。
        self._picker = PositionPicker(None)
        self._picker.picked.connect(self._handle_picked)
        self._picker.canceled.connect(self._handle_canceled)

        self._template_picker = TemplatePicker(None)
        self._template_picker.selected.connect(self._handle_template_selected)
        self._template_picker.canceled.connect(self._handle_canceled)

        self._mode: str | None = None
        self._on_picked: PickCallback | None = None
        self._on_template: TemplateCallback | None = None
        self._on_captured = None
        self._on_canceled: CancelCallback | None = None
        self._hider: QWidget | None = None
        self._pending_rect: ScreenRect | None = None

    @property
    def busy(self) -> bool:
        """是否有取点/截模板流程正在进行（含等待合成器重绘的那一小段）。

        ``busy`` 不能只看遮罩可见性：松手之后遮罩已经隐藏，但截取还没完成，
        这段时间里必须继续拒绝新的取点请求（比如用户又按了一下 F8）。
        """
        return self._mode is not None

    def cancel(self) -> None:
        """外部请求取消（等价于用户按 Esc）。"""
        if self._mode == self.POINT:
            self._picker.cancel()
        elif self._mode == self.TEMPLATE and self._template_picker.isVisible():
            self._template_picker.cancel()

    def request(
        self,
        on_picked: PickCallback,
        on_canceled: CancelCallback | None = None,
        hide_widget: QWidget | None = None,
    ) -> bool:
        """开始取点。已有流程在进行时返回 False。"""
        if self._mode is not None:
            return False

        self._mode = self.POINT
        self._on_picked = on_picked
        self._on_canceled = on_canceled
        self._hider = hide_widget
        if hide_widget is not None:
            hide_widget.hide()

        self._picker.start()
        return True

    def request_template(
        self,
        on_selected: TemplateCallback,
        on_canceled: CancelCallback | None = None,
        hide_widget: QWidget | None = None,
    ) -> bool:
        """开始拖框截模板。已有流程在进行时返回 False。

        回调拿到的是刚从屏幕上抠下来的 ``Template``；截取失败时走 ``on_canceled``
        并发出 ``failed`` 信号。
        """
        if self._mode is not None:
            return False

        self._mode = self.TEMPLATE
        self._on_template = on_selected
        self._on_canceled = on_canceled
        self._hider = hide_widget
        if hide_widget is not None:
            hide_widget.hide()

        self._template_picker.start()
        return True

    def capture_screen_after_hide(self, on_captured, hide_widget: QWidget | None = None) -> bool:
        """让请求方让开一下，抓一帧整屏后把它恢复。

        给「模拟测试」用：对话框里正显示着模板预览图，而预览图**就是模板本身**，
        不先让开的话找图会先命中自己的预览图，测出来的结果毫无意义。
        """
        if self._mode is not None:
            return False

        self._mode = self.CAPTURE
        self._on_captured = on_captured
        self._hider = hide_widget
        if hide_widget is not None:
            hide_widget.hide()

        QTimer.singleShot(HIDE_SETTLE_MS, self._capture_full)
        return True

    # ------------------------------------------------------------------ 内部
    def _restore(self) -> None:
        # 遮罩正常路径下会自己 hide 后再发信号，这里再兜一次：
        # 「回调已交付」与「遮罩已消失」必须是同一时刻成立的不变式，
        # 否则遮罩会一直挂在屏幕上挡住用户操作。
        self._picker.hide()
        self._template_picker.hide()

        widget = self._hider
        self._hider = None
        self._on_picked = None
        self._on_template = None
        self._on_captured = None
        self._on_canceled = None
        self._pending_rect = None
        self._mode = None

        if widget is not None:
            widget.show()
            widget.raise_()
            widget.activateWindow()

    def _handle_picked(self, x: int, y: int) -> None:
        callback = self._on_picked
        self._restore()
        if callback is not None:
            callback(x, y)

    def _handle_canceled(self) -> None:
        callback = self._on_canceled
        self._restore()
        if callback is not None:
            callback()

    def _handle_template_selected(self, rect: ScreenRect) -> None:
        """松手了，但此刻遮罩刚刚隐藏、屏幕还没重绘完，所以延后一点再抓。"""
        self._pending_rect = rect
        QTimer.singleShot(HIDE_SETTLE_MS, self._capture_template)

    def _capture_template(self) -> None:
        rect = self._pending_rect
        if rect is None:  # pragma: no cover - 流程被提前清理
            return

        callback = self._on_template
        on_canceled = self._on_canceled
        failure = None
        template = None
        try:
            template = Template.from_frame(capture_screen(rect))
        except (OSError, ValueError) as exc:
            failure = f"截取模板失败：{exc}"
            logger.error("%s", failure)
        except Exception as exc:  # noqa: BLE001 - 见下：兜底也必须把请求方还回去
            failure = f"截取模板时出现未预期的错误：{exc!r}"
            logger.exception("%s", failure)
        finally:
            # 请求方的窗口是我们 hide() 掉的，``_mode`` 也只能在这里清掉。
            # 所以这一步不是「正常路径的收尾」而是不变式：漏掉它的症状是
            # 「对话框/主窗口再也回不来」，而且此后**所有**拾取都被 busy 挡回去。
            self._restore()

        if failure is not None:
            self.failed.emit(failure)
            if on_canceled is not None:
                on_canceled()
            return
        if callback is not None and template is not None:
            callback(template)

    def _capture_full(self) -> None:
        callback = self._on_captured
        failure = None
        frame = None
        try:
            frame = capture_screen()
        except OSError as exc:
            failure = f"抓取屏幕失败：{exc}"
            logger.error("%s", failure)
        except Exception as exc:  # noqa: BLE001 - 同上，不变式优先
            failure = f"抓取屏幕时出现未预期的错误：{exc!r}"
            logger.exception("%s", failure)
        finally:
            self._restore()  # 同样：无论如何都要把请求方还回去

        if failure is not None:
            self.failed.emit(failure)
            return
        if callback is not None and frame is not None:
            callback(frame)

