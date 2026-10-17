"""基于 Win32 ``PostMessage`` 的**后台**输入后端。

它和 :class:`~app.input.sendinput.SendInputBackend` 的根本区别
------------------------------------------------------------
``SendInput`` 是**前台**注入：它是往系统输入队列里塞真实事件，驱动的是唯一那一个
系统光标，所以必须把目标窗口切到前台，运行期间也不能动鼠标。

本后端改成**往目标窗口的消息队列投递** ``WM_MOUSEMOVE`` / ``WM_LBUTTONDOWN`` 之类的
消息，于是：

- **不移动系统光标**，宏跑着的时候你照样能用鼠标；
- **不要求目标窗口在前台**，部分被遮挡也能点（前提是拿得到它的 HWND）。

代价（``PostMessage`` 机制本身的固有局限，不是 bug）
----------------------------------------------------
- 只对**自己处理鼠标消息**的经典 Win32 窗口有效。Chromium/Electron、DirectX 游戏、
  不少 UWP 会直接忽略投递进来的消息（它们的输入走别的通道），表现为「没反应」。
- 需要**绑定目标窗口**才能把屏幕坐标换算成客户区坐标；没绑定时退化为「按屏幕坐标
  找当时最顶层的窗口」，被遮住就找不准。
- 不会产生 ``WM_ACTIVATE`` / 焦点变化，个别控件要先获得焦点才响应。
- 受 Windows 的 UIPI 限制：低权限进程发往高权限进程（本工具没提权、目标是管理员程序）
  的消息会被拦掉，表现为投递失败或没反应。
- 键盘消息只能投给绑定窗口（或退而求其次给前台窗口）；后台键盘比后台鼠标更不可靠，
  很多程序直接忽略。

消息细节
--------
- 点击前先补一条 ``WM_MOUSEMOVE``，因为不少控件靠它更新悬停/热点状态；
- 命中的目标不是顶尖层窗口，而是**该点下最内层的子窗口**——真实鼠标消息正是由命中
  测试路由到那一层的，只丢给顶层窗口很多程序不会自己再往下转发；
- ``lParam`` 是**客户区坐标**打包成的 ``MAKELPARAM``；按键消息的 ``lParam`` 带上扫描码
  与抬起标志位。
"""

from __future__ import annotations

import logging

from ..win.window import (
    deepest_child_at,
    describe,
    foreground_window,
    is_window,
    map_virtual_key,
    post_message,
    root_window,
    screen_to_client,
    window_from_point,
)
from .base import InputBackend, MouseButton

logger = logging.getLogger(__name__)

# 鼠标消息
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208

# 键盘消息
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

# wParam 里的按键状态位
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

# 按键 → (按下消息, 抬起消息, 按下时的 wParam 状态位)
_BUTTON_MESSAGES: dict[MouseButton, tuple[int, int, int]] = {
    MouseButton.LEFT: (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
    MouseButton.RIGHT: (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
    MouseButton.MIDDLE: (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
}


def _make_lparam(x: int, y: int) -> int:
    """客户区坐标打包进 ``lParam``（低 16 位 x、高 16 位 y）。"""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def _key_lparam(scancode: int, up: bool) -> int:
    """键盘消息的 ``lParam``：重复次数 1 + 扫描码，抬起再置 bit30/31。"""
    value = 1 | ((int(scancode) & 0xFF) << 16)
    if up:
        value |= (1 << 30) | (1 << 31)
    return value


class PostMessageBackend(InputBackend):
    """后台注入后端：向绑定窗口投递消息，不占用系统光标。"""

    name = "postmessage"

    def __init__(self) -> None:
        self._hwnd: int | None = None
        self._title: str = ""
        self._warned_no_target = False

    # ---------------------------------------------------------------- 绑定
    @property
    def bound_window(self) -> int | None:
        return self._hwnd

    @property
    def bound_title(self) -> str:
        """绑定窗口的「标题 [类名]」，未绑定时为空串。"""
        return self._title

    def bind_window(self, hwnd: int | None) -> bool:
        """绑定目标窗口（会归一化到顶层窗口）。

        返回是否绑定成功。句柄失效时返回 ``False`` 且不改动原有绑定。
        """
        if not hwnd or not is_window(hwnd):
            return False
        root = root_window(int(hwnd)) or int(hwnd)
        self._hwnd = root
        self._title = describe(root)
        self._warned_no_target = False
        logger.info("后台点击已绑定窗口: %s (hwnd=0x%X)", self._title, root)
        return True

    def bind_point(self, x: int, y: int) -> bool:
        """绑定屏幕坐标 ``(x, y)`` 处的顶层窗口。找不到窗口时返回 ``False``。"""
        hwnd = window_from_point(x, y)
        if not hwnd:
            return False
        return self.bind_window(hwnd)

    def unbind(self) -> None:
        self._hwnd = None
        self._title = ""
        self._warned_no_target = False

    def close(self) -> None:
        self.unbind()

    # ---------------------------------------------------------------- 目标解析
    def _resolve(self, x: int, y: int) -> tuple[int, int, int] | None:
        """把屏幕坐标解析成 ``(hwnd, client_x, client_y)``，解析不了返回 ``None``。"""
        base = self._hwnd
        if base is None or not is_window(base):
            base = window_from_point(x, y)
            if base is None:
                return None

        hwnd = deepest_child_at(base, x, y)
        client = screen_to_client(hwnd, x, y)
        if client is None:
            return None
        return hwnd, client[0], client[1]

    def _warn_no_target(self) -> None:
        # 一个宏里可能有成百次点击，同一个原因只提醒一次，别刷屏
        if self._warned_no_target:
            return
        self._warned_no_target = True
        logger.warning(
            "后台点击找不到目标窗口：请先「绑定窗口」，或确认该坐标处存在可见窗口"
        )

    # ---------------------------------------------------------------- 注入
    def click(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        target = self._resolve(x, y)
        if target is None:
            self._warn_no_target()
            return

        hwnd, cx, cy = target
        down_message, up_message, pressed = _BUTTON_MESSAGES[MouseButton(button)]
        lparam = _make_lparam(cx, cy)
        # 先移动：不少控件靠 WM_MOUSEMOVE 更新悬停/热点状态，直接按下可能不认
        post_message(hwnd, WM_MOUSEMOVE, 0, lparam)
        post_message(hwnd, down_message, pressed, lparam)
        post_message(hwnd, up_message, 0, lparam)

    def move_to(self, x: int, y: int) -> None:
        target = self._resolve(x, y)
        if target is None:
            return
        hwnd, cx, cy = target
        post_message(hwnd, WM_MOUSEMOVE, 0, _make_lparam(cx, cy))

    def press_key(self, vk: int) -> None:
        hwnd = self._hwnd if is_window(self._hwnd) else foreground_window()
        if not hwnd:
            self._warn_no_target()
            return

        scancode = map_virtual_key(vk)
        post_message(hwnd, WM_KEYDOWN, vk, _key_lparam(scancode, up=False))
        post_message(hwnd, WM_KEYUP, vk, _key_lparam(scancode, up=True))
