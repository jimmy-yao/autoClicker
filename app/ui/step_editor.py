"""步骤序列编辑器。

一个表格 + 一排按钮：增 / 改 / 删 / 上移 / 下移。

设计取舍
--------
- 表格整体是**只读**的（``NoEditTriggers``），所有改动都走对话框。
  这样每种步骤类型可以有自己的字段表单，不用写一堆自定义 delegate；
  只有「启用」列是内联可勾选的。
- 编辑器只负责一份 ``list[Step]``，不关心持久化和执行，方便单独测试。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import ClickStep, DelayStep, KeyStep, MoveStep, Step, StepKind, WaitStep
from ..win.geometry import get_cursor_pos, virtual_screen_rect
from ..win.hotkey import vk_of
from .position_picker import PickCoordinator
from .step_dialog import StepDialog

logger = logging.getLogger(__name__)

COL_ENABLED = 0
COL_KIND = 1
COL_SUMMARY = 2
COL_NOTE = 3

_ROW_COLORS = {
    StepKind.CLICK: None,
    StepKind.DELAY: "#e8a33d",
    StepKind.KEY: "#3da35d",
    StepKind.MOVE: "#7d6cf5",
    StepKind.WAIT: "#e0619b",
}


class StepEditor(QWidget):
    """步骤列表。``changed`` 在任何内容变动后发出。"""

    changed = Signal()

    def __init__(self, picker: PickCoordinator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._picker = picker
        self._steps: list[Step] = []
        self._populating = False
        self._build_ui()

    # ================================================================ 对外接口
    def set_steps(self, steps: list[Step]) -> None:
        """整体替换步骤列表。"""
        self._steps = list(steps)
        self._refresh()

    def steps(self) -> list[Step]:
        """返回步骤列表的副本（浅拷贝，元素仍是原对象）。"""
        return list(self._steps)

    def current_row(self) -> int:
        return self._table.currentRow()

    def step_at(self, row: int) -> Step | None:
        if 0 <= row < len(self._steps):
            return self._steps[row]
        return None

    def select_row(self, row: int) -> None:
        if 0 <= row < self._table.rowCount():
            self._table.selectRow(row)

    def update_click_coords(self, row: int, x: int, y: int) -> bool:
        """把拾取到的坐标写回某一行。

        点击步骤改的是坐标；移动步骤改的是**终点**（起点保留，中间拐点拉直），
        这样「选中轨迹行 → F8 重新瞄一下」也能用。
        """
        step = self.step_at(row)
        if isinstance(step, ClickStep):
            step.x = x
            step.y = y
        elif isinstance(step, MoveStep):
            step.points = ([step.points[0]] if step.points else []) + [(x, y)]
            step.validate()
        else:
            return False
        self._refresh()
        self.select_row(row)
        self.changed.emit()
        return True

    # ================================================================ UI 构建
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        root.addLayout(self._build_toolbar())

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["启用", "类型", "参数", "备注"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.doubleClicked.connect(lambda _index: self.edit_current())
        self._table.itemSelectionChanged.connect(self._sync_buttons)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_ENABLED, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_KIND, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_SUMMARY, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_NOTE, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(COL_ENABLED, 48)
        self._table.setColumnWidth(COL_KIND, 56)
        self._table.setColumnWidth(COL_NOTE, 150)

        root.addWidget(self._table, 1)

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self._add_click_button = QPushButton("+ 点击")
        self._add_click_button.clicked.connect(lambda: self.add_step(StepKind.CLICK))
        self._add_delay_button = QPushButton("+ 延时")
        self._add_delay_button.clicked.connect(lambda: self.add_step(StepKind.DELAY))
        self._add_key_button = QPushButton("+ 按键")
        self._add_key_button.clicked.connect(lambda: self.add_step(StepKind.KEY))
        self._add_move_button = QPushButton("+ 移动")
        self._add_move_button.clicked.connect(lambda: self.add_step(StepKind.MOVE))
        self._add_wait_button = QPushButton("+ 找图")
        self._add_wait_button.setToolTip(
            "等屏幕上出现某张图片再继续（等弹窗、等按钮亮起）。\n"
            "可以顺手点一下找到的位置，超时就按设置的策略处理。"
        )
        self._add_wait_button.clicked.connect(lambda: self.add_step(StepKind.WAIT))

        self._edit_button = QPushButton("编辑")
        self._edit_button.clicked.connect(self.edit_current)
        self._remove_button = QPushButton("删除")
        self._remove_button.clicked.connect(self.remove_selected)
        self._up_button = QPushButton("↑")
        self._up_button.setFixedWidth(34)
        self._up_button.clicked.connect(lambda: self.move_selected(-1))
        self._down_button = QPushButton("↓")
        self._down_button.setFixedWidth(34)
        self._down_button.clicked.connect(lambda: self.move_selected(1))

        for widget in (
            self._add_click_button,
            self._add_delay_button,
            self._add_key_button,
            self._add_move_button,
            self._add_wait_button,
            self._edit_button,
            self._remove_button,
        ):
            bar.addWidget(widget)
        bar.addStretch(1)
        bar.addWidget(self._up_button)
        bar.addWidget(self._down_button)
        return bar

    # ================================================================ 步骤操作
    def add_step(self, kind: StepKind) -> None:
        """新增一个步骤，插入到当前行之后。"""
        edited = self._open_dialog(self._make_default(kind))
        if edited is None:
            return

        row = self.current_row()
        insert_at = row + 1 if row >= 0 else len(self._steps)
        self._steps.insert(insert_at, edited)
        self._refresh()
        self.select_row(insert_at)
        self.changed.emit()

    def edit_current(self) -> None:
        row = self.current_row()
        step = self.step_at(row)
        if step is None:
            return

        edited = self._open_dialog(step)
        if edited is None:
            return

        self._steps[row] = edited
        self._refresh()
        self.select_row(row)
        self.changed.emit()

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self._table.selectedIndexes()}, reverse=True)
        if not rows:
            return

        if QMessageBox.question(
            self,
            "删除步骤",
            f"确定删除选中的 {len(rows)} 个步骤吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        for row in rows:
            if 0 <= row < len(self._steps):
                del self._steps[row]

        self._refresh()
        if self._steps:
            self.select_row(min(rows[-1], len(self._steps) - 1))
        self.changed.emit()

    def move_selected(self, delta: int) -> None:
        row = self.current_row()
        target = row + delta
        if row < 0 or not (0 <= target < len(self._steps)):
            return

        self._steps[row], self._steps[target] = self._steps[target], self._steps[row]
        self._refresh()
        self.select_row(target)
        self.changed.emit()

    # ================================================================ 内部
    def _make_default(self, kind: StepKind) -> Step:
        if kind is StepKind.CLICK:
            rect = virtual_screen_rect()
            return ClickStep(
                x=rect.x + rect.width // 2,
                y=rect.y + rect.height // 2,
                repeat=1,
                interval_ms=200,
            )
        if kind is StepKind.DELAY:
            return DelayStep(delay_ms=500)
        if kind is StepKind.MOVE:
            return MoveStep(points=[get_cursor_pos()], duration_ms=300)
        if kind is StepKind.WAIT:
            return WaitStep()
        return KeyStep(vk=vk_of("F5") or 0, repeat=1, interval_ms=50)

    def _open_dialog(self, step: Step) -> Step | None:
        dialog = StepDialog(step, self._picker, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.result_step()
        return None

    def _refresh(self) -> None:
        """按 ``self._steps`` 重建表格。"""
        self._populating = True
        try:
            self._table.setRowCount(len(self._steps))
            for row, step in enumerate(self._steps):
                enabled_item = QTableWidgetItem()
                enabled_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                )
                enabled_item.setCheckState(
                    Qt.CheckState.Checked if step.enabled else Qt.CheckState.Unchecked
                )
                enabled_item.setToolTip("取消勾选可临时跳过该步骤")
                self._table.setItem(row, COL_ENABLED, enabled_item)

                kind_item = QTableWidgetItem(step.kind.label)
                color = _ROW_COLORS.get(step.kind)
                if color:
                    kind_item.setForeground(QColor(color))
                self._table.setItem(row, COL_KIND, kind_item)

                self._table.setItem(row, COL_SUMMARY, QTableWidgetItem(step.summary()))

                note_item = QTableWidgetItem(step.note)
                if not step.enabled:
                    note_item.setToolTip("该步骤已被停用")
                self._table.setItem(row, COL_NOTE, note_item)
        finally:
            self._populating = False

        self._sync_buttons()

    def _sync_buttons(self) -> None:
        """按当前选择状态启用/禁用按钮。"""
        rows = {index.row() for index in self._table.selectedIndexes()}
        has_selection = bool(rows)
        row = self.current_row()

        self._edit_button.setEnabled(len(rows) == 1)
        self._remove_button.setEnabled(has_selection)
        self._up_button.setEnabled(len(rows) == 1 and row > 0)
        self._down_button.setEnabled(len(rows) == 1 and 0 <= row < len(self._steps) - 1)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._populating:
            return

        row = item.row()
        step = self.step_at(row)
        if step is None:
            return

        if item.column() == COL_ENABLED:
            step.enabled = item.checkState() == Qt.CheckState.Checked
            self.changed.emit()
        elif item.column() == COL_NOTE:
            step.note = item.text()
            self.changed.emit()
