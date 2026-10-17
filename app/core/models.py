"""核心数据模型。

坐标一律是虚拟屏幕物理像素。

结构
----
``Profile`` 是一个完整方案：一串 ``Step`` + 外层循环设置。
``Step`` 有五个子类，各自携带自己需要的字段，用 ``kind`` 做区分：

- ``ClickStep`` —— 在指定坐标重复点击
- ``DelayStep`` —— 纯等待
- ``KeyStep``   —— 按键
- ``MoveStep``  —— 沿折线轨迹移动光标
- ``WaitStep``  —— 等屏幕上出现某张图片（找图），出现后可点它

选择「子类」而不是「一个塞满字段的大 dataclass」，是为了让校验和 UI 都能按类型分派，
新增步骤类型时只需加一个子类 + 注册到 ``step_from_dict``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..input.base import MouseButton
from .matching import Template

logger = logging.getLogger(__name__)

# 循环次数填 0 表示无限
INFINITE = 0

# 找图步骤的默认参数
DEFAULT_WAIT_TOLERANCE = 32
DEFAULT_WAIT_TIMEOUT_MS = 30_000
DEFAULT_WAIT_POLL_MS = 300
# 轮询再密就有意义地吃 CPU 了：每轮都要抓一帧整屏
MIN_WAIT_POLL_MS = 20

# 超时后的两种处理
ON_TIMEOUT_CONTINUE = "continue"
ON_TIMEOUT_STOP = "stop"


class StepKind(str, Enum):
    CLICK = "click"
    DELAY = "delay"
    KEY = "key"
    MOVE = "move"
    WAIT = "wait"

    @property
    def label(self) -> str:
        return {
            "click": "点击",
            "delay": "延时",
            "key": "按键",
            "move": "移动",
            "wait": "找图",
        }[self.value]


# ============================================================== 步骤
@dataclass
class Step:
    """步骤基类。子类用无注解的类属性声明 ``kind``，因此它不会成为 dataclass 字段。"""

    enabled: bool = True
    note: str = ""

    def summary(self) -> str:
        """表格里显示的参数摘要。"""
        raise NotImplementedError

    def validate(self) -> None:
        """校验参数，非法时抛 ``ValueError``。"""
        raise NotImplementedError

    @property
    def kind(self) -> StepKind:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind.value,
            "enabled": self.enabled,
            "note": self.note,
        }
        data.update(self._payload())
        return data

    def _payload(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class ClickStep(Step):
    """在 ``(x, y)`` 处重复点击 ``repeat`` 次。"""

    kind = StepKind.CLICK

    x: int = 0
    y: int = 0
    button: MouseButton = MouseButton.LEFT
    repeat: int = 1
    interval_ms: int = 100
    jitter_ms: int = 0
    jitter_px: int = 0

    def __post_init__(self) -> None:
        # 统一成枚举类型。``QComboBox.currentData()`` 会把 str 子类还原成普通 str，
        # 不归一化的话 ``summary()`` 会显示 "left" 而不是 "左键"。
        self.button = MouseButton(self.button)

    def summary(self) -> str:
        text = f"({self.x}, {self.y})  {self.button}  ×{self.repeat}  {self.interval_ms}ms"
        extras = []
        if self.jitter_ms:
            extras.append(f"间隔抖动 ±{self.jitter_ms}ms")
        if self.jitter_px:
            extras.append(f"坐标抖动 ±{self.jitter_px}px")
        if extras:
            text += "  [" + ", ".join(extras) + "]"
        return text

    def validate(self) -> None:
        if self.interval_ms < 1:
            raise ValueError("点击间隔必须大于 0 毫秒")
        if self.repeat < 1:
            raise ValueError("点击次数必须大于等于 1")
        if self.jitter_ms < 0 or self.jitter_px < 0:
            raise ValueError("抖动参数不能为负数")

    def _payload(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "button": MouseButton(self.button).value,
            "repeat": self.repeat,
            "interval_ms": self.interval_ms,
            "jitter_ms": self.jitter_ms,
            "jitter_px": self.jitter_px,
        }


@dataclass
class DelayStep(Step):
    """等待 ``delay_ms`` 毫秒。"""

    kind = StepKind.DELAY

    delay_ms: int = 500
    jitter_ms: int = 0

    def summary(self) -> str:
        text = f"{self.delay_ms} ms"
        if self.jitter_ms:
            text += f"  [抖动 +0~{self.jitter_ms}ms]"
        return text

    def validate(self) -> None:
        if self.delay_ms < 0:
            raise ValueError("延时不能为负数")
        if self.jitter_ms < 0:
            raise ValueError("抖动参数不能为负数")

    def _payload(self) -> dict[str, Any]:
        return {"delay_ms": self.delay_ms, "jitter_ms": self.jitter_ms}


@dataclass
class KeyStep(Step):
    """按下并抬起 ``vk`` 对应的键 ``repeat`` 次。"""

    kind = StepKind.KEY

    vk: int = 0
    repeat: int = 1
    interval_ms: int = 50

    def summary(self) -> str:
        from ..win.hotkey import name_of_vk

        return f"{name_of_vk(self.vk)}  ×{self.repeat}  {self.interval_ms}ms"

    def validate(self) -> None:
        if self.vk <= 0:
            raise ValueError("请选择一个按键")
        if self.repeat < 1:
            raise ValueError("按键次数必须大于等于 1")
        if self.interval_ms < 1:
            raise ValueError("按键间隔必须大于 0 毫秒")

    def _payload(self) -> dict[str, Any]:
        return {"vk": self.vk, "repeat": self.repeat, "interval_ms": self.interval_ms}


@dataclass
class MoveStep(Step):
    """沿一条折线轨迹把光标移到终点。

    为什么要用「一条轨迹 = 一个步骤」而不是「一个点 = 一个步骤」：
    录一段鼠标移动会产生几百个采样点，逐点成步会让步骤表爆炸。
    折线 + 总时长既能还原路径，又只占一行。

    只保留 ``points`` 时是「瞬移」；``points`` 有多个点时按总时长匀速插值。
    """

    kind = StepKind.MOVE

    points: list[tuple[int, int]] = field(default_factory=list)
    duration_ms: int = 300

    def summary(self) -> str:
        if not self.points:
            return "（空轨迹）"
        if len(self.points) == 1:
            x, y = self.points[0]
            return f"移动到 ({x}, {y})"
        sx, sy = self.points[0]
        ex, ey = self.points[-1]
        return f"轨迹 {len(self.points)} 点  ({sx}, {sy}) → ({ex}, {ey})  {self.duration_ms}ms"

    def validate(self) -> None:
        if not self.points:
            raise ValueError("移动轨迹不能为空")
        if self.duration_ms < 0:
            raise ValueError("移动时长不能为负数")

    @property
    def start_point(self) -> tuple[int, int] | None:
        return self.points[0] if self.points else None

    @property
    def end_point(self) -> tuple[int, int] | None:
        return self.points[-1] if self.points else None

    def _payload(self) -> dict[str, Any]:
        return {
            "points": [[int(x), int(y)] for x, y in self.points],
            "duration_ms": self.duration_ms,
        }


@dataclass
class WaitStep(Step):
    """等屏幕上出现某张图片，出现后可以点它。

    这是「条件步骤」的第一块拼图：录制只能记下「什么时候做了什么」，
    记不下「等某个按钮出现再点」；而真实界面经常是先弹窗、再点确认。
    找图补上的正是这一段。

    模板是**像素级**的：目标程序换主题、换语言、改 DPI 缩放后就得重新截。
    换来的好处是完全不依赖目标程序的内部结构（自绘界面、网页、游戏都能用）。
    """

    kind = StepKind.WAIT

    template: str = ""
    """``matching.Template.encode()`` 的结果：base64(zlib(BGRA))。空 = 还没截。"""

    tolerance: int = DEFAULT_WAIT_TOLERANCE
    """单通道允许的最大色差（0-255）。越大越宽容，也越容易误命中。"""

    timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_WAIT_POLL_MS
    click: bool = True
    """找到之后是否顺手点一下（绝大多数场景就是「点确认」）。"""

    button: MouseButton = MouseButton.LEFT
    offset_x: int = 0
    """点击位置相对**模板中心**的偏移，用来微调到底点哪里。"""

    offset_y: int = 0
    on_timeout: str = ON_TIMEOUT_CONTINUE
    """超时后继续下一步（``ON_TIMEOUT_CONTINUE``）还是中止整个任务（``ON_TIMEOUT_STOP``）。"""

    def __post_init__(self) -> None:
        self.button = MouseButton(self.button)

    def decoded(self) -> Template:
        """还原模板。数据坏了会抛 ``ValueError``。"""
        return Template.decode(self.template)

    def summary(self) -> str:
        try:
            size = Template.decode(self.template).summary
        except ValueError:
            return "（未截取模板）"

        action = f"{self.button} 点击" if self.click else "仅继续"
        text = f"{size}  ±{self.tolerance}  ≤{_format_ms(self.timeout_ms)}  → {action}"
        if self.on_timeout == ON_TIMEOUT_STOP:
            text += "  超时中止"
        return text

    def validate(self) -> None:
        if not self.template:
            raise ValueError("请先截取要查找的图片模板")
        try:
            self.decoded()
        except ValueError as exc:
            raise ValueError(f"图片模板不可用：{exc}") from exc
        if not 0 <= self.tolerance <= 255:
            raise ValueError("容差必须在 0-255 之间")
        if self.timeout_ms < 0:
            raise ValueError("超时时间不能为负数")
        if self.poll_interval_ms < MIN_WAIT_POLL_MS:
            raise ValueError(
                f"轮询间隔不能小于 {MIN_WAIT_POLL_MS} 毫秒（每轮都要抓一次整屏）"
            )
        if self.on_timeout not in (ON_TIMEOUT_CONTINUE, ON_TIMEOUT_STOP):
            raise ValueError(f"未知的超时处理方式: {self.on_timeout!r}")
        if self.click and not isinstance(self.button, MouseButton):
            raise ValueError(f"未知的鼠标按键: {self.button!r}")

    def _payload(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "tolerance": self.tolerance,
            "timeout_ms": self.timeout_ms,
            "poll_interval_ms": self.poll_interval_ms,
            "click": self.click,
            "button": MouseButton(self.button).value,
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
            "on_timeout": self.on_timeout,
        }


def _format_ms(value: int) -> str:
    if value == 0:
        return "不等待"
    if value % 1000 == 0:
        return f"{value // 1000}s"
    return f"{value}ms"


def step_from_dict(data: dict[str, Any]) -> Step:
    """从字典还原步骤。未知字段会被忽略，缺失字段用默认值补齐。"""
    if not isinstance(data, dict):
        raise TypeError(f"步骤必须是对象，实际是 {type(data).__name__}")

    kind = StepKind(data.get("kind", StepKind.CLICK.value))
    enabled = bool(data.get("enabled", True))
    note = str(data.get("note", ""))

    if kind is StepKind.CLICK:
        return ClickStep(
            enabled=enabled,
            note=note,
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            button=MouseButton(data.get("button", MouseButton.LEFT.value)),
            repeat=int(data.get("repeat", 1)),
            interval_ms=int(data.get("interval_ms", 100)),
            jitter_ms=int(data.get("jitter_ms", 0)),
            jitter_px=int(data.get("jitter_px", 0)),
        )
    if kind is StepKind.DELAY:
        return DelayStep(
            enabled=enabled,
            note=note,
            delay_ms=int(data.get("delay_ms", 500)),
            jitter_ms=int(data.get("jitter_ms", 0)),
        )
    if kind is StepKind.KEY:
        return KeyStep(
            enabled=enabled,
            note=note,
            vk=int(data.get("vk", 0)),
            repeat=int(data.get("repeat", 1)),
            interval_ms=int(data.get("interval_ms", 50)),
        )
    if kind is StepKind.MOVE:
        return MoveStep(
            enabled=enabled,
            note=note,
            points=_parse_points(data.get("points")),
            duration_ms=int(data.get("duration_ms", 300)),
        )
    if kind is StepKind.WAIT:
        return WaitStep(
            enabled=enabled,
            note=note,
            template=str(data.get("template", "")),
            tolerance=int(data.get("tolerance", DEFAULT_WAIT_TOLERANCE)),
            timeout_ms=int(data.get("timeout_ms", DEFAULT_WAIT_TIMEOUT_MS)),
            poll_interval_ms=int(data.get("poll_interval_ms", DEFAULT_WAIT_POLL_MS)),
            click=bool(data.get("click", True)),
            button=MouseButton(data.get("button", MouseButton.LEFT.value)),
            offset_x=int(data.get("offset_x", 0)),
            offset_y=int(data.get("offset_y", 0)),
            on_timeout=str(data.get("on_timeout", ON_TIMEOUT_CONTINUE)),
        )
    raise ValueError(f"未知的步骤类型: {kind!r}")  # pragma: no cover


def _parse_points(raw: Any) -> list[tuple[int, int]]:
    """解析轨迹点。单个点损坏只跳过它，其余照常还原。"""
    points: list[tuple[int, int]] = []
    if not isinstance(raw, (list, tuple)):
        return points
    for item in raw:
        try:
            px, py = item
            points.append((int(px), int(py)))
        except (TypeError, ValueError):
            logger.warning("跳过无法解析的轨迹点 %r", item)
    return points


# ============================================================== 方案
@dataclass
class Profile:
    """一套完整的自动化方案。"""

    name: str = "默认方案"
    steps: list[Step] = field(default_factory=list)
    repeat: int = INFINITE
    """整个序列重复多少轮，``0`` 表示无限。"""

    repeat_delay_ms: int = 0
    """每轮之间的额外等待。"""

    start_delay_ms: int = 300
    """按下开始后的延迟，给用户留出切换窗口的时间。"""

    # ---------------------------------------------------------------- 便利方法
    @property
    def infinite(self) -> bool:
        return self.repeat == INFINITE

    def active_steps(self) -> list[Step]:
        """只返回被启用的步骤。"""
        return [step for step in self.steps if step.enabled]

    def validate(self, require_steps: bool = True) -> None:
        if self.repeat < 0:
            raise ValueError("重复轮数不能为负数")
        if self.repeat_delay_ms < 0 or self.start_delay_ms < 0:
            raise ValueError("延时参数不能为负数")
        if require_steps and not self.active_steps():
            raise ValueError("方案里没有任何启用的步骤")
        for index, step in enumerate(self.steps, start=1):
            try:
                step.validate()
            except ValueError as exc:
                raise ValueError(f"第 {index} 步（{step.kind.label}）: {exc}") from exc

    # ---------------------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "name": self.name,
            "repeat": self.repeat,
            "repeat_delay_ms": self.repeat_delay_ms,
            "start_delay_ms": self.start_delay_ms,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        steps: list[Step] = []
        for raw in data.get("steps", []):
            try:
                steps.append(step_from_dict(raw))
            except Exception as exc:  # noqa: BLE001 - 单个坏步骤不该拖垮整个方案
                logger.warning("跳过无法解析的步骤 %r: %s", raw, exc)

        return cls(
            name=str(data.get("name", "未命名方案")),
            steps=steps,
            repeat=int(data.get("repeat", INFINITE)),
            repeat_delay_ms=int(data.get("repeat_delay_ms", 0)),
            start_delay_ms=int(data.get("start_delay_ms", 300)),
        )

    @classmethod
    def default(cls) -> "Profile":
        """新用户的初始方案：在主显示器中央点 10 次。

        用主显示器而不是虚拟桌面中心，是因为多屏之间可能有「空隙」，
        居中到空隙上会点不到任何窗口。Windows 上主显示器左上角恒为 (0, 0)。
        """
        import ctypes

        user32 = ctypes.windll.user32
        width = user32.GetSystemMetrics(0)  # SM_CXSCREEN
        height = user32.GetSystemMetrics(1)  # SM_CYSCREEN

        return cls(
            name="默认方案",
            steps=[
                ClickStep(
                    x=width // 2,
                    y=height // 2,
                    repeat=10,
                    interval_ms=200,
                )
            ],
            repeat=INFINITE,
        )
