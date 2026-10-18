"""UI Automation（无障碍接口）客户端：手写 COM，不依赖第三方库。

为什么手写
----------
本项目的运行期依赖一直只有 PySide6：输入注入、抓屏、钩子全是手写 ctypes。UIA 也不该
破例——多一个第三方包就多一份「打包时漏了 hidden import、对方升个版本就崩」的风险，
而我们要用的 UIA 其实非常小，就这一条链：

    CoCreateInstance(CUIAutomation)
        → ElementFromPoint          拿到坐标处的控件
        → GetCurrentPattern(Invoke) 拿到「Invoke」这个模式
        → Invoke                    执行点击

手写 COM 唯一真正的坑是**虚表下标**：ABI 只认顺序，错一个数就会调到隔壁函数上，而且是
静默的（HRESULT 可能是 0，只是干了别的事）。微软的接口文档页是按**字母序**排的，不能用
来定下标；``VTABLE`` 这张表是从 SDK 的 ``UIAutomationClient.idl`` 里按声明顺序核出来的，
并且 ``tests/test_uia.py`` 逐条钉住。

线程与资源
----------
COM 接口有线程亲和性：谁 ``CoInitializeEx``，就得由谁使用。引擎跑在 Qt 的工作线程里，
所以 :class:`UiaClient` 按线程缓存一份 ``IUIAutomation``，第一次用到时才惰性创建。

接口指针是引用计数的，漏 ``Release`` 会在长时间挂机时慢慢泄漏——本模块里每个接口指针
都在 ``finally`` 里释放，:class:`Element` 同时是上下文管理器。

只用简单出参，不碰 VARIANT
--------------------------
需要的属性里 ``CurrentName``/``CurrentClassName`` 出参是 ``BSTR``、``CurrentProcessId``/
``CurrentControlType`` 是 ``int``、``CurrentNativeWindowHandle`` 是句柄、``CurrentIsOffscreen``
是 ``BOOL``、``CurrentBoundingRectangle`` 直接是 ``RECT``——都不需要构造 ``VARIANT``，
少了一大块最容易写错的编组代码。

非 Windows 上 import
--------------------
``ctypes.WinDLL`` 在非 Windows 上不存在，这里做了保护：纯逻辑（GUID 解析、虚表下标表、
错误码翻译、元素描述）因此能在任何平台上被测试，真正的 COM 调用当然只在 Windows 上有意义。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import functools
import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

HRESULT = ctypes.c_long

# ---------------------------------------------------------------- 常量
S_OK = 0
E_POINTER = -2147467261  # 0x80004003
COINIT_MULTITHREADED = 0x0
CLSCTX_INPROC_SERVER = 0x1
# 本线程已经用别的套间模型初始化过 COM（我们会容忍它，而不是报错）
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

# 控件模式 ID（UIAutomationClient.idl）
UIA_InvokePatternId = 10000

# 控件类型 ID（UIAutomationClient.idl）
UIA_ButtonControlTypeId = 50000
UIA_CheckBoxControlTypeId = 50002
UIA_ComboBoxControlTypeId = 50003
UIA_EditControlTypeId = 50004
UIA_HyperlinkControlTypeId = 50005
UIA_ListItemControlTypeId = 50007
UIA_MenuItemControlTypeId = 50011
UIA_RadioButtonControlTypeId = 50013
UIA_TabItemControlTypeId = 50019
UIA_TextControlTypeId = 50020
UIA_TreeItemControlTypeId = 50024
UIA_CustomControlTypeId = 50025
UIA_GroupControlTypeId = 50026
UIA_DocumentControlTypeId = 50030
UIA_SplitButtonControlTypeId = 50031
UIA_WindowControlTypeId = 50032
UIA_PaneControlTypeId = 50033

CONTROL_TYPE_NAMES: dict[int, str] = {
    50000: "按钮",
    50001: "日历",
    50002: "复选框",
    50003: "下拉框",
    50004: "编辑框",
    50005: "超链接",
    50006: "图片",
    50007: "列表项",
    50008: "列表",
    50009: "菜单",
    50010: "菜单栏",
    50011: "菜单项",
    50012: "进度条",
    50013: "单选按钮",
    50014: "滚动条",
    50015: "滑块",
    50016: "微调器",
    50017: "状态栏",
    50018: "选项卡",
    50019: "选项卡项",
    50020: "文本",
    50021: "工具栏",
    50022: "工具提示",
    50023: "树",
    50024: "树项",
    50025: "自定义控件",
    50026: "分组",
    50027: "滑块柄",
    50028: "数据网格",
    50029: "数据项",
    50030: "文档",
    50031: "拆分按钮",
    50032: "窗口",
    50033: "窗格",
    50034: "表头",
    50035: "表头项",
    50036: "表格",
    50037: "标题栏",
}

# 常见 HRESULT 的人话解释，让日志能直接看懂
HRESULT_NOTES: dict[int, str] = {
    RPC_E_CHANGED_MODE: "本线程已用别的套间模型初始化过 COM",
    -2147220992: "控件当前不可用（禁用状态）",  # UIA_E_ELEMENTNOTENABLED
    -2147220991: "控件已不存在（界面变了 / 窗口关了）",  # UIA_E_ELEMENTNOTAVAILABLE
    -2147220990: "控件没有可点击的点",  # UIA_E_NOCLICKABLEPOINT
    -2147024891: "拒绝访问（权限不足，可能目标以管理员运行）",  # E_ACCESSDENIED
    -2147467259: "调用失败",  # E_FAIL
    -2147221008: "COM 尚未在本线程初始化",  # CO_E_NOTINITIALIZED
}

# ---------------------------------------------------------------- GUID
# COM 的 ABI 按顺序认虚表，GUID 则用来点名要哪个接口。字符串是唯一真源，
# 便于对照 SDK 头文件；解析结果做缓存。
GUIDS: dict[str, str] = {
    "CLSID_CUIAutomation": "ff48dba4-60ef-4201-aa87-54103eef594e",
    "IID_IUIAutomation": "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee",
    "IID_IUIAutomationElement": "d22108aa-8ac5-49a5-837b-37bbb3d7591e",
    "IID_IUIAutomationTreeWalker": "4042c624-389c-4afc-a630-9df854a541fc",
    "IID_IUIAutomationInvokePattern": "fb377fbe-8ea6-46d5-9c73-6499642d3059",
}

# 虚表下标。IUnknown 的 QueryInterface/AddRef/Release 恒为 0/1/2，各接口的方法从 3 开始，
# 顺序 = UIAutomationClient.idl 里的声明顺序（不是文档页的字母序）。
VTABLE: dict[str, int] = {
    "IUnknown.QueryInterface": 0,
    "IUnknown.AddRef": 1,
    "IUnknown.Release": 2,
    # IUIAutomation
    "IUIAutomation.ElementFromPoint": 7,
    "IUIAutomation.ControlViewWalker": 14,
    # IUIAutomationElement
    "IUIAutomationElement.GetCurrentPattern": 16,
    "IUIAutomationElement.CurrentProcessId": 20,
    "IUIAutomationElement.CurrentControlType": 21,
    "IUIAutomationElement.CurrentName": 23,
    "IUIAutomationElement.CurrentClassName": 30,
    "IUIAutomationElement.CurrentNativeWindowHandle": 36,
    "IUIAutomationElement.CurrentIsOffscreen": 38,
    "IUIAutomationElement.CurrentBoundingRectangle": 43,
    # IUIAutomationTreeWalker —— 点到的常常是按钮里的「文本」子节点，
    # 所以需要往上一层层找真正能 Invoke 的那个控件
    "IUIAutomationTreeWalker.GetParentElement": 3,
    # IUIAutomationInvokePattern
    "IUIAutomationInvokePattern.Invoke": 3,
}


class GUID(ctypes.Structure):
    """COM 的 ``GUID`` / ``CLSID`` / ``IID``（同一块内存布局）。"""

    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def parse(cls, text: str) -> "GUID":
        """``xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`` → GUID（两侧的花括号可有可无）。"""
        parts = text.strip().strip("{}").split("-")
        if len(parts) != 5 or [len(p) for p in parts] != [8, 4, 4, 4, 12]:
            raise ValueError(f"不是合法的 GUID: {text!r}")
        tail = bytes.fromhex(parts[3] + parts[4])
        return cls(
            int(parts[0], 16),
            int(parts[1], 16),
            int(parts[2], 16),
            (ctypes.c_ubyte * 8)(*tail),
        )

    def __str__(self) -> str:
        tail = "".join(f"{byte:02x}" for byte in self.Data4)
        return f"{self.Data1:08x}-{self.Data2:04x}-{self.Data3:04x}-{tail[:4]}-{tail[4:]}"


@functools.lru_cache(maxsize=None)
def guid(name: str) -> GUID:
    """按名字取一个 GUID（缓存一份，避免每次调用都重建）。"""
    return GUID.parse(GUIDS[name])


def hresult_note(code: int) -> str:
    """把 HRESULT 翻译成人话，用于日志。"""
    note = HRESULT_NOTES.get(code)
    return f"0x{code & 0xFFFFFFFF:08X}（{note}）" if note else f"0x{code & 0xFFFFFFFF:08X}"


# ---------------------------------------------------------------- 声明
try:
    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    _oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
except (AttributeError, OSError):  # pragma: no cover - 仅非 Windows
    # 非 Windows：不拦在 import 上，好让判定/解析这类纯逻辑仍可被测试
    _ole32 = None
    _oleaut32 = None

OLE_AVAILABLE = _ole32 is not None


def _declare(ole32, oleaut32) -> None:
    """声明用到的 ole32/oleaut32 函数签名。集中一处，方便对照 MSDN。"""
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoUninitialize.argtypes = ()
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = (
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    ole32.CoCreateInstance.restype = HRESULT
    oleaut32.SysFreeString.argtypes = (ctypes.c_void_p,)
    oleaut32.SysFreeString.restype = None


if OLE_AVAILABLE:  # pragma: no cover - 仅 Windows
    _declare(_ole32, _oleaut32)


def _address_of(pointer) -> int:
    """把 ``c_void_p`` / 整数 / ``None`` 统一成 Python int（拿不到就是 0）。"""
    if not pointer:
        return 0
    value = getattr(pointer, "value", pointer)
    return int(value) if value is not None else 0


# COM 的方法一律是 stdcall，Windows 上对应 ``WINFUNCTYPE``。非 Windows 上 Python 不提供
# 它，这里退回 ``CFUNCTYPE`` 只是为了「这一层在任何平台上都能被真的测到」——x64 上两者
# 本来就是同一种调用约定，真机上拿到的永远是 ``WINFUNCTYPE``。
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", None) or ctypes.CFUNCTYPE


def _call(pointer, index: int, restype, argtypes, *args):
    """按虚表下标调用一个 COM 方法。

    ``pointer`` 可以是 ``c_void_p`` 或地址整数；返回值就是方法的返回值
    （调用方自己判断 HRESULT）。

    两处容易写错的地方，都踩过：

    - **接口指针不是虚表**。COM 对象的内存布局是「对象的第一个字段指向虚表」，
      所以要先解一层拿到虚表地址，再按下标取函数地址。少解一层不会报错，只会读到
      野地址——实测「下标 7」读出来是 ``0xfd010303`` 这种垃圾，一调就崩。
    - **函数地址必须是 Python int**。虚表项取出来可能是 ``c_void_p``，直接丢给
      ``CFUNCTYPE`` 是 ``TypeError``（甚至崩进程），所以统一过 :func:`_address_of`。
      ``int(某个 ctypes 对象)`` 也不行——它走缓冲区协议，会把字节当十进制字符串解析
      并对 ``c_void_p`` 抛 ``ValueError``。
    """
    address = _address_of(pointer)
    if not address:
        return E_POINTER
    vtable = _address_of(ctypes.cast(address, ctypes.POINTER(ctypes.c_void_p))[0])
    if not vtable:
        return E_POINTER
    entry = _address_of(ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index])
    if not entry:
        return E_POINTER
    function = _FUNCTYPE(restype, ctypes.c_void_p, *argtypes)(entry)
    return function(ctypes.c_void_p(address), *args)


def _release(pointer) -> None:
    """释放一个 COM 接口指针（空指针/None 时什么都不做）。"""
    address = _address_of(pointer)
    if not address:
        return
    _call(address, VTABLE["IUnknown.Release"], ctypes.c_ulong, [])


def _take_bstr(out: ctypes.c_void_p) -> str:
    """读出一个 ``BSTR`` 出参并立刻释放。

    ``BSTR`` 是 COM 分配的，必须 ``SysFreeString``——这是最常见的泄漏点之一。
    """
    if not out.value:
        return ""
    try:
        return ctypes.wstring_at(out.value)
    finally:
        _oleaut32.SysFreeString(out)
        out.value = None


# ---------------------------------------------------------------- 元素
@dataclass(frozen=True)
class ElementInfo:
    """一个 UIA 元素的只读快照。

    刻意**不**持有 COM 指针：快照可以安全地传出去（写日志、给界面显示），
    不用担心它变成悬垂引用。
    """

    name: str = ""
    control_type: int = 0
    class_name: str = ""
    process_id: int = 0
    native_window: int = 0
    offscreen: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def control_type_name(self) -> str:
        return CONTROL_TYPE_NAMES.get(self.control_type, f"#{self.control_type}")

    def describe(self) -> str:
        """一行话描述这个控件，用于日志。"""
        label = f"『{self.name}』" if self.name else "(无名称)"
        window = f"0x{self.native_window:X}" if self.native_window else "?"
        return (
            f"{label}[{self.control_type_name}] "
            f"class={self.class_name or '?'} pid={self.process_id} hwnd={window}"
        )


class Element:
    """``IUIAutomationElement`` 的薄包装，负责读取属性与释放。"""

    def __init__(self, pointer, client: "UiaClient") -> None:
        self._ptr = pointer
        self._client = client

    # ------------------------------------------------------------ 生命周期
    @property
    def valid(self) -> bool:
        return bool(self._ptr)

    def close(self) -> None:
        if self._ptr:
            _release(self._ptr)
            self._ptr = None

    def __enter__(self) -> "Element":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------ 只读属性
    def _out_int(self, key: str, argtype) -> int | None:
        out = argtype()
        hr = _call(self._ptr, VTABLE[key], HRESULT, [ctypes.POINTER(argtype)], ctypes.byref(out))
        if hr < 0:
            return None
        # 句柄类出参（c_void_p）在「空句柄」时 .value 是 None —— 而这恰恰是常态：
        # 按钮内部的文本节点都没有自己的 HWND。直接 int() 会抛 TypeError。
        value = getattr(out, "value", None)
        return int(value) if value is not None else None

    def _out_text(self, key: str) -> str | None:
        out = ctypes.c_void_p()
        hr = _call(
            self._ptr,
            VTABLE[key],
            HRESULT,
            [ctypes.POINTER(ctypes.c_void_p)],
            ctypes.byref(out),
        )
        if hr < 0:
            _take_bstr(out)  # 失败时也把可能分配出来的 BSTR 放掉
            return None
        return _take_bstr(out)

    def name(self) -> str:
        return self._out_text("IUIAutomationElement.CurrentName") or ""

    def class_name(self) -> str:
        return self._out_text("IUIAutomationElement.CurrentClassName") or ""

    def control_type(self) -> int:
        return self._out_int("IUIAutomationElement.CurrentControlType", ctypes.c_int) or 0

    def process_id(self) -> int:
        return self._out_int("IUIAutomationElement.CurrentProcessId", ctypes.c_int) or 0

    def native_window(self) -> int:
        value = self._out_int("IUIAutomationElement.CurrentNativeWindowHandle", ctypes.c_void_p)
        return value or 0

    def is_offscreen(self) -> bool:
        out = wintypes.BOOL()
        hr = _call(
            self._ptr,
            VTABLE["IUIAutomationElement.CurrentIsOffscreen"],
            HRESULT,
            [ctypes.POINTER(wintypes.BOOL)],
            ctypes.byref(out),
        )
        return bool(out.value) if hr >= 0 else False

    def bounding_rect(self) -> tuple[int, int, int, int]:
        out = wintypes.RECT()
        hr = _call(
            self._ptr,
            VTABLE["IUIAutomationElement.CurrentBoundingRectangle"],
            HRESULT,
            [ctypes.POINTER(wintypes.RECT)],
            ctypes.byref(out),
        )
        if hr < 0:
            return (0, 0, 0, 0)
        return (int(out.left), int(out.top), int(out.right), int(out.bottom))

    def info(self) -> ElementInfo:
        """把常用属性一次读齐（每次点击只读一次，够写日志用）。"""
        return ElementInfo(
            name=self.name(),
            control_type=self.control_type(),
            class_name=self.class_name(),
            process_id=self.process_id(),
            native_window=self.native_window(),
            offscreen=self.is_offscreen(),
            rect=self.bounding_rect(),
        )

    # ------------------------------------------------------------ 树与动作
    def parent(self) -> "Element | None":
        """沿「控件视图」往上走一层（点到的常是按钮内部的文本节点）。"""
        walker = self._client.control_view_walker()
        if not walker:
            return None
        out = ctypes.c_void_p()
        hr = _call(
            walker,
            VTABLE["IUIAutomationTreeWalker.GetParentElement"],
            HRESULT,
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
            self._ptr,
            ctypes.byref(out),
        )
        if hr < 0 or not out.value:
            return None
        return Element(out, self._client)

    def can_invoke(self) -> bool:
        """这个控件支不支持 ``InvokePattern``（按钮/链接/菜单项一般支持）。"""
        pattern = self._invoke_pattern()
        if pattern is None:
            return False
        _release(pattern)
        return True

    def _invoke_pattern(self):
        out = ctypes.c_void_p()
        hr = _call(
            self._ptr,
            VTABLE["IUIAutomationElement.GetCurrentPattern"],
            HRESULT,
            [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
            UIA_InvokePatternId,
            ctypes.byref(out),
        )
        if hr < 0 or not out.value:
            return None
        return out

    def invoke(self) -> bool:
        """调用 ``InvokePattern.Invoke()``：等价于一次点击，但不碰鼠标、不抢焦点。"""
        pattern = self._invoke_pattern()
        if pattern is None:
            return False
        try:
            hr = _call(pattern, VTABLE["IUIAutomationInvokePattern.Invoke"], HRESULT, [])
        finally:
            _release(pattern)
        return hr >= 0


# 点到的常常是按钮内部的「文本」子节点，所以需要往上一层层找真正能 Invoke 的那个。
# 再往上就可能是整个面板了，反而不该乱点，所以给个小上限。
DEFAULT_MAX_PARENT_HOPS = 3


def walk_to_invokable(element, max_hops: int = DEFAULT_MAX_PARENT_HOPS):
    """从 ``element`` 起往上找第一个支持 ``InvokePattern`` 的控件。

    返回 ``(控件, 上了几层)``；找不到时控件为 ``None``、层数为实际走过的层数。

    释放责任：路上经过的**中间**控件在这里就被释放了；``element`` 和返回的那个控件
    （如果它不是 ``element`` 本身）都由**调用方**释放。这样调用方只需要管住两头。

    这段逻辑抽在这里而不是留在后端里，是为了让 ``tools/probe_background.py`` 用的是
    同一份实现——探针说「会点这个控件」，后端跑起来就必须真的点它。
    """
    node = element
    owned = False  # node 是不是我们自己取来的
    hop = 0
    try:
        for hop in range(max(0, int(max_hops)) + 1):
            if node.can_invoke():
                owned = False  # 交给调用方
                return node, hop
            parent = node.parent()
            if owned:
                node.close()
                owned = False
            if parent is None:
                return None, hop
            node = parent
            owned = True
        return None, hop
    finally:
        if owned:
            node.close()


class UiaClient:
    """UIA 的入口，按线程持有一份 ``IUIAutomation``。

    线程亲和：``IUIAutomation`` 只能在 ``CoInitializeEx`` 过的线程上用，所以这里用
    ``threading.local`` 缓存。引擎的工作线程是长期存在的，因此每个线程只初始化一次。

    ``close()`` 释放本线程的那一份；进程退出时不释放也只是「进程级的最后一份引用」，
    不会随点击次数增长。
    """

    def __init__(self) -> None:
        self._local = threading.local()

    # ------------------------------------------------------------ 初始化
    def _state(self) -> tuple[object | None, str]:
        """返回 ``(IUIAutomation 指针, 错误说明)``，失败时指针为 ``None``。

        失败结果也会被缓存：UIA 起不来时不该每次点击都重试一遍 COM 创建。
        """
        cached = getattr(self._local, "state", None)
        if cached is not None:
            return cached
        state = self._create()
        self._local.state = state
        return state

    def _create(self) -> tuple[object | None, str]:
        if not OLE_AVAILABLE:  # pragma: no cover - 仅非 Windows
            return None, "非 Windows 平台，没有 UI Automation"
        hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            return None, f"CoInitializeEx 失败：{hresult_note(hr)}"
        # 只有我们真正初始化成功过，才由我们负责 CoUninitialize
        self._local.com_owned = hr >= 0

        pointer = ctypes.c_void_p()
        hr = _ole32.CoCreateInstance(
            ctypes.byref(guid("CLSID_CUIAutomation")),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(guid("IID_IUIAutomation")),
            ctypes.byref(pointer),
        )
        if hr < 0 or not pointer.value:
            return None, f"CoCreateInstance(CUIAutomation) 失败：{hresult_note(hr)}"
        logger.debug("UIA 客户端已就绪（本线程）")
        return pointer, ""

    @property
    def available(self) -> bool:
        return self._state()[0] is not None

    @property
    def last_error(self) -> str:
        return self._state()[1]

    def close(self) -> None:
        state = getattr(self._local, "state", None)
        if state and state[0]:
            _release(state[0])
        self._local.state = None
        walker = getattr(self._local, "walker", None)
        if walker:
            _release(walker)
        self._local.walker = None
        if getattr(self._local, "com_owned", False):  # pragma: no cover - 仅 Windows
            _ole32.CoUninitialize()
        self._local.com_owned = False

    # ------------------------------------------------------------ 查找
    def control_view_walker(self):
        """取（并缓存）控件视图 TreeWalker，用于往上找父控件。"""
        cached = getattr(self._local, "walker", None)
        if cached:
            return cached
        automation, _error = self._state()
        if automation is None:
            return None
        out = ctypes.c_void_p()
        hr = _call(
            automation,
            VTABLE["IUIAutomation.ControlViewWalker"],
            HRESULT,
            [ctypes.POINTER(ctypes.c_void_p)],
            ctypes.byref(out),
        )
        if hr < 0 or not out.value:
            return None
        self._local.walker = out
        return out

    def element_from_point(self, x: int, y: int) -> Element | None:
        """取屏幕物理坐标 ``(x, y)`` 处的控件。

        注意它和 ``WindowFromPoint`` 一样是**按屏幕取**：该点被别的窗口挡住时，拿到的是
        遮挡者的控件。所以后端在绑定了窗口之后会核对进程号，避免点到别处去。
        """
        automation, _error = self._state()
        if automation is None:
            return None
        point = wintypes.POINT(int(x), int(y))
        out = ctypes.c_void_p()
        hr = _call(
            automation,
            VTABLE["IUIAutomation.ElementFromPoint"],
            HRESULT,
            [wintypes.POINT, ctypes.POINTER(ctypes.c_void_p)],
            point,
            ctypes.byref(out),
        )
        if hr < 0 or not out.value:
            return None
        return Element(out, self)
