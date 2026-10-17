"""方案管理。

把「当前是哪个方案 / 有没有未保存的改动 / 落盘 / 改名 / 删除」收在一处。

界面控件由 MainWindow 建好之后**注入**进来，本模块不参与布局。这样做的取舍是构造函数
参数偏多，但换来两件事：布局仍然只有一处（不会为了改个对齐翻两个文件），以及这个类
碰过哪些控件一目了然。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QSpinBox,
)

from ..core.models import ClickStep, Profile
from ..core.recording import APPLY_REPLACE
from ..storage.config import ProfileStore
from .step_editor import StepEditor

logger = logging.getLogger(__name__)

AUTOSAVE_NAME = "默认方案"


class ProfileController(QObject):
    """当前方案的状态与持久化。"""

    log = Signal(str)
    # (当前方案名, 是否有未保存改动) —— 窗口标题由 MainWindow 拼
    changed = Signal(str, bool)

    def __init__(
        self,
        *,
        store: ProfileStore,
        window: QMainWindow,
        combo: QComboBox,
        editor: StepEditor,
        repeat_spin: QSpinBox,
        round_delay_spin: QSpinBox,
        start_delay_spin: QSpinBox,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._window = window
        self._combo = combo
        self._editor = editor
        self._repeat_spin = repeat_spin
        self._round_delay_spin = round_delay_spin
        self._start_delay_spin = start_delay_spin

        self._name = ""
        self._dirty = False
        # 载入方案时控件会连锁触发 valueChanged，用这个闸门把「装载」和「用户编辑」分开
        self._loading = False

        self._combo.currentIndexChanged.connect(self._on_selected)
        # 「哪里改了就算脏」这条规则只写在这一处：三个运行参数 + 步骤表
        for spin in (repeat_spin, round_delay_spin, start_delay_spin):
            spin.valueChanged.connect(self.mark_dirty)
        editor.changed.connect(self.mark_dirty)

    # ---------------------------------------------------------------- 只读状态
    @property
    def current_name(self) -> str:
        return self._name

    @property
    def effective_name(self) -> str:
        """没名字时给一个兜底名，避免存出空名文件。"""
        return self._name or AUTOSAVE_NAME

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ---------------------------------------------------------------- 启动
    def bootstrap(self) -> None:
        """首次启动时落盘一个默认方案，然后载入最近使用的方案。"""
        names = self._store.list_names()
        if not names:
            default = Profile.default()
            self._store.save(default)
            names = self._store.list_names()
            logger.info("已创建初始方案: %s", default.name)

        self.refresh_combo()

        target = self._store.get_last_profile()
        if target not in names:
            target = names[0] if names else None
        if target:
            self.load(target)

    # ---------------------------------------------------------------- 读写
    def collect(self) -> Profile:
        """从界面收集当前方案。"""
        return Profile(
            name=self.effective_name,
            steps=self._editor.steps(),
            repeat=self._repeat_spin.value(),
            repeat_delay_ms=self._round_delay_spin.value(),
            start_delay_ms=self._start_delay_spin.value(),
        )

    def apply_recording(self, profile: Profile, mode: str) -> None:
        """把录制结果并入当前方案。

        ``mode`` 为 ``APPLY_REPLACE`` 时连运行参数一起换成录制出来的默认值，
        为 ``APPLY_APPEND`` 时只追加步骤、保留用户自己调过的轮数与延迟。
        """
        if mode == APPLY_REPLACE:
            self._editor.set_steps(profile.steps)
            self._repeat_spin.setValue(profile.repeat)
            self._round_delay_spin.setValue(profile.repeat_delay_ms)
            self._start_delay_spin.setValue(profile.start_delay_ms)
            first_new = 0
        else:
            existing = self._editor.steps()
            self._editor.set_steps(existing + profile.steps)
            first_new = len(existing)

        # 光标停在「新录进来的第一行」，用户一眼就能看出东西加在哪
        self._editor.select_row(first_new)
        self.mark_dirty()

    def refresh_combo(self, select: str | None = None) -> None:
        self._loading = True
        try:
            self._combo.clear()
            names = self._store.list_names()
            self._combo.addItems(names)
            target = select or self._name
            index = self._combo.findText(target) if target else -1
            if index < 0 and names:
                index = 0
            if index >= 0:
                self._combo.setCurrentIndex(index)
        finally:
            self._loading = False

    def load(self, name: str) -> None:
        try:
            profile = self._store.load(name)
        except ValueError as exc:
            logger.error("加载方案失败: %s", exc)
            self.log.emit(f"[错误] {exc}")
            QMessageBox.warning(self._window, "加载失败", str(exc))
            return

        self._name = name
        self._apply(profile)
        self._store.set_last_profile(name)
        self._announce()
        self.log.emit(f"已加载方案「{name}」（{len(profile.steps)} 个步骤）")

    # ---------------------------------------------------------------- 增删改
    @Slot()
    def create(self) -> None:
        name, ok = QInputDialog.getText(self._window, "新建方案", "方案名称:", text="新方案")
        if not ok or not name.strip():
            return
        name = name.strip()
        if self._store.exists(name):
            QMessageBox.warning(self._window, "名称冲突", f"方案「{name}」已存在。")
            return

        self.save(silent=True)
        profile = Profile(name=name, steps=[])
        fallback = next(
            (step for step in Profile.default().steps if isinstance(step, ClickStep)), None
        )
        if fallback is not None:
            profile.steps.append(fallback)
        try:
            self._store.save(profile)
        except OSError as exc:
            QMessageBox.critical(self._window, "保存失败", str(exc))
            return

        self._name = name
        self._apply(profile)
        self.refresh_combo(select=name)
        self._store.set_last_profile(name)
        self._announce()
        self.log.emit(f"已新建方案「{name}」")

    def save(self, silent: bool = False) -> bool:
        """保存当前方案（校验失败时只在非静默模式下弹窗）。"""
        if not self._name:
            return self.save_as()

        profile = self.collect()
        try:
            profile.validate(require_steps=False)
        except ValueError as exc:
            if not silent:
                QMessageBox.warning(self._window, "参数错误", f"无法保存：{exc}")
            return False

        try:
            self._store.save(profile)
        except OSError as exc:
            QMessageBox.critical(self._window, "保存失败", str(exc))
            return False

        self.set_dirty(False)
        if not silent:
            self.log.emit(f"方案「{profile.name}」已保存")
        return True

    @Slot()
    def save_as(self) -> None:
        current = self.collect()
        name, ok = QInputDialog.getText(self._window, "另存为", "方案名称:", text=current.name)
        if not ok or not name.strip():
            return
        name = name.strip()
        if self._store.exists(name) and QMessageBox.question(
            self._window,
            "覆盖确认",
            f"方案「{name}」已存在，是否覆盖？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        current.name = name
        try:
            current.validate(require_steps=False)
            self._store.save(current)
        except ValueError as exc:
            QMessageBox.warning(self._window, "参数错误", f"无法保存：{exc}")
            return
        except OSError as exc:
            QMessageBox.critical(self._window, "保存失败", str(exc))
            return

        self._name = name
        self.refresh_combo(select=name)
        self._store.set_last_profile(name)
        self.set_dirty(False)
        self.log.emit(f"方案已另存为「{name}」")

    @Slot()
    def rename(self) -> None:
        if not self._name:
            return
        name, ok = QInputDialog.getText(self._window, "重命名方案", "新名称:", text=self._name)
        if not ok or not name.strip() or name.strip() == self._name:
            return
        name = name.strip()
        if self._store.exists(name):
            QMessageBox.warning(self._window, "名称冲突", f"方案「{name}」已存在。")
            return

        self.save(silent=True)
        if not self._store.rename(self._name, name):
            QMessageBox.critical(self._window, "重命名失败", "无法重命名方案文件。")
            return

        self._name = name
        self.set_dirty(False)
        self.refresh_combo(select=name)
        self._store.set_last_profile(name)
        self.log.emit(f"方案已重命名为「{name}」")

    @Slot()
    def delete(self) -> None:
        if not self._name:
            return
        if QMessageBox.question(
            self._window,
            "删除方案",
            f"确定删除方案「{self._name}」吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        removed = self._name
        self._store.delete(removed)
        self.log.emit(f"方案「{removed}」已删除")

        names = self._store.list_names()
        if not names:
            default = Profile.default()
            self._store.save(default)
            names = self._store.list_names()

        self._name = ""
        self.refresh_combo(select=names[0])
        if names:
            self.load(names[0])

    # ---------------------------------------------------------------- 脏标记
    @Slot()
    def mark_dirty(self) -> None:
        if self._loading:
            return
        self.set_dirty(True)

    def set_dirty(self, dirty: bool) -> None:
        if self._dirty == dirty:
            return
        self._dirty = dirty
        self._announce()

    # ---------------------------------------------------------------- 内部
    def _apply(self, profile: Profile) -> None:
        """把方案渲染到界面。"""
        self._loading = True
        try:
            self._editor.set_steps(profile.steps)
            self._repeat_spin.setValue(profile.repeat)
            self._round_delay_spin.setValue(profile.repeat_delay_ms)
            self._start_delay_spin.setValue(profile.start_delay_ms)
        finally:
            self._loading = False
        self.set_dirty(False)

    def _announce(self) -> None:
        self.changed.emit(self._name, self._dirty)

    @Slot(int)
    def _on_selected(self, index: int) -> None:
        # 切方案前先把当前这份静默存了，避免用户辛苦编辑的内容丢掉
        if self._loading or index < 0:
            return
        name = self._combo.itemText(index)
        if not name or name == self._name:
            return
        self.save(silent=True)
        self.load(name)
