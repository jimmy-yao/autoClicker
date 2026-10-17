"""Windows 集成测试：在**真实的** Win32 窗口上验证后台点击后端。

和 ``tests/test_smoke.py`` 的分工
--------------------------------
``TestPostMessageBackend`` 把 ``post_message`` 与窗口查询函数全换成假的，只验证
「逻辑上发给谁、发什么消息」。本文件反过来：**真的建窗口、真的投消息、真的收消息**，
验证 ``PostMessageBackend`` 在真实 Win32 语义下确实工作。

为什么非有不可
--------------
``app/win/*`` 在 import 阶段就调 ``ctypes.WinDLL``，所以在 Linux / macOS 上整套后台
点击逻辑**一行都执行不到**（``test_smoke.py`` 能跑，是因为它从假函数进去，压根没碰
真实窗口）。本文件把这块补上：自造一个带自定义 ``WndProc`` 的顶层窗口 + 子窗口，
断言点下去之后收到的正是 ``WM_MOUSEMOVE → WM_LBUTTONDOWN → WM_LBUTTONUP``，
投递对象是**最内层子窗口**，``lParam`` 是**客户区坐标**。

用例不依赖任何外部程序（不需要 IDEA、不需要记事本），所以任何 Windows 机器都能跑，
也就能放进 CI（GitHub 的 ``windows-latest``，或自备的 Gitea runner）。

运行
----
::

    python tests\\test_win_integration.py

非 Windows 上整个模块会 skip（退出码 0），不影响其它测试。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

IS_WINDOWS = sys.platform == "win32"

# 非 Windows 上只跳过，不去 import 任何 app.*（app/win 会在 import 时报 WinDLL 不存在）
if IS_WINDOWS:  # pragma: no cover - Windows 分支
    import ctypes
    import ctypes.wintypes as wintypes

    from app.input.base import MouseButton
    from app.input.postmessage import (
        MK_LBUTTON,
        WM_KEYDOWN,
        WM_KEYUP,
        WM_LBUTTONDOWN,
        WM_LBUTTONUP,
        WM_MOUSEMOVE,
        PostMessageBackend,
    )
    from app.win import window as winmod

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _HWND = wintypes.HWND
    _HANDLE = wintypes.HANDLE
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    # ---- 窗口样式 / 消息循环常量
    WS_POPUP = 0x80000000
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_EX_TRANSPARENT = 0x00000020

    PM_REMOVE = 0x0001
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010

    # ---- 原型声明
    # 不写 argtypes 的话，Python int 会按 c_int 传，64 位下句柄会被截断——必须声明。
    _user32.RegisterClassW.argtypes = (ctypes.c_void_p,)
    _user32.RegisterClassW.restype = ctypes.c_ushort
    _user32.UnregisterClassW.argtypes = (wintypes.LPCWSTR, _HANDLE)
    _user32.UnregisterClassW.restype = wintypes.BOOL
    _user32.CreateWindowExW.argtypes = (
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        _HWND,
        _HANDLE,
        _HANDLE,
        ctypes.c_void_p,
    )
    _user32.CreateWindowExW.restype = _HWND
    _user32.DestroyWindow.argtypes = (_HWND,)
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.DefWindowProcW.argtypes = (_HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.DefWindowProcW.restype = LRESULT
    _user32.PeekMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG),
        _HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    )
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    _user32.DispatchMessageW.restype = LRESULT
    _user32.SetWindowPos.argtypes = (
        _HWND,
        _HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    _user32.SetWindowPos.restype = wintypes.BOOL
    _kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    _kernel32.GetModuleHandleW.restype = _HANDLE

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", _HANDLE),
            ("hIcon", _HANDLE),
            ("hCursor", _HANDLE),
            ("hbrBackground", _HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    _MOUSE_MESSAGE_IDS = frozenset(
        {WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, 0x0204, 0x0205, 0x0207, 0x0208}
    )

    def _as_int(value) -> int:
        """ctypes 句柄/整数 → Python int。

        直接 ``int(ctypes.c_ulong(5))`` 并不安全（ctypes 的简单类型未公开
        ``__int__``，会被当成 bytes 去解析而抛 ``ValueError``），所以统一走
        ``.value``——和 ``app.win.window._as_hwnd`` 是同一套写法。
        """
        return int(getattr(value, "value", value))

    def _unpack(lparam: int) -> tuple[int, int]:
        """把消息 ``lParam`` 还原成客户区 ``(x, y)``（低 16 位 x、高 16 位 y）。"""
        return (lparam & 0xFFFF, (lparam >> 16) & 0xFFFF)

    def _mouse_records(window: "_Window") -> list[tuple[int, int, int]]:
        return [record for record in window.messages if record[0] in _MOUSE_MESSAGE_IDS]

    def _drain(limit: int = 4096) -> int:
        """把当前线程消息队列里的消息全部派发掉，返回派发条数。"""
        message = wintypes.MSG()
        dispatched = 0
        while dispatched < limit:
            if not _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                break
            _user32.TranslateMessage(ctypes.byref(message))
            _user32.DispatchMessageW(ctypes.byref(message))
            dispatched += 1
        return dispatched

    def _flush(timeout: float = 2.0) -> int:
        """排空消息队列：一直派发，直到连续两轮都没消息可派。

        ``PostMessage`` 是同线程入队，``click()`` 返回时消息已经在队列里了，
        所以正常情况一轮就够；多留一轮空转是为了防御性地等待任何迟到的消息。
        """
        total = 0
        quiet = 0
        deadline = time.monotonic() + timeout
        while quiet < 2 and time.monotonic() < deadline:
            dispatched = _drain()
            total += dispatched
            quiet = 0 if dispatched else quiet + 1
            if quiet:
                time.sleep(0.005)
        return total

    class _Window:
        """一个真实的 Win32 窗口，所有到达它的消息都记进 ``messages``。

        顶层窗口用 ``WS_POPUP``：没有边框和标题栏，**客户区矩形等于窗口矩形**，
        于是屏幕坐标与客户区坐标的换算可以精确预期，断言不会因为边框宽度飘。
        """

        _serial = 0

        def __init__(
            self,
            *,
            parent: "_Window | None" = None,
            x: int = 30,
            y: int = 30,
            width: int = 200,
            height: int = 150,
            style: int | None = None,
            ex_style: int = 0,
            title: str = "auto-click 集成测试",
        ) -> None:
            _Window._serial += 1
            self.class_name = f"AutoClickIT_{os.getpid()}_{_Window._serial}"
            self.messages: list[tuple[int, int, int]] = []
            self.hwnd: int | None = None
            self._registered = False
            self._instance = _kernel32.GetModuleHandleW(None)

            @WNDPROC
            def _wndproc(hwnd, message, wparam, lparam):
                self.messages.append((_as_int(message), _as_int(wparam), _as_int(lparam)))
                return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

            # 回调必须留引用：被 GC 回收的话下次消息进来会直接把进程打崩
            self._wndproc = _wndproc

            window_class = WNDCLASSW()
            window_class.lpfnWndProc = _wndproc
            window_class.hInstance = self._instance
            window_class.lpszClassName = self.class_name
            if not _user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._registered = True

            if style is None:
                style = (WS_CHILD | WS_VISIBLE) if parent is not None else (WS_POPUP | WS_VISIBLE)

            handle = _user32.CreateWindowExW(
                ex_style,
                self.class_name,
                title,
                style,
                int(x),
                int(y),
                int(width),
                int(height),
                parent.hwnd if parent is not None else None,
                None,
                self._instance,
                None,
            )
            if not handle:
                error = ctypes.get_last_error()
                self.close()
                raise ctypes.WinError(error)
            self.hwnd = _as_int(handle)

        # ------------------------------------------------------------ 生命周期
        def bring_to_front(self) -> None:
            """压到 z 序顶端，保证 ``WindowFromPoint`` 能找到它（CI 里别的窗口可能挡着）。"""
            _user32.SetWindowPos(
                _HWND(self.hwnd),
                ctypes.c_void_p(HWND_TOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )

        def close(self) -> None:
            if self.hwnd is not None:
                _user32.DestroyWindow(_HWND(self.hwnd))
                self.hwnd = None
            if self._registered:
                _user32.UnregisterClassW(self.class_name, self._instance)
                self._registered = False

        # ------------------------------------------------------------ 便利视图
        @property
        def mouse_records(self) -> list[tuple[int, int, int]]:
            return _mouse_records(self)

        @property
        def mouse_messages(self) -> list[int]:
            return [record[0] for record in self.mouse_records]

        @property
        def key_records(self) -> list[tuple[int, int, int]]:
            return [
                record for record in self.messages if record[0] in (WM_KEYDOWN, WM_KEYUP)
            ]


_SKIP_REASON = "Windows 专有：需要真实的 Win32 窗口与消息循环"


class _WindowTestCaseMixin:
    """公共夹具：每个用例都有一块干净的顶层窗口。"""

    def setUp(self) -> None:
        self._windows: list = []
        # 顶层窗口放在 (30, 30)，200x150，客户区原点即屏幕 (30, 30)
        self.top = self._new_window(x=30, y=30, width=200, height=150)

    def tearDown(self) -> None:
        for window in reversed(self._windows):
            try:
                window.close()
            except Exception:  # noqa: BLE001 - 清理失败不该盖住真正的断言失败
                pass
        _drain()

    def _new_window(self, *args, **kwargs):
        window = _Window(*args, **kwargs)
        self._windows.append(window)
        return window

    def _child(self, *, x: int = 20, y: int = 30, width: int = 60, height: int = 40, **kwargs):
        """顶层窗口客户区内 ``(x, y)`` 处的子窗口。"""
        return self._new_window(parent=self.top, x=x, y=y, width=width, height=height, **kwargs)


@unittest.skipUnless(IS_WINDOWS, _SKIP_REASON)
class TestWin32WindowHelpers(_WindowTestCaseMixin, unittest.TestCase):
    """``app.win.window`` 里那些纯 Win32 查询函数，在真窗口上的行为。"""

    def test_client_screen_roundtrip(self):
        """客户区坐标 ↗ 屏幕坐标 ↘ 客户区坐标应原样返回。"""
        screen = winmod.client_to_screen(self.top.hwnd, 12, 34)
        self.assertIsNotNone(screen)
        self.assertEqual(winmod.screen_to_client(self.top.hwnd, *screen), (12, 34))

    def test_popup_client_origin_equals_window_origin(self):
        """WS_POPUP 无边框，客户区原点应正好落在窗口左上角 (30, 30)。"""
        self.assertEqual(winmod.screen_to_client(self.top.hwnd, 30, 30), (0, 0))

    def test_root_window_normalizes_child_to_top(self):
        child = self._child()
        self.assertEqual(
            winmod.root_window(child.hwnd), self.top.hwnd, "子窗口应被归一化到顶层窗口"
        )

    def test_is_window_goes_false_after_destroy(self):
        doomed = self._new_window(x=300, y=300, width=80, height=60)
        handle = doomed.hwnd
        self.assertTrue(winmod.is_window(handle))
        doomed.close()
        self.assertFalse(winmod.is_window(handle), "窗口销毁后 IsWindow 应为假")

    def test_bind_window_rejects_destroyed_handle(self):
        """句柄失效后 ``bind_window`` 必须返回 False，而不是绑定一个死窗口。"""
        doomed = self._new_window(x=300, y=300, width=80, height=60)
        doomed.close()
        backend = PostMessageBackend()
        self.assertFalse(backend.bind_window(doomed.hwnd or 0xDEAD))
        self.assertIsNone(backend.bound_window)

    def test_deepest_child_at_descends_into_child(self):
        """子窗口在客户区 (20, 30)，点 (25, 35) 应下钻到它。"""
        child = self._child()
        self.top.bring_to_front()
        self.assertEqual(
            winmod.deepest_child_at(self.top.hwnd, 30 + 25, 30 + 35),
            child.hwnd,
            "应下钻到该点下最内层的子窗口",
        )

    def test_child_window_from_point_skips_invisible_child(self):
        """不可见子窗口上真实鼠标会穿过去，投递消息也应穿过去。"""
        self._child(style=WS_CHILD)  # 有 WS_CHILD 但不带 WS_VISIBLE
        self.assertEqual(winmod.child_window_from_point(self.top.hwnd, 25, 35), None)
        self.assertEqual(winmod.deepest_child_at(self.top.hwnd, 30 + 25, 30 + 35), self.top.hwnd)

    def test_child_window_from_point_skips_transparent_child(self):
        """WS_EX_TRANSPARENT 的子窗口同样会被默认跳过。"""
        self._child(ex_style=WS_EX_TRANSPARENT)
        self.assertEqual(winmod.child_window_from_point(self.top.hwnd, 25, 35), None)
        self.assertEqual(winmod.deepest_child_at(self.top.hwnd, 30 + 25, 30 + 35), self.top.hwnd)

    def test_child_window_from_point_returns_none_outside_child(self):
        self._child()
        self.assertIsNone(
            winmod.child_window_from_point(self.top.hwnd, 150, 120), "子窗口之外不该命中它"
        )

    def test_describe_mentions_class_name(self):
        self.assertIn(self.top.class_name, winmod.describe(self.top.hwnd))


@unittest.skipUnless(IS_WINDOWS, _SKIP_REASON)
class TestBackendOnRealWindows(_WindowTestCaseMixin, unittest.TestCase):
    """``PostMessageBackend`` 端到端：真的投消息，真的由目标窗口的 ``WndProc`` 收下。"""

    def test_click_reaches_deepest_child_with_client_coords(self):
        """旗舰用例：点击应投给**子窗口**，且 ``lParam`` 是**子窗口的客户区坐标**。"""
        child = self._child(x=20, y=30, width=60, height=40)
        backend = PostMessageBackend()
        self.assertTrue(backend.bind_window(self.top.hwnd))

        # 子窗口屏幕矩形 (50, 60)-(110, 100)；取 (55, 65) → 子窗口客户区 (5, 5)
        backend.click(30 + 25, 30 + 35, MouseButton.LEFT)
        _flush()

        self.assertEqual(
            child.mouse_messages,
            [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP],
            f"子窗口应依次收到移动/按下/抬起；实际收到 {child.mouse_messages!r}",
        )
        self.assertEqual(self.top.mouse_records, [], "消息应直接投给子窗口，不该发给父窗口")

        for record in child.mouse_records:
            self.assertEqual(
                _unpack(record[2]), (5, 5), "lParam 必须是相对收件窗口客户区的坐标"
            )
        self.assertEqual(
            child.mouse_records[1][1], MK_LBUTTON, "按下时 wParam 应带 MK_LBUTTON"
        )
        self.assertEqual(child.mouse_records[2][1], 0, "抬起时 wParam 应为 0")

    def test_click_outside_child_targets_parent(self):
        child = self._child(x=20, y=30, width=60, height=40)
        backend = PostMessageBackend()
        backend.bind_window(self.top.hwnd)

        # (150, 120) → 顶层客户区 (120, 90)，在子窗口之外
        backend.click(150, 120, MouseButton.LEFT)
        _flush()

        self.assertEqual(child.mouse_messages, [], "子窗口之外不该命中子窗口")
        self.assertEqual(
            self.top.mouse_messages, [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP]
        )
        for record in self.top.mouse_records:
            self.assertEqual(_unpack(record[2]), (120, 90))

    def test_click_passes_through_invisible_child_covering_the_point(self):
        """不可见子窗口盖住该点时必须穿透，消息发给父窗口（端到端复现真实命中测试）。"""
        hidden = self._child(x=20, y=30, width=60, height=40, style=WS_CHILD)
        backend = PostMessageBackend()
        backend.bind_window(self.top.hwnd)

        backend.click(30 + 25, 30 + 35, MouseButton.LEFT)
        _flush()

        # 注意：不能断言 hidden.messages 为空——CreateWindowEx 本身就会给窗口送去
        # WM_NCCREATE / WM_NCCALCSIZE / WM_CREATE / WM_SIZE / WM_MOVE。
        # 这里要排除的是「点击」，所以只过滤鼠标消息。
        self.assertEqual(hidden.mouse_records, [], "不可见子窗口不该收到任何鼠标消息")
        self.assertEqual(
            self.top.mouse_messages, [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP]
        )
        for record in self.top.mouse_records:
            self.assertEqual(
                _unpack(record[2]), (25, 35), "穿透后坐标应按父窗口客户区换算"
            )

    def test_right_button_uses_button_specific_messages(self):
        backend = PostMessageBackend()
        backend.bind_window(self.top.hwnd)

        backend.click(100, 100, MouseButton.RIGHT)
        _flush()

        self.assertEqual(self.top.mouse_messages, [WM_MOUSEMOVE, 0x0204, 0x0205])
        self.assertEqual(self.top.mouse_records[1][1], 0x0002, "右键按下应带 MK_RBUTTON")

    def test_move_to_posts_only_mousemove(self):
        backend = PostMessageBackend()
        backend.bind_window(self.top.hwnd)

        backend.move_to(140, 130)
        _flush()

        self.assertEqual(self.top.mouse_messages, [WM_MOUSEMOVE])
        self.assertEqual(_unpack(self.top.mouse_records[0][2]), (110, 100))

    def test_press_key_reaches_bound_window(self):
        """后台键盘：绑定窗口应收到带扫描码的 WM_KEYDOWN / WM_KEYUP。"""
        backend = PostMessageBackend()
        backend.bind_window(self.top.hwnd)

        backend.press_key(0x41)  # 'A'
        _flush()

        records = self.top.key_records
        self.assertEqual([record[0] for record in records], [WM_KEYDOWN, WM_KEYUP])
        self.assertEqual(records[0][1], 0x41, "wParam 应是虚拟键码")
        self.assertEqual(records[1][1], 0x41)

        scancode = winmod.map_virtual_key(0x41)
        self.assertEqual((records[0][2] >> 16) & 0xFF, scancode, "lParam 应带扫描码")
        self.assertEqual(records[0][2] & 0xFFFF, 1, "重复次数应为 1")
        self.assertEqual((records[0][2] >> 30) & 0b11, 0, "按下不该有 bit30/31")
        self.assertEqual((records[1][2] >> 30) & 0b11, 0b11, "抬起应置 bit30/31")

    def test_bind_point_normalizes_to_top_window(self):
        """按点绑定：点在子窗口上，也应归一化绑定到顶层窗口。"""
        child = self._child(x=20, y=30, width=60, height=40)
        self.top.bring_to_front()

        found = winmod.window_from_point(55, 65)
        if found not in {self.top.hwnd, child.hwnd}:
            self.skipTest(
                f"(55, 65) 被别的窗口占用（class={winmod.class_name(found) if found else None}），"
                "这是环境问题，不是被测代码的问题"
            )

        backend = PostMessageBackend()
        self.assertTrue(backend.bind_point(55, 65))
        self.assertEqual(backend.bound_window, self.top.hwnd, "bind_point 应归一化到顶层窗口")
        self.assertIn(self.top.class_name, backend.bound_title)

    def test_unbound_click_falls_back_to_window_under_point(self):
        """没绑定时退化为「按屏幕坐标找当时最顶层的窗口」，而不是直接失灵。"""
        self.top.bring_to_front()
        if winmod.window_from_point(100, 110) != self.top.hwnd:
            self.skipTest("(100, 110) 被别的窗口占用，这是环境问题")

        backend = PostMessageBackend()
        backend.click(100, 110, MouseButton.LEFT)
        _flush()

        self.assertEqual(
            self.top.mouse_messages, [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP]
        )
        self.assertEqual(_unpack(self.top.mouse_records[0][2]), (70, 80))


if __name__ == "__main__":
    unittest.main(verbosity=2)
