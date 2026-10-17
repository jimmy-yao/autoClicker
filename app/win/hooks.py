"""全局输入录制（Win32 低级钩子）。

方案选择
--------
用 ``WH_MOUSE_LL`` + ``WH_KEYBOARD_LL`` 低级钩子，而不是：

- ``WH_JOURNALRECORD``：Vista 之后对非提权进程基本失效，已废弃；
- ``GetAsyncKeyState`` 轮询：拿不到精确时序，也无法屏蔽事件；
- 第三方库（``pynput`` / ``keyboard``）：底层还是同一套钩子，白白多一层依赖。

低级钩子**不会注入到其它进程**，回调在「安装钩子的那个线程」上通过消息队列被调用，
所以只需要普通消息循环，不需要写 DLL。Qt 主线程正好在跑消息循环。

三条必须遵守的约束
------------------
1. **回调必须极快**。Windows 有 ``LowLevelHooksTimeout``（默认 300ms），
   回调超时会被系统静默摘掉钩子。因此这里只做「追加一条记录」，其它什么都不干。
2. **必须忽略注入事件**（``LLMHF_INJECTED`` / ``LLKHF_INJECTED``），
   否则会录到本工具自己回放的动作，也可能和其它自动化工具互相打架。
3. **必须持有 HOOKPROC 的引用**。回调是 ctypes 闭包，一旦被 GC，
   系统回调到野指针会直接崩掉进程。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# ---------------------------------------------------------------- 常量
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

LLMHF_INJECTED = 0x00000001
LLKHF_INJECTED = 0x00000010

_BUTTON_DOWN = {
    WM_LBUTTONDOWN: "left",
    WM_RBUTTONDOWN: "right",
    WM_MBUTTONDOWN: "middle",
}
_BUTTON_UP = {
    WM_LBUTTONUP: "left",
    WM_RBUTTONUP: "right",
    WM_MBUTTONUP: "middle",
}
_KEY_DOWN = (WM_KEYDOWN, WM_SYSKEYDOWN)
_KEY_UP = (WM_KEYUP, WM_SYSKEYUP)


# ---------------------------------------------------------------- 结构体
class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE


# ---------------------------------------------------------------- 事件
@dataclass(frozen=True)
class InputEvent:
    """一条原始输入事件。时间戳来自单调时钟，仅用于计算相邻间隔。"""

    kind: str  # "move" | "click" | "key"
    time: float
    x: int = 0
    y: int = 0
    button: str = ""
    vk: int = 0
    is_down: bool = True


class InputRecorder:
    """全局鼠标/键盘录制器。

    线程说明：低级钩子的回调始终在**安装钩子的线程**上执行（即 Qt 主线程），
    所以 ``_events`` 的写入和 ``stop()`` 的读取天然串行，不需要加锁。
    """

    def __init__(self) -> None:
        self._events: list[InputEvent] = []
        self._recording = False
        self._swallow = True
        self._passthrough_vks: set[int] = set()
        self._keys_held: set[int] = set()
        self._action_count = 0
        self._move_count = 0

        self._mouse_hook: int | None = None
        self._key_hook: int | None = None
        # 必须持有引用，否则 ctypes 回调被 GC 后系统回调会踩空指针
        self._mouse_proc: HOOKPROC | None = None
        self._key_proc: HOOKPROC | None = None

    # ---------------------------------------------------------------- 状态
    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def swallow(self) -> bool:
        return self._swallow

    @property
    def action_count(self) -> int:
        """已记录的有效动作数（点击 + 按键），不含鼠标移动。"""
        return self._action_count

    @property
    def move_count(self) -> int:
        return self._move_count

    @property
    def event_count(self) -> int:
        return len(self._events)

    # ---------------------------------------------------------------- 生命周期
    def start(self, swallow: bool = True, passthrough_vks=()) -> bool:
        """开始录制。返回是否成功安装钩子。

        ``swallow=True`` 时会屏蔽鼠标与键盘事件，使其不再传给任何程序，
        因此录制过程不会真的点到东西；代价是必须靠全局热键才能结束录制
        （调用方要先确认该热键注册成功，见 ``MainWindow._start_recording``）。

        ``passthrough_vks`` 里的键不屏蔽，好让 ``RegisterHotKey`` 能收到
        ——被低级钩子拦掉的按键永远不会进入热键分发流程。
        """
        if self._recording:
            return False

        self._events.clear()
        self._keys_held.clear()
        self._action_count = 0
        self._move_count = 0
        self._swallow = bool(swallow)
        self._passthrough_vks = {int(vk) for vk in passthrough_vks}

        self._mouse_proc = HOOKPROC(self._mouse_hook_proc)
        self._key_proc = HOOKPROC(self._key_hook_proc)

        module = _kernel32.GetModuleHandleW(None)
        self._mouse_hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, module, 0)
        self._key_hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._key_proc, module, 0)

        if not self._mouse_hook or not self._key_hook:
            logger.error(
                "安装输入钩子失败: mouse=%s key=%s GetLastError=%s",
                self._mouse_hook,
                self._key_hook,
                ctypes.get_last_error(),
            )
            self._unhook()
            return False

        self._recording = True
        logger.info("开始录制（屏蔽输入=%s）", self._swallow)
        return True

    def stop(self) -> list[InputEvent]:
        """结束录制并返回采集到的事件。"""
        if self._recording:
            logger.info(
                "结束录制: 动作 %s 个, 移动采样 %s 条", self._action_count, self._move_count
            )
        self._recording = False
        self._unhook()
        return list(self._events)

    @property
    def events(self) -> list[InputEvent]:
        return list(self._events)

    def _unhook(self) -> None:
        for attribute in ("_mouse_hook", "_key_hook"):
            handle = getattr(self, attribute)
            if handle:
                _user32.UnhookWindowsHookEx(handle)
                setattr(self, attribute, None)
        # 已摘钩，回调不再会被调用，可以安全释放
        self._mouse_proc = None
        self._key_proc = None

    # ---------------------------------------------------------------- 钩子回调
    # 回调结构：先判断要不要处理，再「尽力记录」（失败只记日志），最后统一决定是否吃掉事件。
    # 这样即使记录逻辑炸了，「录制期间不会误点到东西」这条保证依然成立。
    def _mouse_hook_proc(self, n_code, w_param, l_param):  # noqa: N802 - Win32 命名
        if n_code < 0 or not self._recording:
            return _user32.CallNextHookEx(None, n_code, w_param, l_param)

        message = int(w_param)
        injected = False
        try:
            info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            injected = bool(info.flags & LLMHF_INJECTED)
            if not injected:
                self._handle_mouse(message, int(info.pt.x), int(info.pt.y))
        except Exception:  # noqa: BLE001 - 钩子里绝不能抛异常
            logger.exception("鼠标钩子回调出错")

        if self._swallow and not injected:
            return 1
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _key_hook_proc(self, n_code, w_param, l_param):  # noqa: N802 - Win32 命名
        if n_code < 0 or not self._recording:
            return _user32.CallNextHookEx(None, n_code, w_param, l_param)

        message = int(w_param)
        injected = False
        control_key = False
        try:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)
            injected = bool(info.flags & LLKHF_INJECTED)
            control_key = vk in self._passthrough_vks
            if not injected:
                self._handle_key(message, vk)
        except Exception:  # noqa: BLE001
            logger.exception("键盘钩子回调出错")

        if self._swallow and not injected and not control_key:
            return 1
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    # ---------------------------------------------------------------- 记录
    def _handle_mouse(self, message: int, x: int, y: int) -> None:
        if message == WM_MOUSEMOVE:
            self._move_count += 1
            self._events.append(InputEvent("move", time.perf_counter(), x=x, y=y))
            return

        button = _BUTTON_DOWN.get(message)
        if button is not None:
            self._action_count += 1
            self._events.append(
                InputEvent("click", time.perf_counter(), x=x, y=y, button=button, is_down=True)
            )
            return

        # 抬起事件不单独记录：位置取按下的那一刻，时序用父动作的结束时间表示
        if message in _BUTTON_UP:
            self._events.append(
                InputEvent(
                    "click",
                    time.perf_counter(),
                    x=x,
                    y=y,
                    button=_BUTTON_UP[message],
                    is_down=False,
                )
            )

    def _handle_key(self, message: int, vk: int) -> None:
        # 控制热键（F8/F9/F10/F11）是「操作工具」用的，不是用户宏的一部分，
        # 混进录制结果里会出现「回放到一半自己按 F10 停掉」这种荒唐情况。
        if vk in self._passthrough_vks:
            return

        if message in _KEY_DOWN:
            # 长按会持续产生 WM_KEYDOWN，只记第一次
            if vk in self._keys_held:
                return
            self._keys_held.add(vk)
            self._action_count += 1
            self._events.append(InputEvent("key", time.perf_counter(), vk=vk, is_down=True))
        elif message in _KEY_UP:
            if vk not in self._keys_held:
                return
            self._keys_held.discard(vk)
            self._events.append(InputEvent("key", time.perf_counter(), vk=vk, is_down=False))
