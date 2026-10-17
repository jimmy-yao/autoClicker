"""录制控制。

钩子录制 + 悬浮指示条 + 「事件 → 步骤」的转换与落地选择。

安全约束（本模块存在的主要理由）
--------------------------------
勾选「屏蔽输入」时鼠标键盘不再传给任何程序，**唯一的出口是 F10 全局热键**。
所以热键不可用时宁可拒绝开录，也不能把用户锁在一个点不动的系统里。
录制期间放行的控制热键（F8/F9/F10/F11）也是同一个道理：不放行的话低级钩子会把它们
吃掉，``RegisterHotKey`` 永远收不到。

``ask_how_to_apply`` 故意做成公开方法：自动化测试会替换它，免得弹出没人点的模态框。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QMainWindow, QMessageBox

from ..core.models import Profile, StepKind
from ..core.recording import (
    APPLY_APPEND,
    APPLY_DISCARD,
    APPLY_REPLACE,
    RecordSettings,
    build_profile,
    summarize,
)
from ..win.hooks import InputRecorder
from ..win.hotkey import vk_of
from .record_overlay import RecordOverlay

logger = logging.getLogger(__name__)

# 指示条刷新间隔。钩子回调里绝不能刷 UI（超时会掉钩子），所以用定时器轮询计数。
RECORD_TICK_MS = 200

# 录制时放行这些控制热键，否则它们会被低级钩子吃掉，RegisterHotKey 永远收不到
PASSTHROUGH_KEYS = ("F8", "F9", "F10", "F11")


class RecordingController(QObject):
    """录制一次操作并把它转成步骤。"""

    log = Signal(str)
    status = Signal(str)
    recording_changed = Signal(bool)
    # (Profile, mode) —— 由 MainWindow 转交给 ProfileController 落地
    applied = Signal(object, str)

    def __init__(self, window: QMainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._recorder = InputRecorder()
        self._overlay = RecordOverlay()
        self._active = False
        self._settings = RecordSettings()
        self._name = ""

        self._timer = QTimer(self)
        self._timer.setInterval(RECORD_TICK_MS)
        self._timer.timeout.connect(self._sync_overlay)

    # ---------------------------------------------------------------- 只读
    @property
    def recording(self) -> bool:
        return self._active

    @property
    def overlay(self) -> RecordOverlay:
        return self._overlay

    @property
    def recorder(self) -> InputRecorder:
        """底层录制器。供测试注入假事件、以及调试时查看计数。"""
        return self._recorder

    # ---------------------------------------------------------------- 开始
    def start(
        self,
        *,
        settings: RecordSettings,
        swallow: bool,
        name: str,
        stop_hotkey_ready: bool,
    ) -> bool:
        """开始录制，返回是否真的开始了。被拒绝时一定给出可操作的原因。"""
        if self._active:
            self.log.emit("[提示] 已经在录制中，按 F10 结束")
            return False

        if swallow and not stop_hotkey_ready:
            # 屏蔽模式下鼠标键盘都不通，唯一的出口就是 F10 热键。热键不可用就绝不能开录，
            # 否则用户会被「锁」在一个点不动的系统里。
            logger.error("拒绝开始录制：停止热键不可用")
            self.log.emit("[错误] 无法录制：F10 热键不可用")
            QMessageBox.warning(
                self._window,
                "无法录制",
                "无法以「屏蔽输入」方式录制：F10（结束录制）热键未注册成功。\n\n"
                "屏蔽输入后鼠标键盘都不再传给任何程序，必须能按 F10 才能安全结束。\n"
                "请关闭占用 F10 的程序，或取消勾选「屏蔽输入」后重试。",
            )
            return False

        if not stop_hotkey_ready:
            logger.warning("停止热键 F10 不可用，录制只能从托盘菜单结束")
            self.log.emit("[警告] F10 热键不可用，录制请用托盘菜单的「开始/结束录制」结束")

        passthrough = [vk for vk in (vk_of(key) for key in PASSTHROUGH_KEYS) if vk]
        if not self._recorder.start(swallow=swallow, passthrough_vks=passthrough):
            logger.error("安装输入钩子失败")
            self.log.emit("[错误] 安装输入钩子失败")
            QMessageBox.critical(
                self._window, "无法录制", "安装全局输入钩子失败，无法开始录制。"
            )
            return False

        self._active = True
        self._settings = settings
        self._name = name
        self._timer.start()
        self._overlay.start(swallow=self._recorder.swallow)
        self._window.hide()

        self.recording_changed.emit(True)
        self.status.emit("录制中…")
        mode = "已屏蔽鼠标键盘" if swallow else "正常操作（会真的点到东西）"
        self.log.emit(f"● 开始录制（{mode}），按 F10 结束")
        return True

    # ---------------------------------------------------------------- 结束
    @Slot()
    def stop(self) -> None:
        """结束录制，把结果交给用户决定怎么落地。"""
        if not self._active:
            return
        self._active = False
        self._timer.stop()
        self._overlay.finish()

        events = self._recorder.stop()
        self._restore_window()
        self.recording_changed.emit(False)

        profile = build_profile(events, name=self._name, settings=self._settings)
        if not profile.steps:
            self.status.emit("空闲（本次没有录到动作）")
            self.log.emit("[提示] 没有记录到任何动作")
            QMessageBox.information(
                self._window,
                "录制结束",
                "没有记录到任何动作。\n\n"
                "如果确实操作了鼠标键盘，请确认没有其他程序也在安装输入钩子。",
            )
            return

        summary = summarize(profile)
        self.log.emit(f"● 录制结束，{summary}")

        mode = self.ask_how_to_apply(summary)
        if mode == APPLY_DISCARD:
            self.status.emit("空闲（已丢弃本次录制）")
            self.log.emit("已丢弃本次录制")
            return

        # 交给 ProfileController 落地（直接连接，同线程同步执行）
        self.applied.emit(profile, mode)
        self.status.emit(f"空闲（{summary}）")
        if _count_kind(profile, StepKind.MOVE):
            self.log.emit(
                "[提示] 轨迹步骤已按「一个移动 = 一个步骤」压缩；双击任意一行可微调时长与终点"
            )

    def shutdown(self) -> None:
        """退出流程：丢弃录制中的内容、摘掉钩子、收起指示条。可重复调用。"""
        if self._active:
            self._active = False
            self._timer.stop()
            self._recorder.stop()
        self._overlay.finish()

    # ---------------------------------------------------------------- 弹窗
    def ask_how_to_apply(self, summary: str) -> str:
        """让用户决定录制结果如何并入当前方案。

        做成公开方法（而不是内部 ``_xxx``）是故意的：自动化测试会整段替换它，
        免得弹出一个没人点的模态框把测试挂死。
        """
        box = QMessageBox(self._window)
        box.setWindowTitle("录制结束")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"{summary}\n\n要如何应用这次录制？")
        replace_button = box.addButton("替换当前步骤", QMessageBox.ButtonRole.AcceptRole)
        append_button = box.addButton("追加到末尾", QMessageBox.ButtonRole.ActionRole)
        discard_button = box.addButton("丢弃", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(append_button)
        box.exec()

        clicked = box.clickedButton()
        if clicked is replace_button:
            return APPLY_REPLACE
        if clicked is append_button:
            return APPLY_APPEND
        if clicked is discard_button:
            return APPLY_DISCARD
        return APPLY_DISCARD  # 直接关掉窗口按丢弃处理

    # ---------------------------------------------------------------- 内部
    @Slot()
    def _sync_overlay(self) -> None:
        if self._active:
            self._overlay.set_count(self._recorder.action_count, self._recorder.move_count)

    def _restore_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()


def _count_kind(profile: Profile, kind: StepKind) -> int:
    return sum(1 for step in profile.steps if step.kind is kind)
