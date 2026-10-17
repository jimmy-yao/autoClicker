"""Win32 窗口查询与坐标换算。

后台点击（``PostMessage``）需要两样 ``SendInput`` 用不到的东西：

1. **目标窗口的句柄（HWND）**：消息得指定收件人；
2. **客户区坐标**：``WM_LBUTTONDOWN`` 的 ``lParam`` 是相对目标窗口客户区的坐标，
   而不是屏幕绝对坐标。

本模块把这层平台细节收在一处，供 ``app.input.postmessage`` 使用。所有函数都是
「查得到就返回、查不到返回 ``None`` / ``False``」的风格，不抛异常——调用方
（输入后端）在拿不到窗口时自行决定如何降级。

坐标语义：本模块仍然只认**虚拟屏幕物理像素**（与 ``geometry`` 一致），
换算交给 ``ScreenToClient``，与进程的 DPI 感知级别无关。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging

logger = logging.getLogger(__name__)

# GetAncestor
GA_PARENT = 1
GA_ROOT = 2
GA_ROOTOWNER = 3

# ChildWindowFromPointEx
CWP_ALL = 0x0000
CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
CWP_SKIPTRANSPARENT = 0x0004

# MapVirtualKey
MAPVK_VK_TO_VSC = 0

# 消息投递是最多下钻这么多层子窗口就停手，避免畸形窗口树把循环拖死
MAX_CHILD_DEPTH = 16

HWND = wintypes.HWND

_user32 = ctypes.WinDLL("user32", use_last_error=True)

_user32.IsWindow.argtypes = (wintypes.HWND,)
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = (wintypes.HWND,)
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.WindowFromPoint.argtypes = (wintypes.POINT,)
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.ChildWindowFromPointEx.argtypes = (wintypes.HWND, wintypes.POINT, wintypes.UINT)
_user32.ChildWindowFromPointEx.restype = wintypes.HWND
_user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = ()
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.ScreenToClient.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
_user32.ScreenToClient.restype = wintypes.BOOL
_user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
_user32.ClientToScreen.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_user32.PostMessageW.restype = wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetClassNameW.restype = ctypes.c_int

_user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
_user32.MapVirtualKeyW.restype = wintypes.UINT


def _as_hwnd(value) -> int | None:
    """把 ctypes 句柄（``c_void_p``）或 int 归一成 Python int；空句柄返回 ``None``。

    ``wintypes.HWND`` 是 ``c_void_p``，``int()`` 它并不成立（会去解析内存内容），
    必须走 ``.value``。
    """
    if not value:
        return None
    raw = getattr(value, "value", value)
    return int(raw) if raw else None


def is_window(hwnd: int | None) -> bool:
    """句柄是否指向一个仍然存在的窗口。"""
    if not hwnd:
        return False
    return bool(_user32.IsWindow(HWND(hwnd)))


def is_window_visible(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    return bool(_user32.IsWindowVisible(HWND(hwnd)))


def window_from_point(x: int, y: int) -> int | None:
    """屏幕坐标处**最顶层**的窗口（可能是子窗口）。找不到返回 ``None``。

    注意它只看得到当前可见、位于该点的窗口；被完全遮住的窗口谁也找不到，
    那种情况必须先手动绑定 HWND。
    """
    return _as_hwnd(_user32.WindowFromPoint(wintypes.POINT(int(x), int(y))))


def root_window(hwnd: int) -> int | None:
    """取顶层窗口（``GA_ROOT``），用于把绑定的目标归一化到稳定的顶层句柄。"""
    return _as_hwnd(_user32.GetAncestor(HWND(int(hwnd)), GA_ROOT)) or int(hwnd)


def child_window_from_point(
    hwnd: int, x: int, y: int, flags: int = CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT
) -> int | None:
    """``hwnd`` 客户区坐标 ``(x, y)`` 处的直接子窗口，没有子窗口时返回 ``None``。

    默认跳过不可见与 ``WS_EX_TRANSPARENT`` 的子窗口——真实鼠标会直接穿过它们，
    投递消息时也应该穿过去。
    """
    found = _as_hwnd(
        _user32.ChildWindowFromPointEx(HWND(int(hwnd)), wintypes.POINT(int(x), int(y)), flags)
    )
    if found is None or found == int(hwnd):
        return None
    return found


def deepest_child_at(hwnd: int, screen_x: int, screen_y: int) -> int:
    """从 ``hwnd`` 出发，逐层下钻到屏幕坐标处最内层的子窗口。

    真正的鼠标消息由命中测试路由到最内层控件，因此投递时也应该发到那一层；
    只发给顶层窗口，很多程序不会自己再往下转发。
    """
    current = int(hwnd)
    for _ in range(MAX_CHILD_DEPTH):
        client = screen_to_client(current, screen_x, screen_y)
        if client is None:
            break
        child = child_window_from_point(current, client[0], client[1])
        if child is None:
            break
        current = child
    return current


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int] | None:
    """屏幕物理像素 → ``hwnd`` 的客户区坐标。失败返回 ``None``。"""
    point = wintypes.POINT(int(x), int(y))
    if not _user32.ScreenToClient(HWND(int(hwnd)), ctypes.byref(point)):
        return None
    return int(point.x), int(point.y)


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int] | None:
    """``hwnd`` 的客户区坐标 → 屏幕物理像素。失败返回 ``None``。"""
    point = wintypes.POINT(int(x), int(y))
    if not _user32.ClientToScreen(HWND(int(hwnd)), ctypes.byref(point)):
        return None
    return int(point.x), int(point.y)


def foreground_window() -> int | None:
    return _as_hwnd(_user32.GetForegroundWindow())


def map_virtual_key(vk: int) -> int:
    """虚拟键码 → 扫描码，失败返回 0（键盘消息的 ``lParam`` 里会用到）。"""
    try:
        return int(_user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC))
    except OSError:  # pragma: no cover - 仅在异常系统上触发
        return 0


def post_message(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> bool:
    """向窗口消息队列投递一条消息（``PostMessageW``，异步、不阻塞）。

    失败（窗口已销毁、队列已满等）返回 ``False``，不抛异常。
    """
    if not hwnd:
        return False
    return bool(_user32.PostMessageW(HWND(int(hwnd)), int(message), int(wparam), int(lparam)))


def window_text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(HWND(int(hwnd)))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(HWND(int(hwnd)), buffer, length + 1)
    return buffer.value


def class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(HWND(int(hwnd)), buffer, 256)
    return buffer.value


def describe(hwnd: int) -> str:
    """给用户看的窗口描述，形如 ``记事本 [Notepad]``。"""
    title = window_text(hwnd) or "(无标题)"
    return f"{title} [{class_name(hwnd)}]"
