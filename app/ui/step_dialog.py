"""步骤编辑对话框。

一个对话框同时支持所有步骤类型，靠 ``step.kind`` 决定展示哪一组字段。
比每种类型各写一个对话框省掉大量重复代码，又能保证「确定」后返回的步骤类型不变。

对话框是**模态**的。点「拾取坐标」/「截取屏幕」时会把自己临时隐藏再启动遮罩，
恢复由 ``PickCoordinator`` 负责（见 ``position_picker.PickCoordinator`` 的说明）。

「模拟测试」也需要先把自己藏起来：对话框里的模板预览图正好**就是模板本身**，
如果它还在屏幕上，找图会先命中自己的预览图，测出来的结果毫无意义。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.matching import MIN_PROBE_PX, Template, find_template, solid_run_length
from ..core.models import (
    ON_TIMEOUT_CONTINUE,
    ClickStep,
    DelayStep,
    KeyStep,
    MoveStep,
    Step,
    StepKind,
    WaitStep,
)
from ..input.base import MouseButton
from ..win.hotkey import vk_names, vk_of

_TITLES = {
    StepKind.CLICK: "点击步骤",
    StepKind.DELAY: "延时步骤",
    StepKind.KEY: "按键步骤",
    StepKind.MOVE: "移动步骤",
    StepKind.WAIT: "找图步骤",
}

# 预览图最大宽度：缩得比原始像素小，既省地方又不会「预览图 == 模板」自命中
_MAX_PREVIEW_WIDTH = 220


class StepDialog(QDialog):
    """新增 / 编辑一个步骤。"""

    def __init__(self, step: Step, picker, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = step.kind
        self._picker = picker
        self._source = step
        # MoveStep 的轨迹是可变的，单独存一份，避免拾取时污染原对象
        self._points: list[tuple[int, int]] = (
            list(step.points) if isinstance(step, MoveStep) else []
        )
        # WaitStep 的模板同理：先只存字符串，需要显示/测试时才解码
        self._template_b64: str = step.template if isinstance(step, WaitStep) else ""
        self._pending_test: Template | None = None

        self.setWindowTitle(_TITLES[step.kind])
        self.setModal(True)
        self.setMinimumWidth(380)

        self._build_ui()
        self._load(step)

    # ---------------------------------------------------------------- 构建
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._enabled_check = QCheckBox("启用此步骤")
        self._enabled_check.setChecked(True)
        form.addRow("", self._enabled_check)

        if self._kind is StepKind.CLICK:
            form.addRow(self._build_click_fields())
        elif self._kind is StepKind.DELAY:
            form.addRow(self._build_delay_fields())
        elif self._kind is StepKind.MOVE:
            form.addRow(self._build_move_fields())
        elif self._kind is StepKind.WAIT:
            form.addRow(self._build_wait_fields())
        else:
            form.addRow(self._build_key_fields())

        self._note_edit = QLineEdit()
        self._note_edit.setPlaceholderText("可选，例如「等待加载完成」")
        form.addRow("备注:", self._note_edit)

        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_click_fields(self) -> QWidget:
        from ..win.geometry import virtual_screen_rect

        limit = virtual_screen_rect()
        span = max(limit.width, limit.height, 10_000) * 2

        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)

        self._x_spin = QSpinBox()
        self._x_spin.setRange(-span, span)
        self._x_spin.setSuffix(" px")
        self._y_spin = QSpinBox()
        self._y_spin.setRange(-span, span)
        self._y_spin.setSuffix(" px")

        pick_button = QPushButton("拾取坐标 (F8)")
        pick_button.clicked.connect(self.start_pick)

        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("X"))
        position_row.addWidget(self._x_spin)
        position_row.addWidget(QLabel("Y"))
        position_row.addWidget(self._y_spin)
        position_row.addWidget(pick_button)

        self._button_combo = QComboBox()
        for button in MouseButton:
            self._button_combo.addItem(str(button), button)

        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(1, 1_000_000)
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 3_600_000)
        self._interval_spin.setSuffix(" ms")
        self._jitter_ms_spin = QSpinBox()
        self._jitter_ms_spin.setRange(0, 60_000)
        self._jitter_ms_spin.setSuffix(" ms")
        self._jitter_ms_spin.setToolTip("实际间隔落在 [间隔, 间隔 + 抖动] 之间，避免机械规律")
        self._jitter_px_spin = QSpinBox()
        self._jitter_px_spin.setRange(0, 500)
        self._jitter_px_spin.setSuffix(" px")
        self._jitter_px_spin.setToolTip("落点在 ±该半径内随机偏移")

        form.addRow("位置:", _wrap(position_row))
        form.addRow("鼠标按键:", self._button_combo)
        form.addRow("点击次数:", self._repeat_spin)
        form.addRow("点击间隔:", self._interval_spin)
        form.addRow("间隔抖动:", self._jitter_ms_spin)
        form.addRow("坐标抖动:", self._jitter_px_spin)
        return box

    def _build_delay_fields(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)

        self._delay_spin = QSpinBox()
        self._delay_spin.setRange(0, 86_400_000)
        self._delay_spin.setSuffix(" ms")
        self._jitter_ms_spin = QSpinBox()
        self._jitter_ms_spin.setRange(0, 60_000)
        self._jitter_ms_spin.setSuffix(" ms")

        form.addRow("等待时长:", self._delay_spin)
        form.addRow("随机追加:", self._jitter_ms_spin)
        return box

    def _build_key_fields(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)

        self._key_combo = QComboBox()
        self._key_combo.setEditable(False)
        for name in vk_names():
            self._key_combo.addItem(name, vk_of(name))

        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(1, 100_000)
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 3_600_000)
        self._interval_spin.setSuffix(" ms")

        form.addRow("按键:", self._key_combo)
        form.addRow("按键次数:", self._repeat_spin)
        form.addRow("按键间隔:", self._interval_spin)
        return box

    def _build_move_fields(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)

        self._path_label = QLabel()
        self._path_label.setWordWrap(True)
        pick_button = QPushButton("拾取终点 (F8)")
        pick_button.setToolTip("把轨迹改成「从原起点直线移动到新拾取的点」，时长不变")
        pick_button.clicked.connect(self.start_pick)

        self._duration_spin = QSpinBox()
        self._duration_spin.setRange(0, 600_000)
        self._duration_spin.setSuffix(" ms")
        self._duration_spin.setToolTip("走完整条轨迹所用的时间；填 0 表示瞬移")

        form.addRow("轨迹:", self._path_label)
        form.addRow("", pick_button)
        form.addRow("移动时长:", self._duration_spin)
        return box

    def _build_wait_fields(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)

        self._template_label = QLabel()
        self._template_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._template_label.setMinimumSize(_MAX_PREVIEW_WIDTH + 20, 64)
        self._template_label.setStyleSheet(
            "border: 1px dashed #7a7a7a; border-radius: 6px; color: #9aa4b2;"
        )
        self._template_hint = QLabel()
        self._template_hint.setWordWrap(True)

        crop_button = QPushButton("截取屏幕")
        crop_button.setToolTip(
            "把屏幕压暗后拖一个框，框住要让程序去认的那块画面。\n"
            "建议连按钮的纯色底色一起框进来——匹配就是靠这段纯色做初筛的。"
        )
        crop_button.clicked.connect(self.pick_template)

        self._tolerance_spin = QSpinBox()
        self._tolerance_spin.setRange(0, 255)
        self._tolerance_spin.setToolTip(
            "抽样比对时每个像素通道允许的最大色差。\n"
            "用来容忍文字抗锯齿、半透明之类的细微差异。\n"
            "注意：整块颜色都变了（换主题 / 改 DPI 缩放）时调它没用，必须重新截模板。"
        )

        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(0, 3_600_000)
        self._timeout_spin.setSingleStep(1000)
        self._timeout_spin.setSuffix(" ms")
        self._timeout_spin.setToolTip("最多等这么久。填 0 表示只找一次，找不到立刻按「超时后」处理。")

        self._poll_spin = QSpinBox()
        self._poll_spin.setRange(20, 60_000)
        self._poll_spin.setSingleStep(50)
        self._poll_spin.setSuffix(" ms")
        self._poll_spin.setToolTip("每隔多久看屏幕一眼。每看一眼都要抓一帧整屏，间隔越大越省 CPU。")

        self._click_check = QCheckBox("找到后点击它")
        self._click_check.toggled.connect(self._sync_click_enabled)

        self._button_combo = QComboBox()
        for button in MouseButton:
            self._button_combo.addItem(str(button), button)

        self._offset_x_spin = QSpinBox()
        self._offset_x_spin.setRange(-10_000, 10_000)
        self._offset_x_spin.setSuffix(" px")
        self._offset_y_spin = QSpinBox()
        self._offset_y_spin.setRange(-10_000, 10_000)
        self._offset_y_spin.setSuffix(" px")
        for spin in (self._offset_x_spin, self._offset_y_spin):
            spin.setToolTip("点击位置相对模板中心的偏移，用来微调到底点哪儿")

        offset_row = QHBoxLayout()
        offset_row.addWidget(self._offset_x_spin)
        offset_row.addWidget(self._offset_y_spin)

        self._timeout_combo = QComboBox()
        self._timeout_combo.addItem("继续下一步", ON_TIMEOUT_CONTINUE)
        self._timeout_combo.addItem("停止并报错", "stop")
        self._timeout_combo.setToolTip(
            "等不到时的处理。\n"
            "「停止并报错」会把当时的屏幕存一张图到快照目录，方便事后看是模板过期还是弹窗没出现。"
        )

        test_button = QPushButton("模拟测试")
        test_button.setToolTip("不跑宏、不点鼠标，就地抓一帧看看现在能不能找到；用来调容差。")
        test_button.clicked.connect(self.test_match)

        form.addRow(self._template_label)
        form.addRow("图片模板:", crop_button)
        form.addRow("", self._template_hint)
        form.addRow("容差:", self._tolerance_spin)
        form.addRow("超时:", self._timeout_spin)
        form.addRow("轮询间隔:", self._poll_spin)
        form.addRow("", self._click_check)
        form.addRow("鼠标按键:", self._button_combo)
        form.addRow("点击偏移:", _wrap(offset_row))
        form.addRow("超时后:", self._timeout_combo)
        form.addRow("", test_button)
        return box

    def _sync_click_enabled(self) -> None:
        enabled = self._click_check.isChecked()
        for widget in (
            self._button_combo,
            self._offset_x_spin,
            self._offset_y_spin,
        ):
            widget.setEnabled(enabled)

    # ---------------------------------------------------------------- 读写
    def _load(self, step: Step) -> None:
        self._enabled_check.setChecked(step.enabled)
        self._note_edit.setText(step.note)

        if isinstance(step, ClickStep):
            self._x_spin.setValue(step.x)
            self._y_spin.setValue(step.y)
            self._button_combo.setCurrentIndex(
                self._button_combo.findData(MouseButton(step.button).value)
            )
            self._repeat_spin.setValue(step.repeat)
            self._interval_spin.setValue(step.interval_ms)
            self._jitter_ms_spin.setValue(step.jitter_ms)
            self._jitter_px_spin.setValue(step.jitter_px)
        elif isinstance(step, DelayStep):
            self._delay_spin.setValue(step.delay_ms)
            self._jitter_ms_spin.setValue(step.jitter_ms)
        elif isinstance(step, KeyStep):
            index = self._key_combo.findData(step.vk)
            self._key_combo.setCurrentIndex(index if index >= 0 else 0)
            self._repeat_spin.setValue(step.repeat)
            self._interval_spin.setValue(step.interval_ms)
        elif isinstance(step, MoveStep):
            self._duration_spin.setValue(step.duration_ms)
            self._refresh_path_label()
        elif isinstance(step, WaitStep):
            self._tolerance_spin.setValue(step.tolerance)
            self._timeout_spin.setValue(step.timeout_ms)
            self._poll_spin.setValue(step.poll_interval_ms)
            self._click_check.setChecked(step.click)
            self._sync_click_enabled()
            self._button_combo.setCurrentIndex(
                self._button_combo.findData(MouseButton(step.button).value)
            )
            self._offset_x_spin.setValue(step.offset_x)
            self._offset_y_spin.setValue(step.offset_y)
            index = self._timeout_combo.findData(step.on_timeout)
            self._timeout_combo.setCurrentIndex(index if index >= 0 else 0)
            self._render_template()

    def result_step(self) -> Step:
        """把界面上的值组装成一个新的步骤对象。"""
        common = {"enabled": self._enabled_check.isChecked(), "note": self._note_edit.text().strip()}

        if self._kind is StepKind.CLICK:
            return ClickStep(
                x=self._x_spin.value(),
                y=self._y_spin.value(),
                button=MouseButton(self._button_combo.currentData()),
                repeat=self._repeat_spin.value(),
                interval_ms=self._interval_spin.value(),
                jitter_ms=self._jitter_ms_spin.value(),
                jitter_px=self._jitter_px_spin.value(),
                **common,
            )
        if self._kind is StepKind.DELAY:
            return DelayStep(
                delay_ms=self._delay_spin.value(),
                jitter_ms=self._jitter_ms_spin.value(),
                **common,
            )
        if self._kind is StepKind.MOVE:
            return MoveStep(
                points=list(self._points),
                duration_ms=self._duration_spin.value(),
                **common,
            )
        if self._kind is StepKind.WAIT:
            return WaitStep(
                template=self._template_b64,
                tolerance=self._tolerance_spin.value(),
                timeout_ms=self._timeout_spin.value(),
                poll_interval_ms=self._poll_spin.value(),
                click=self._click_check.isChecked(),
                button=MouseButton(self._button_combo.currentData()),
                offset_x=self._offset_x_spin.value(),
                offset_y=self._offset_y_spin.value(),
                on_timeout=str(self._timeout_combo.currentData()),
                **common,
            )
        return KeyStep(
            vk=int(self._key_combo.currentData() or 0),
            repeat=self._repeat_spin.value(),
            interval_ms=self._interval_spin.value(),
            **common,
        )

    # ---------------------------------------------------------------- 取点
    def start_pick(self) -> None:
        """隐藏本对话框并启动取点遮罩。延时/按键步骤没有坐标可填，直接忽略。"""
        if self._kind not in (StepKind.CLICK, StepKind.MOVE):
            return
        self._picker.request(self._apply_picked, hide_widget=self)

    def _apply_picked(self, x: int, y: int) -> None:
        if self._kind is StepKind.MOVE:
            # 保留原起点，改成直线走到新终点
            origin = self._points[0] if self._points else (x, y)
            self._points = [origin, (x, y)]
            self._refresh_path_label()
            return
        self._x_spin.setValue(x)
        self._y_spin.setValue(y)

    def _refresh_path_label(self) -> None:
        if not self._points:
            self._path_label.setText("（空轨迹，请点击下方按钮拾取终点）")
            return
        if len(self._points) == 1:
            self._path_label.setText(f"移动到 ({self._points[0][0]}, {self._points[0][1]})")
            return
        sx, sy = self._points[0]
        ex, ey = self._points[-1]
        self._path_label.setText(f"{len(self._points)} 个点：({sx}, {sy}) → ({ex}, {ey})")

    # ---------------------------------------------------------------- 找图
    def pick_template(self) -> None:
        """隐藏对话框并启动拖框遮罩，把框内的画面截成模板。"""
        if self._kind is not StepKind.WAIT:
            return
        self._picker.request_template(self._apply_template, hide_widget=self)

    def _apply_template(self, template: Template) -> None:
        self._template_b64 = template.encode()
        self._render_template()

    def _render_template(self) -> None:
        """刷新预览图与「这个模板好不好找」的提示。"""
        template = self._decoded_template()
        if template is None:
            self._template_label.setPixmap(QPixmap())
            self._template_label.setText("（未截取）")
            self._template_hint.setText("")
            return

        self._template_label.setText("")
        self._template_label.setPixmap(_preview_pixmap(template))

        run = solid_run_length(template)
        if run < MIN_PROBE_PX:
            self._template_hint.setStyleSheet("color: #e8a33d;")
            self._template_hint.setText(
                f"{template.summary} 像素 · 这块画面里没有连续 {MIN_PROBE_PX} 像素的纯色区域，"
                "匹配只能靠逐像素硬碰：界面稍微变一点（换主题、改 DPI、盖上半透明层）就可能找不到。"
                "建议把按钮的纯色底色一起框进来。"
            )
        else:
            self._template_hint.setStyleSheet("color: #7fb2e5;")
            self._template_hint.setText(
                f"{template.summary} 像素 · 含 {run} 像素纯色区域，颜色微变也扛得住。"
            )

    def _decoded_template(self) -> Template | None:
        if not self._template_b64:
            return None
        try:
            return Template.decode(self._template_b64)
        except ValueError:
            return None

    def test_match(self) -> None:
        """就地抓一帧找一次，把结果摆出来——调容差不用来回跑宏。"""
        template = self._decoded_template()
        if template is None:
            QMessageBox.information(self, "模拟测试", "请先截取要查找的图片模板。")
            return

        self._pending_test = template
        if not self._picker.capture_screen_after_hide(self._apply_test_frame, hide_widget=self):
            QMessageBox.information(self, "模拟测试", "另一个拾取流程正在进行，请稍后再试。")

    def _apply_test_frame(self, frame) -> None:
        template = self._pending_test
        self._pending_test = None
        if template is None:  # pragma: no cover - 流程被提前清理
            return

        result = find_template(frame, template, tolerance=self._tolerance_spin.value())
        match = result.match
        if match is not None:
            x = frame.x + match.x + template.width // 2
            y = frame.y + match.y + template.height // 2
            QMessageBox.information(
                self,
                "模拟测试",
                f"找到了。\n\n"
                f"相似度：{match.similarity:.2f}\n"
                f"模板尺寸：{template.summary} 像素\n"
                f"命中位置：({frame.x + match.x}, {frame.y + match.y})\n"
                f"点击位置：({x}, {y})（模板中心）\n\n"
                f"共扫描了 {result.candidates} 个候选位置。",
            )
            return

        best = result.best
        if best is None:
            detail = (
                "屏幕上没有任何一处颜色和目标一致。\n"
                "多半是目标窗口此刻没显示、被别的窗口挡住，"
                "或者它的外观变了（换了主题/语言/DPI 缩放）——那就得重新截一次模板。"
            )
        else:
            detail = (
                f"有几处颜色接近，但形状对不上（最像的一处相似度 {best.similarity:.2f}）。\n"
                "可以调大容差，或者重新截取模板时把按钮的纯色底色一起框进来。"
            )
        QMessageBox.warning(
            self,
            "模拟测试",
            f"没找到。\n\n{detail}\n\n"
            f"（当前容差 {self._tolerance_spin.value()}，扫描了 {result.candidates} 个候选位置）",
        )

    # ---------------------------------------------------------------- 校验
    def accept(self) -> None:
        """确定前先校验，非法时不关闭对话框。"""
        try:
            self.result_step().validate()
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        super().accept()


def _wrap(layout) -> QWidget:
    """把 QHBoxLayout 包成一个 QWidget，方便塞进 QFormLayout 的字段位。"""
    widget = QWidget()
    widget.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    return widget


def _preview_pixmap(template: Template) -> QPixmap:
    """把模板画成预览图。

    用 ``FastTransformation`` 而不是平滑缩放：缩放后的像素值会和模板不一致，
    而这种「看起来差不多」的模糊很容易让人误判匹配为什么会偏。
    """
    image = QImage(
        template.data,
        template.width,
        template.height,
        template.stride,
        QImage.Format.Format_RGB32,
    ).copy()  # copy：QImage 不持有 buffer，模板对象随时可能被回收
    pixmap = QPixmap.fromImage(image)
    if pixmap.width() > _MAX_PREVIEW_WIDTH:
        pixmap = pixmap.scaledToWidth(
            _MAX_PREVIEW_WIDTH, Qt.TransformationMode.FastTransformation
        )
    return pixmap
