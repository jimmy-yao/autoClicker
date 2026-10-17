"""基于 Win32 ``SendInput`` 的前台输入注入后端。

为什么选 SendInput：
- 属于系统级输入注入，走的是和真实鼠标键盘相同的输入队列，兼容性最好；
- 只需要标准库 ``ctypes``，零第三方依赖；
- 可以一次调用原子地发送「移动 + 按下 + 抬起」，减少被目标程序拆分的可能。

局限（已在 README 说明）：
- 需要目标位置真的在屏幕上、且没有被其他窗口遮挡；
- 对 DirectX/游戏、部分 UWP、做消息校验的程序可能无效；
- 目标程序若以管理员运行，本进程也必须提权，否则注入会被 UIPI 丢弃。

坐标使用 ``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK``，
把虚拟屏幕物理像素归一化到 0..65535，可正确处理多显示器与负坐标。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging

from ..win.geometry import virtual_screen_rect
from .base import InputBackend, MouseButton

logger = logging.getLogger(__name__)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTUNION),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT


def _normalize(x: int, y: int) -> tuple[int, int]:
    """把虚拟屏幕物理像素坐标归一化到 SendInput 要求的 0..65535 区间。"""
    rect = virtual_screen_rect()
    if rect.width <= 1 or rect.height <= 1:  # pragma: no cover - 异常显示配置
        raise RuntimeError(f"虚拟屏幕尺寸非法: {rect}")

    nx = round((x - rect.x) * 65535 / (rect.width - 1))
    ny = round((y - rect.y) * 65535 / (rect.height - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _mouse_input(flags: int, dx: int = 0, dy: int = 0) -> INPUT:
    item = INPUT()
    item.type = INPUT_MOUSE
    item.mi.dx = dx
    item.mi.dy = dy
    item.mi.dwFlags = flags
    return item


def _key_input(vk: int, flags: int = 0) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki.wVk = vk
    item.ki.dwFlags = flags
    return item


def _send(*inputs: INPUT) -> bool:
    """批量发送输入事件，返回是否全部被系统接受。"""
    count = len(inputs)
    array = (INPUT * count)(*inputs)
    sent = _user32.SendInput(count, array, ctypes.sizeof(INPUT))
    if sent != count:
        logger.warning(
            "SendInput 部分失败: 期望 %s 条, 实际 %s 条, GetLastError=%s",
            count,
            sent,
            ctypes.get_last_error(),
        )
        return False
    return True


class SendInputBackend(InputBackend):
    """前台注入后端：点击会作用于光标所在位置的窗口，与窗口焦点无关。"""

    name = "sendinput"

    _BUTTON_FLAGS: dict[MouseButton, tuple[int, int]] = {
        MouseButton.LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        MouseButton.RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        MouseButton.MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }

    def _move(self, x: int, y: int) -> INPUT:
        nx, ny = _normalize(x, y)
        return _mouse_input(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny
        )

    def click(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        down_flag, up_flag = self._BUTTON_FLAGS[MouseButton(button)]
        _send(
            self._move(x, y),
            _mouse_input(down_flag),
            _mouse_input(up_flag),
        )

    def move_to(self, x: int, y: int) -> None:
        _send(self._move(x, y))

    def press_key(self, vk: int) -> None:
        _send(_key_input(vk), _key_input(vk, KEYEVENTF_KEYUP))
