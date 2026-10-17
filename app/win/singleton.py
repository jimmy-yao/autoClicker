"""单实例守卫（命名互斥量）。

第二次启动时 ``acquire()`` 返回 False，调用方应提示用户并退出。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    """基于命名互斥量的进程单实例守卫。

    互斥量句柄必须保持存活，因此调用方需要持有本对象的引用。
    """

    def __init__(self, name: str = r"Local\AutoClicker.SingleInstance.v1") -> None:
        self._name = name
        self._handle: int | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """尝试获取实例锁。已存在其他实例时返回 False。"""
        if self._handle is not None:
            return True

        handle = _kernel32.CreateMutexW(None, False, self._name)
        last_error = ctypes.get_last_error()

        if not handle:  # pragma: no cover - 极端情况下才发生
            logger.error("CreateMutexW 失败, GetLastError=%s", last_error)
            return False

        if last_error == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False

        self._handle = handle
        return True

    def release(self) -> None:
        """释放实例锁。"""
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
