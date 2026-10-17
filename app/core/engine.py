"""宏执行引擎。

线程模型
--------
``MacroEngine`` 会被 ``moveToThread`` 到一个工作线程，主线程只通过信号与它交互：

- 主线程 → 引擎：``start_requested`` 信号（队列连接）投递方案；
  ``request_stop()`` 直接置位 ``threading.Event``，跨线程调用是安全的。
- 引擎 → 主线程：``started`` / ``step_changed`` / ``progress`` / ``stopped`` / ``failed``
  信号，自动排队回到 UI 线程。

时序策略
--------
用 ``time.perf_counter()``（单调时钟）维护 **下一个绝对触发时刻**，每轮只 sleep 差值，
因此不会像 ``sleep(interval)`` 那样累积漂移。
若因为系统卡顿已经落后太多，则把时间轴重置到当前时刻，避免「追赶风暴」把 CPU 打满。

停止响应
--------
所有等待都走 ``Event.wait()`` 而不是 ``sleep()``，所以 F10 能立刻打断等待，
即使正处在 5 秒的延时步骤里。
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time

from PySide6.QtCore import QObject, Signal, Slot

from ..input.base import InputBackend
from .matching import find_template
from .models import (
    ON_TIMEOUT_STOP,
    ClickStep,
    DelayStep,
    KeyStep,
    MoveStep,
    Profile,
    Step,
    WaitStep,
)

logger = logging.getLogger(__name__)

# 落后超过该阈值（秒）就重新对齐时间轴
LAG_RESET_THRESHOLD = 0.25

# 进度信号的最小间隔（秒）。间隔 1ms 时若每次点击都发信号会淹掉 UI 事件循环
PROGRESS_THROTTLE = 0.05

# 轨迹插值的步长（秒）。太小会淹没输入队列，太大movement会一顿一顿
MOVE_TICK = 0.012


class MacroEngine(QObject):
    """在工作线程中按步骤序列执行方案。"""

    started = Signal()
    step_changed = Signal(int, int)  # 当前步骤序号（1 起）, 启用步骤总数
    progress = Signal(int, int, int)  # 累计动作数, 已完成轮数, 总轮数（0 = 无限）
    stopped = Signal(int)  # 本次运行累计动作数
    failed = Signal(str)  # 不可恢复的错误信息
    wait_timeout = Signal(object)  # 找图超时时的最后一帧（ScreenFrame），供 UI 落盘诊断

    def __init__(
        self,
        backend: InputBackend,
        parent: QObject | None = None,
        screen=None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        # 抓屏函数，默认走 GDI。测试注入假函数就不需要真的有个屏幕
        self._screen = screen
        self._stop_event = threading.Event()
        self._running = False
        self._lock = threading.Lock()

        # 可变状态只在工作线程里读写
        self._actions = 0
        self._rounds_done = 0
        self._rounds_total = 0
        self._last_progress_time = 0.0

    # ------------------------------------------------------------------ 状态
    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ 控制
    @Slot(object)
    def run(self, profile: Profile) -> None:
        """执行方案。本槽由工作线程调用，不要直接在主线程调用。"""
        with self._lock:
            if self._running:
                logger.debug("引擎已在运行，忽略新的启动请求")
                return
            self._running = True

        self._stop_event.clear()
        self._actions = 0
        self._rounds_done = 0
        self._rounds_total = profile.repeat
        self._last_progress_time = 0.0
        self.started.emit()

        steps = profile.active_steps()
        logger.info(
            "开始执行方案「%s」: %s 个启用步骤, 轮数=%s",
            profile.name,
            len(steps),
            "无限" if profile.infinite else profile.repeat,
        )

        try:
            profile.validate()
            self._execute(profile, steps)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要反馈到 UI
            logger.exception("执行方案出错")
            self.failed.emit(str(exc))
        finally:
            with self._lock:
                self._running = False
            self.stopped.emit(self._actions)
            logger.info("执行结束，共完成 %s 次动作", self._actions)

    def request_stop(self) -> None:
        """请求停止。可从任意线程调用。"""
        self._stop_event.set()

    def set_backend(self, backend: InputBackend) -> None:
        """切换输入后端（前台 SendInput / 后台 PostMessage）。

        只在未运行时允许切换：``_backend`` 只在工作线程里被读，运行途中替换会让
        同一次运行混用两种注入方式。UI 侧统一在 start 之前调用，所以碰不到这条限制。
        """
        with self._lock:
            if self._running:
                raise RuntimeError("运行中不能切换输入后端")
            self._backend = backend

    # ------------------------------------------------------------------ 主循环
    def _execute(self, profile: Profile, steps: list[Step]) -> None:
        if profile.start_delay_ms:
            logger.info("启动延迟 %s ms", profile.start_delay_ms)
            if self._stop_event.wait(profile.start_delay_ms / 1000.0):
                return

        total_steps = len(steps)

        while not self._stop_event.is_set():
            for index, step in enumerate(steps, start=1):
                if self._stop_event.is_set():
                    return
                self.step_changed.emit(index, total_steps)
                logger.debug("执行第 %s/%s 步: %s", index, total_steps, step.summary())
                self._run_step(step)

            self._rounds_done += 1
            self._emit_progress(force=True)

            if self._stop_event.is_set():
                return
            if self._rounds_total and self._rounds_done >= self._rounds_total:
                return
            if profile.repeat_delay_ms and self._stop_event.wait(profile.repeat_delay_ms / 1000.0):
                return

    def _run_step(self, step: Step) -> None:
        if isinstance(step, ClickStep):
            self._run_click(step)
        elif isinstance(step, DelayStep):
            self._run_delay(step)
        elif isinstance(step, KeyStep):
            self._run_key(step)
        elif isinstance(step, MoveStep):
            self._run_move(step)
        elif isinstance(step, WaitStep):
            self._run_wait(step)
        else:  # pragma: no cover - 新增步骤类型时忘了加分支
            raise ValueError(f"不支持的步骤类型: {step.kind}")

    def _run_click(self, step: ClickStep) -> None:
        interval = step.interval_ms / 1000.0
        jitter = step.jitter_ms / 1000.0
        next_time = time.perf_counter()
        done = 0

        while done < step.repeat and not self._stop_event.is_set():
            x, y = step.x, step.y
            if step.jitter_px:
                x += random.randint(-step.jitter_px, step.jitter_px)
                y += random.randint(-step.jitter_px, step.jitter_px)

            self._backend.click(x, y, step.button)
            done += 1
            self._actions += 1

            now = time.perf_counter()
            self._emit_progress(now=now, force=done >= step.repeat)
            if done >= step.repeat:
                break

            wait = interval + (random.uniform(0.0, jitter) if jitter else 0.0)
            next_time += wait
            if next_time < now - LAG_RESET_THRESHOLD:
                next_time = now

            remaining = next_time - now
            if remaining > 0 and self._stop_event.wait(remaining):
                break

    def _run_delay(self, step: DelayStep) -> None:
        seconds = step.delay_ms / 1000.0
        if step.jitter_ms:
            seconds += random.uniform(0.0, step.jitter_ms / 1000.0)
        if seconds > 0:
            self._stop_event.wait(seconds)

    def _run_move(self, step: MoveStep) -> None:
        """沿折线轨迹匀速移动光标。

        用「总时长按各段长度比例分配」的方式插值，而不是固定每段多少毫秒，
        这样拐点疏密不同时速度依然均匀，视觉上不会忽快忽慢。
        """
        points = step.points
        if not points:
            return

        self._actions += 1
        self._emit_progress(force=True)

        if len(points) == 1:
            self._backend.move_to(points[0][0], points[0][1])
            return

        lengths = [
            math.hypot(bx - ax, by - ay)
            for (ax, ay), (bx, by) in zip(points, points[1:])
        ]
        total_length = sum(lengths)
        total_seconds = step.duration_ms / 1000.0

        # 没有位移或没给时长：直接落点，不做无意义的插值
        if total_length <= 0 or total_seconds <= 0:
            self._backend.move_to(points[-1][0], points[-1][1])
            return

        begin = time.perf_counter()
        elapsed = 0.0

        for index, segment_length in enumerate(lengths):
            if self._stop_event.is_set():
                return
            (ax, ay), (bx, by) = points[index], points[index + 1]
            segment_seconds = total_seconds * (segment_length / total_length)
            ticks = max(1, int(segment_seconds / MOVE_TICK) + 1)
            step_seconds = segment_seconds / ticks

            for tick in range(1, ticks + 1):
                fraction = tick / ticks
                self._backend.move_to(
                    round(ax + (bx - ax) * fraction),
                    round(ay + (by - ay) * fraction),
                )
                elapsed += step_seconds
                remaining = (begin + elapsed) - time.perf_counter()
                if remaining > 0 and self._stop_event.wait(remaining):
                    return

        self._backend.move_to(points[-1][0], points[-1][1])

    def _run_key(self, step: KeyStep) -> None:
        interval = step.interval_ms / 1000.0
        next_time = time.perf_counter()
        done = 0

        while done < step.repeat and not self._stop_event.is_set():
            self._backend.press_key(step.vk)
            done += 1
            self._actions += 1

            now = time.perf_counter()
            self._emit_progress(now=now, force=done >= step.repeat)
            if done >= step.repeat:
                break

            next_time += interval
            if next_time < now - LAG_RESET_THRESHOLD:
                next_time = now

            remaining = next_time - now
            if remaining > 0 and self._stop_event.wait(remaining):
                break

    def _run_wait(self, step: WaitStep) -> None:
        """等屏幕上出现目标图片，出现后（可选）点它。

        每一轮都是「抓一帧 → 找一次 → 睡到下一个轮询点」，等待用 ``Event.wait``
        分段进行，所以 F10 依然能立刻打断，不会卡在最大 30 秒的等待里。

        超时**只在「中止」模式下**把最后一帧交给 UI 落盘：如果是「继续」模式，
        一个循环里反复超时的等待步骤会写出一堆截图，反而变成噪声。
        """
        template = step.decoded()
        poll = step.poll_interval_ms / 1000.0
        deadline = time.perf_counter() + step.timeout_ms / 1000.0

        while True:
            if self._stop_event.is_set():
                return

            try:
                frame = self._capture()
            except OSError as exc:
                raise RuntimeError(f"抓取屏幕失败：{exc}") from exc

            result = find_template(frame, template, tolerance=step.tolerance)
            match = result.match
            if match is not None:
                x = frame.x + match.x + template.width // 2 + step.offset_x
                y = frame.y + match.y + template.height // 2 + step.offset_y
                logger.info(
                    "找图命中 %s，位置 (%s, %s)，相似度 %.2f",
                    template.summary,
                    x,
                    y,
                    match.similarity,
                )
                if step.click:
                    self._backend.click(x, y, step.button)
                    self._actions += 1
                    self._emit_progress(force=True)
                return

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                self._handle_wait_timeout(step, result, frame)
                return
            if self._stop_event.wait(min(poll, remaining)):
                return

    def _handle_wait_timeout(self, step: WaitStep, result, frame) -> None:
        detail = f"，最接近的相似度只有 {result.best_similarity:.2f}" if result else ""
        message = f"等图片出现超时（{step.timeout_ms} 毫秒内没找到）{detail}"
        if step.on_timeout == ON_TIMEOUT_STOP:
            self.wait_timeout.emit(frame)
            raise TimeoutError(message)
        logger.warning("%s，继续下一步", message)

    # ------------------------------------------------------------------ 抓屏
    def _capture(self):
        """抓一帧整屏。默认走 GDI；测试注入假函数后就不再需要真屏幕。"""
        if self._screen is not None:
            return self._screen()
        from ..win.screen import capture_screen

        return capture_screen()

    # ------------------------------------------------------------------ 进度
    def _emit_progress(self, now: float | None = None, force: bool = False) -> None:
        """按 ``PROGRESS_THROTTLE`` 节流上报进度。"""
        if now is None:
            now = time.perf_counter()
        if not force and now - self._last_progress_time < PROGRESS_THROTTLE:
            return
        self._last_progress_time = now
        self.progress.emit(self._actions, self._rounds_done, self._rounds_total)
