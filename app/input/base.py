"""输入后端的抽象接口。

引擎只依赖这里的接口，因此前台注入（``SendInput``）与后台注入（``PostMessage``）
可以互换而无需改动引擎：换后端只是换一个 ``InputBackend`` 实现。

坐标约定：所有坐标都是**虚拟屏幕物理像素**。前台后端直接用它驱动光标；
后台后端需要先把它换算成目标窗口的客户区坐标（见 ``app.win.window``）。
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

    def reset_notices(self) -> None:
        """每次开始运行之前被调用一次，用于重置「只提示一次」的标记。

        UI 在 ``start`` 之前会调用。依赖它的后端（比如 UIA）因此能保证关键提示
        **每次运行都看得到**，而同一个宏里的成百上千次点击不会把日志刷爆。
        """
