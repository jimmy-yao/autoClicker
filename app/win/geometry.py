"""坐标与屏幕几何。

全项目统一使用 **虚拟屏幕物理像素坐标**：
- 原点在虚拟桌面左上角，多显示器时可能是负数；
- 单位是物理像素（因为入口已声明 Per-Monitor V2 DPI 感知）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from dataclasses import dataclass

# GetSystemMetrics 索引
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_user32 = ctypes.windll.user32


@dataclass(frozen=True)
class ScreenRect:
    """虚拟桌面矩形（物理像素）。"""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"({self.x}, {self.y}, {self.width}x{self.height})"


def virtual_screen_rect() -> ScreenRect:
    """返回覆盖全部显示器的虚拟桌面矩形。"""
    return ScreenRect(
        x=_user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        y=_user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        width=_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        height=_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def get_cursor_pos() -> tuple[int, int]:
    """获取当前鼠标位置（虚拟屏幕物理像素）。"""
    point = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(point)):
        return 0, 0
    return int(point.x), int(point.y)


def set_cursor_pos(x: int, y: int) -> bool:
    """直接设置鼠标位置（不产生点击）。"""
    return bool(_user32.SetCursorPos(int(x), int(y)))
