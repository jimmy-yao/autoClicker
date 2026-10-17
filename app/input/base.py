"""输入后端的抽象接口。

引擎只依赖这里的接口，因此未来新增后台点击（PostMessage）后端时无需改动引擎。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class MouseButton(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"

    def __str__(self) -> str:  # pragma: no cover - 仅用于展示
        return {"left": "左键", "right": "右键", "middle": "中键"}[self.value]


class InputBackend(ABC):
    """输入注入后端。所有坐标均为虚拟屏幕物理像素。"""

    name: str = "base"

    @abstractmethod
    def click(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        """把光标移动到 (x, y) 并在该位置完成一次按下 + 抬起。"""

    @abstractmethod
    def move_to(self, x: int, y: int) -> None:
        """仅移动光标，不产生按键。"""

    @abstractmethod
    def press_key(self, vk: int) -> None:
        """按下并抬起一个虚拟键码对应的键。"""

    def close(self) -> None:
        """释放后端占用的资源（默认无操作）。"""
