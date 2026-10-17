"""执行控制。

把 ``MacroEngine`` 搬到工作线程，并把它报出来的进度翻译成「状态文案 + 进度条」。

**只做执行这一件事**：方案从哪来、要不要先保存、窗口要不要最小化、按了什么键触发、
按钮该不该置灰——都不归它管。那些是 MainWindow 的策略，这里不碰。

界面上唯一属于它的控件是进度条（纯执行进度）。状态文案通过信号抛出去，
因为「状态」这块牌子同时也被录制流程使用，由 MainWindow 统一持有。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QProgressBar

from ..core.engine import MacroEngine
from ..core.models import Profile
from ..input.base import InputBackend
from .shot import save_frame

logger = logging.getLogger(__name__)

# 退出时等工作线程收工的上限；超时只记日志，不阻塞退出
SHUTDOWN_WAIT_MS = 3000


class RunController(QObject):
    """驱动宏引擎跑一个方案，并汇报进度。"""

    log = Signal(str)
    # 状态栏正文（不含「状态: 」前缀，那是视图的事）
    status = Signal(str)
    started = Signal()
    stopped = Signal(int)
    failed = Signal(str)

    # 主线程 → 工作线程：投递方案（队列连接，跨线程安全）
    _run_requested = Signal(object)

    def __init__(
        self,
        backend: InputBackend,
        progress: QProgressBar,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._progress = progress
        self._actions = 0
        self._rounds_done = 0
        self._rounds_total = 0
        self._step_index = 0
        self._step_total = 0

        self._thread = QThread(self)
        self._thread.setObjectName("MacroEngineThread")

        self._engine = MacroEngine(backend)
        self._engine.moveToThread(self._thread)

        self._run_requested.connect(self._engine.run)
        self._engine.started.connect(self._on_started)
        self._engine.step_changed.connect(self._on_step_changed)
        self._engine.progress.connect(self._on_progress)
        self._engine.stopped.connect(self._on_stopped)
        self._engine.failed.connect(self._on_failed)
        self._engine.wait_timeout.connect(self._on_wait_timeout)

        self._thread.start()

    # ---------------------------------------------------------------- 对外
    @property
    def running(self) -> bool:
        return self._engine.running

    def start(self, profile: Profile) -> None:
        """投递一个**已经校验通过**的方案给工作线程。"""
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._run_requested.emit(profile)

    def stop(self) -> None:
        """请求停止。立刻返回，不等工作线程收工。"""
        self._engine.request_stop()

    def set_backend(self, backend: InputBackend) -> None:
        """切换输入后端（前台 SendInput / 后台 PostMessage）。

        供 MainWindow 在 ``start`` 之前按界面上的选择调用。
        """
        self._engine.set_backend(backend)

    def shutdown(self) -> None:
        """停引擎 + 回收线程。退出流程用，可以重复调用。"""
        try:
            self._engine.request_stop()
            self._thread.quit()
            if not self._thread.wait(SHUTDOWN_WAIT_MS):
                logger.warning("工作线程未在 %s ms 内退出", SHUTDOWN_WAIT_MS)
        except Exception:  # noqa: BLE001 - 退出流程不允许抛异常
            logger.exception("关闭引擎时出错")

    # ---------------------------------------------------------------- 引擎回调
    @Slot()
    def _on_started(self) -> None:
        self._actions = 0
        self._rounds_done = 0
        self._step_index = 0
        self._step_total = 0
        self.started.emit()
        self.status.emit("运行中")
        self.log.emit("▶ 开始执行")

    @Slot(int, int)
    def _on_step_changed(self, index: int, total: int) -> None:
        self._step_index = index
        self._step_total = total
        self._progress.setRange(0, total)
        self._progress.setValue(index)
        self._emit_progress_status()

    @Slot(int, int, int)
    def _on_progress(self, actions: int, rounds_done: int, rounds_total: int) -> None:
        self._actions = actions
        self._rounds_done = rounds_done
        self._rounds_total = rounds_total
        self._emit_progress_status()

    @Slot(int)
    def _on_stopped(self, actions: int) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._actions = actions
        self.status.emit(f"空闲（本次共执行 {actions} 次动作）")
        self.log.emit(f"■ 已停止，本次共执行 {actions} 次动作")
        # 放在最后：让订阅方看到进度条/状态都已经归档完毕
        self.stopped.emit(actions)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.log.emit(f"[错误] {message}")
        self.failed.emit(message)

    @Slot(object)
    def _on_wait_timeout(self, frame) -> None:
        """找图超时中止：把最后一帧落盘。

        这类失败光看日志没法判断「是模板过期了还是弹窗根本没出现」，
        一张当时的屏幕截图比什么描述都管用。
        """
        path = save_frame(frame)
        if path is not None:
            self.log.emit(f"已把失败时的屏幕截图存到 {path}")

    def _emit_progress_status(self) -> None:
        parts = ["运行中"]
        if self._step_total:
            parts.append(f"第 {self._step_index}/{self._step_total} 步")
        if self._rounds_total:
            parts.append(f"第 {self._rounds_done + 1}/{self._rounds_total} 轮")
        else:
            parts.append(f"已完成 {self._rounds_done} 轮")
        parts.append(f"共 {self._actions} 次动作")
        self.status.emit(" · ".join(parts))
