"""全局热键。

使用 Win32 ``RegisterHotKey`` + ``QAbstractNativeEventFilter`` 拦截 ``WM_HOTKEY``。

选择这套方案而不是键盘钩子（``SetWindowsHookEx`` / 第三方 ``keyboard`` 库）的原因：
- 不需要注入 DLL、不拦截全局按键流，被安全软件误报的概率低得多；
- 系统级注册，窗口失焦时依然生效；
- 无第三方依赖。

限制：热键组合会被系统独占，注册失败说明已被其他程序占用，需要提示用户改键。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# 常用虚拟键码
VK_CODES: dict[str, int] = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "SHIFT": 0x10,
    "CTRL": 0x11,
    "ALT": 0x12,
    "PAUSE": 0x13,
    "CAPSLOCK": 0x14,
    "ESC": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
}
VK_CODES.update({f"F{i}": 0x6F + i for i in range(1, 25)})  # F1=0x70 ... F24=0x87
VK_CODES.update({f"NUM{i}": 0x60 + i for i in range(10)})  # 小键盘
VK_CODES.update({str(i): 0x30 + i for i in range(10)})  # 主键盘数字
VK_CODES.update({chr(c): c for c in range(0x41, 0x5B)})  # A-Z


def vk_of(name: str) -> int | None:
    """把 ``"F9"`` / ``"A"`` 之类的名字转成虚拟键码。"""
    return VK_CODES.get(name.strip().upper())


def name_of_vk(vk: int) -> str:
    """虚拟键码 → 展示用名字。查不到时返回 ``0xNN``。"""
    return _VK_NAMES.get(int(vk), f"0x{int(vk):02X}")


def vk_names() -> list[str]:
    """供下拉框使用的按键名列表（常用键优先）。"""
    return list(_VK_ORDER)


# 供下拉框使用的有序列表：功能键 → 字母 → 数字 → 其余
_F_KEYS = [f"F{i}" for i in range(1, 25)]
_VK_ORDER: list[str] = list(
    dict.fromkeys(_F_KEYS + [chr(c) for c in range(0x41, 0x5B)] + [str(i) for i in range(10)] + list(VK_CODES))
)
_VK_NAMES: dict[int, str] = {code: name for name, code in VK_CODES.items()}


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class HotkeyManager(QObject, QAbstractNativeEventFilter):
    """全局热键管理器。

    必须在 ``QApplication`` 创建之后实例化，并把对象保持存活（否则会被 GC）。
    """

    activated = Signal(int)  # 参数为注册时使用的 hotkey id

    def __init__(self, parent: QObject | None = None) -> None:
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._registered: dict[int, int] = {}
        self._next_id = 1
        self._installed = False

    # ---------------------------------------------------------------- 注册
    def register(self, vk: int, modifiers: int = 0, hotkey_id: int | None = None) -> int | None:
        """注册一个全局热键，成功返回 id，失败返回 ``None``。"""
        hid = hotkey_id if hotkey_id is not None else self._next_id
        if hid in self._registered:
            self.unregister(hid)

        if not ctypes.windll.user32.RegisterHotKey(None, hid, modifiers | MOD_NOREPEAT, vk):
            logger.warning("热键注册失败 (id=%s, vk=0x%02X)，可能已被其他程序占用", hid, vk)
            return None

        self._registered[hid] = vk
        self._next_id = max(self._next_id, hid + 1)
        if not self._installed:
            self.install()
        logger.debug("热键注册成功 id=%s vk=0x%02X", hid, vk)
        return hid

    def register_named(self, name: str, modifiers: int = 0, hotkey_id: int | None = None) -> int | None:
        """按名字（如 ``"F9"``）注册热键。"""
        vk = vk_of(name)
        if vk is None:
            logger.warning("未知热键名: %s", name)
            return None
        return self.register(vk, modifiers, hotkey_id)

    def unregister(self, hotkey_id: int) -> None:
        if hotkey_id in self._registered:
            ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)
            del self._registered[hotkey_id]

    def unregister_all(self) -> None:
        for hid in list(self._registered):
            self.unregister(hid)

    # ------------------------------------------------------------ 事件过滤
    def install(self) -> None:
        """把自身注册为 QApplication 的原生事件过滤器。"""
        if self._installed:
            return
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:  # pragma: no cover - 调用顺序错误
            raise RuntimeError("HotkeyManager 必须在 QApplication 之后创建")
        app.installNativeEventFilter(self)
        self._installed = True

    def nativeEventFilter(self, eventType, message):  # noqa: N802 - Qt 命名
        try:
            event_type = bytes(eventType)
        except TypeError:  # pragma: no cover
            event_type = eventType

        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG))
            if msg.contents.message == WM_HOTKEY:
                hotkey_id = int(msg.contents.wParam)
                self.activated.emit(hotkey_id)
                return True, 0
        return False, 0

    def shutdown(self) -> None:
        """注销全部热键并移除事件过滤器。"""
        self.unregister_all()
        if self._installed:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self)
            self._installed = False
