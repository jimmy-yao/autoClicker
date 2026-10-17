"""把录制的原始输入事件转换成方案步骤。

分三个阶段，每一步都保持可读、可单独测试：

1. **事件 → 动作**：把按键/抬起的成对事件收敛成一个动作，把一串鼠标移动采样
   细化成一条折线轨迹。
2. **合并相邻动作**：连续多次「同一按键、同一位置」的点击合成一个 ``repeat``，
   这是「双击」「连点 5 下」这类操作的天然表达，也能显著压缩步骤数量。
3. **动作 → 步骤**：按相邻动作之间的真实间隔插入 ``DelayStep``。

刻意不做的事
------------
- **不插值移动轨迹**（回放时才插值），录制阶段只保留拐点，避免步骤表爆炸；
- **不做「智能等待」**（找图/找色），那是独立能力，见 README 的后续计划。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ..input.base import MouseButton
from ..win.hooks import InputEvent
from .models import ClickStep, DelayStep, KeyStep, MoveStep, Profile, Step

logger = logging.getLogger(__name__)

# 录制出来的方案默认留 500ms 启动延迟：录制时窗口往往是全屏的，
# 回放前用户需要时间把目标窗口切到前台。
RECORDING_START_DELAY_MS = 500

# 录制结果并入当前方案的三种选择。放在这里是为了让两个 UI 控制器共用同一套取值，
# 而不是各自散落字符串字面量。
APPLY_REPLACE = "replace"
APPLY_APPEND = "append"
APPLY_DISCARD = "discard"


@dataclass
class RecordSettings:
    """录制转步骤的可调参数。默认值偏「干净」，宁可少几个点也不要噪音。"""

    record_moves: bool = True
    """是否记录鼠标移动轨迹。关掉后只录点击和按键。"""

    move_min_distance_px: int = 12
    """轨迹抽稀：与上一个保留点的距离达到该值就保留一个点。"""

    move_min_interval_ms: int = 80
    """轨迹抽稀：与上一个保留点的时间差达到该值也保留一个点。"""

    move_min_total_px: int = 5
    """整段位移小于该值就整段丢弃（手抖）。"""

    min_delay_ms: int = 40
    """相邻动作间隔小于该值就不单独生成延时步骤。"""

    merge_click_radius_px: int = 4
    """两次点击的位置差异在该半径内才视为同一个点。"""

    merge_max_gap_ms: int = 400
    """两次点击/按键间隔超过该值就不再合并。"""

    default_click_interval_ms: int = 100
    default_key_interval_ms: int = 60


@dataclass
class _Action:
    """录制事件收敛后的一个动作。``count`` 是合并后的重复次数。"""

    kind: str  # "click" | "key" | "move"
    start: float
    end: float
    count: int = 1
    x: int = 0
    y: int = 0
    button: str = ""
    vk: int = 0
    points: list[tuple[int, int]] = field(default_factory=list)
    interval_ms: int = 0


# ==================================================================== 对外接口
def build_profile(
    events: list[InputEvent],
    name: str = "录制方案",
    settings: RecordSettings | None = None,
) -> Profile:
    """把录制的原始事件转换成可直接回放的方案。"""
    settings = settings or RecordSettings()

    actions = _merge_actions(_to_actions(events, settings), settings)
    steps = _to_steps(actions, settings)

    logger.info(
        "录制转换完成: %s 条原始事件 → %s 个动作 → %s 个步骤",
        len(events),
        len(actions),
        len(steps),
    )
    return Profile(
        name=name,
        steps=steps,
        repeat=1,
        repeat_delay_ms=0,
        start_delay_ms=RECORDING_START_DELAY_MS,
    )


def summarize(profile: Profile) -> str:
    """给用户看的一句话摘要。"""
    counts: dict[str, int] = {}
    for step in profile.steps:
        counts[step.kind.label] = counts.get(step.kind.label, 0) + 1
    if not counts:
        return "没有记录到任何动作"
    detail = "、".join(f"{label} {count} 个" for label, count in counts.items())
    return f"共 {len(profile.steps)} 个步骤（{detail}）"


# ==================================================================== 阶段 1
def _to_actions(events: list[InputEvent], settings: RecordSettings) -> list[_Action]:
    actions: list[_Action] = []
    pending_moves: list[tuple[float, int, int]] = []
    pending_click: InputEvent | None = None
    held_keys: dict[int, float] = {}

    def flush_moves() -> None:
        if not pending_moves:
            return
        samples = pending_moves.copy()
        pending_moves.clear()

        kept = _thin(samples, settings)
        if len(kept) < 2:
            return
        origin_x, origin_y = kept[0][1], kept[0][2]
        if max(math.hypot(s[1] - origin_x, s[2] - origin_y) for s in kept) < settings.move_min_total_px:
            return

        actions.append(
            _Action(
                "move",
                kept[0][0],
                kept[-1][0],
                points=[(s[1], s[2]) for s in kept],
            )
        )

    for event in events:
        if event.kind == "move":
            if settings.record_moves:
                pending_moves.append((event.time, event.x, event.y))
            continue

        if event.kind == "click":
            if event.is_down:
                pending_click = event
                continue
            flush_moves()
            if pending_click is not None:
                actions.append(
                    _Action(
                        "click",
                        pending_click.time,
                        event.time,
                        x=pending_click.x,
                        y=pending_click.y,
                        button=pending_click.button,
                    )
                )
                pending_click = None
            continue

        if event.kind == "key":
            if event.is_down:
                flush_moves()
                held_keys.setdefault(event.vk, event.time)
            else:
                down_time = held_keys.pop(event.vk, None)
                if down_time is not None:
                    actions.append(_Action("key", down_time, event.time, vk=event.vk))
            continue

    flush_moves()

    # 录制结束时还按着的键补一个「瞬时」动作，否则会整段丢掉
    for vk, down_time in held_keys.items():
        actions.append(_Action("key", down_time, down_time, vk=vk))

    return actions


def _thin(samples: list[tuple[float, int, int]], settings: RecordSettings) -> list[tuple[float, int, int]]:
    """按「距离或时间」二选一达到阈值就保留一个采样点，末尾点一定保留。"""
    kept = [samples[0]]
    for sample in samples[1:]:
        last = kept[-1]
        moved = math.hypot(sample[1] - last[1], sample[2] - last[2])
        elapsed_ms = (sample[0] - last[0]) * 1000.0
        if moved >= settings.move_min_distance_px or elapsed_ms >= settings.move_min_interval_ms:
            kept.append(sample)

    if kept[-1] is not samples[-1]:
        kept.append(samples[-1])
    return kept


# ==================================================================== 阶段 2
def _merge_actions(actions: list[_Action], settings: RecordSettings) -> list[_Action]:
    merged: list[_Action] = []
    for action in actions:
        previous = merged[-1] if merged else None
        if previous is not None and _can_merge(previous, action, settings):
            count = previous.count + action.count
            span_ms = (action.start - previous.start) * 1000.0
            # start 始终是第一次的起始时间，因此 span/(count-1) 就是平均间隔
            previous.interval_ms = max(1, round(span_ms / max(1, count - 1)))
            previous.count = count
            previous.end = action.end
        else:
            merged.append(action)
    return merged


def _can_merge(previous: _Action, action: _Action, settings: RecordSettings) -> bool:
    if previous.kind != action.kind:
        return False

    if (action.start - previous.end) * 1000.0 > settings.merge_max_gap_ms:
        return False

    if action.kind == "click":
        return previous.button == action.button and (
            abs(previous.x - action.x) <= settings.merge_click_radius_px
            and abs(previous.y - action.y) <= settings.merge_click_radius_px
        )
    if action.kind == "key":
        return previous.vk == action.vk
    return False  # 轨迹不合并


# ==================================================================== 阶段 3
def _to_steps(actions: list[_Action], settings: RecordSettings) -> list[Step]:
    steps: list[Step] = []
    previous_end: float | None = None

    for action in actions:
        if previous_end is not None:
            gap_ms = (action.start - previous_end) * 1000.0
            if gap_ms >= settings.min_delay_ms:
                steps.append(DelayStep(delay_ms=max(1, round(gap_ms))))
        steps.append(_action_to_step(action, settings))
        previous_end = action.end

    return steps


def _action_to_step(action: _Action, settings: RecordSettings) -> Step:
    if action.kind == "click":
        return ClickStep(
            x=action.x,
            y=action.y,
            button=MouseButton(action.button),
            repeat=action.count,
            interval_ms=action.interval_ms or settings.default_click_interval_ms,
        )
    if action.kind == "key":
        return KeyStep(
            vk=action.vk,
            repeat=action.count,
            interval_ms=action.interval_ms or settings.default_key_interval_ms,
        )
    return MoveStep(
        points=list(action.points),
        duration_ms=max(0, round((action.end - action.start) * 1000.0)),
    )
