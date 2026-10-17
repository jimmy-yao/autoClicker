"""全屏抓图（GDI ``BitBlt`` + ``GetDIBits``）。

为什么不用 Qt 的 ``QScreen.grabWindow(0)``
------------------------------------------
它对「整个虚拟桌面」的解释在不同 Qt 版本 / 平台插件下并不一致。而本项目所有坐标
都锚定在 Win32 的虚拟桌面矩形（``geometry.virtual_screen_rect``）上，
直接走 GDI 拿到的就是 ``SendInput`` 使用的**同一套物理像素**，不存在对不上的可能。

输出格式
--------
自顶向下、32 位、``stride == width * 4`` 的 BGRA 裸字节。

之所以用 32 位而不是 24 位：24 位 BMP 的行要按 4 字节对齐，会引入行填充，
字节级 ``find`` 就可能在行边界上错位。32 位没有填充，``stride`` 恰好是一行的长度，
``matching`` 模块因此可以放心地在整块缓冲区上做扁平搜索。
（``BITMAPINFOHEADER.biHeight`` 传负数即表示自顶向下。）

抓图会短暂占用 GDI 资源，因此所有句柄都在 ``finally`` 里释放；
这类「忘了 DeleteObject 就泄漏」的坑在长时间挂机时是致命的。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
from dataclasses import dataclass

from .geometry import ScreenRect, virtual_screen_rect

logger = logging.getLogger(__name__)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

BYTES_PER_PIXEL = 4

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_gdi32.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.CreateCompatibleBitmap.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
_gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.BitBlt.argtypes = (
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
)
_gdi32.BitBlt.restype = wintypes.BOOL
_gdi32.GetDIBits.argtypes = (
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.c_void_p,
    wintypes.UINT,
)
_gdi32.GetDIBits.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteDC.argtypes = (ctypes.c_void_p,)
_gdi32.DeleteDC.restype = wintypes.BOOL

_user32.GetDC.argtypes = (wintypes.HWND,)
_user32.GetDC.restype = ctypes.c_void_p
_user32.ReleaseDC.argtypes = (wintypes.HWND, ctypes.c_void_p)
_user32.ReleaseDC.restype = ctypes.c_int


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


@dataclass(frozen=True)
class ScreenFrame:
    """一帧屏幕像素。``(x, y)`` 是它在虚拟桌面里的左上角（多屏时可能是负数）。"""

    x: int
    y: int
    width: int
    height: int
    data: bytes

    @property
    def stride(self) -> int:
        return self.width * BYTES_PER_PIXEL

    @property
    def rect(self) -> ScreenRect:
        return ScreenRect(self.x, self.y, self.width, self.height)

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        """返回帧内 ``(x, y)`` 处的 ``(B, G, R)``。越界抛 ``IndexError``。"""
        offset = y * self.stride + x * BYTES_PER_PIXEL
        b, g, r = self.data[offset], self.data[offset + 1], self.data[offset + 2]
        return b, g, r


def capture_screen(region: ScreenRect | None = None) -> ScreenFrame:
    """抓取虚拟桌面（或其中一块）的像素。

    用 GDI 而不是 Qt：拿到的一定是 ``SendInput`` 使用的物理像素。
    失败时抛 ``OSError``。
    """
    area = region or virtual_screen_rect()
    if area.width <= 0 or area.height <= 0:
        raise OSError(f"抓图区域无效: {area}")

    screen_dc = _user32.GetDC(None)
    if not screen_dc:
        raise OSError("GetDC 失败，无法抓取屏幕")

    mem_dc = None
    bitmap = None
    previous = None
    try:
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        if not mem_dc:
            raise OSError("CreateCompatibleDC 失败")

        bitmap = _gdi32.CreateCompatibleBitmap(screen_dc, area.width, area.height)
        if not bitmap:
            raise OSError("CreateCompatibleBitmap 失败")

        previous = _gdi32.SelectObject(mem_dc, bitmap)
        if not _gdi32.BitBlt(
            mem_dc, 0, 0, area.width, area.height, screen_dc, area.x, area.y, SRCCOPY
        ):
            raise OSError("BitBlt 失败，无法抓取屏幕")

        # GetDIBits 要求位图**不在**任何 DC 里，所以先把旧对象选回去
        _gdi32.SelectObject(mem_dc, previous)
        previous = None

        header = BITMAPINFOHEADER(
            biSize=ctypes.sizeof(BITMAPINFOHEADER),
            biWidth=area.width,
            biHeight=-area.height,  # 负数 = 自顶向下，省掉一次翻转
            biPlanes=1,
            biBitCount=32,
            biCompression=BI_RGB,
            biSizeImage=area.width * area.height * BYTES_PER_PIXEL,
        )
        size = area.width * area.height * BYTES_PER_PIXEL
        buffer = ctypes.create_string_buffer(size)
        copied = _gdi32.GetDIBits(
            mem_dc, bitmap, 0, area.height, buffer, ctypes.byref(header), DIB_RGB_COLORS
        )
        if copied != area.height:
            raise OSError(f"GetDIBits 只拷回 {copied}/{area.height} 行")

        return ScreenFrame(
            x=area.x, y=area.y, width=area.width, height=area.height, data=buffer.raw
        )
    finally:
        if previous:
            _gdi32.SelectObject(mem_dc, previous)
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        if mem_dc:
            _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)
