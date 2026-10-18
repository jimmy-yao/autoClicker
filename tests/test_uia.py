"""UIA 后端与 COM 客户端的测试。

为什么能在这里跑
----------------
``app/win/uia.py`` 对 ``ctypes.WinDLL`` 做了保护，``app/input/uia.py`` 又把
``app.win.window`` 的 import 放进了方法里（那个模块在 import 阶段就调 ``WinDLL``）。
于是这两层的**逻辑**在任何平台上都能被真的执行——判错方向、漏释放 COM 指针这类问题
就不用等到真机上才发现。

``app.win.window`` 是用 ``sys.modules`` 注入的假模块顶掉的；因为 ``UiaBackend`` 是在
方法内部才 import 它，注入是有效的。

真窗口那一层（真的 ``ElementFromPoint``、真的 ``Invoke``）没法在这里验：那要求
目标程序真的暴露无障碍树，只能靠 ``tools/probe_background.py`` 在真机上跑。

运行
----
::

    python tests/test_uia.py
"""

from __future__ import annotations

import ctypes
import logging
import sys
import types
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.input.base import MouseButton  # noqa: E402
from app.input.uia import UiaBackend  # noqa: E402
from app.win import uia as uia_module  # noqa: E402
from app.win.uia import (  # noqa: E402
    CONTROL_TYPE_NAMES,
    GUID,
    UIA_ButtonControlTypeId,
    Element,
    ElementInfo,
    UiaClient,
    hresult_note,
)


# ================================================================ 假件
class FakeElement:
    """假 UIA 元素：记录被 Invoke 与被释放的次数。"""

    def __init__(
        self,
        *,
        name: str = "继续",
        process_id: int = 4242,
        invokable: bool = True,
        invoke_ok: bool = True,
        control_type: int = UIA_ButtonControlTypeId,
    ) -> None:
        self._info = ElementInfo(
            name=name,
            control_type=control_type,
            class_name="Fake",
            process_id=process_id,
            native_window=0x1,
        )
        self._invokable = invokable
        self._invoke_ok = invoke_ok
        self._parent: FakeElement | None = None
        self.invoked = 0
        self.closed = 0

    def info(self) -> ElementInfo:
        return self._info

    def can_invoke(self) -> bool:
        return self._invokable

    def invoke(self) -> bool:
        self.invoked += 1
        return self._invoke_ok

    def parent(self) -> "FakeElement | None":
        return self._parent

    def close(self) -> None:
        self.closed += 1


def link(*elements: FakeElement) -> FakeElement:
    """把一串元素用 parent 串起来，返回最深的那个。"""
    for child, parent in zip(elements, elements[1:]):
        child._parent = parent
    return elements[0]


class FakeClient:
    """假 UIA 客户端。"""

    def __init__(self, element: FakeElement | None = None) -> None:
        self.element = element
        self.asked: list[tuple[int, int]] = []
        self.closed = 0
        self.available = True
        self.last_error = ""

    def element_from_point(self, x: int, y: int) -> FakeElement | None:
        self.asked.append((x, y))
        return self.element

    def close(self) -> None:
        self.closed += 1


class FakeFallback:
    """前台后端的替身，记录被回落调用的操作。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def click(self, x, y, button=MouseButton.LEFT) -> None:
        self.calls.append(("click", (x, y, MouseButton(button))))

    def move_to(self, x, y) -> None:
        self.calls.append(("move_to", (x, y)))

    def press_key(self, vk) -> None:
        self.calls.append(("press_key", (vk,)))


def fake_window_module(**overrides):
    """顶掉 ``app.win.window`` 的假模块（UiaBackend 只在方法里 import 它）。"""
    module = types.ModuleType("app.win.window")
    module.is_window = lambda hwnd: True
    module.root_window = lambda hwnd: hwnd
    module.process_id = lambda hwnd: 4242
    module.describe = lambda hwnd: "记事本 [Notepad]"
    module.window_from_point = lambda x, y: 0x1234
    for key, value in overrides.items():
        setattr(module, key, value)
    return module


class FakeComObject:
    """一个假的 COM 对象：虚表里放**真回调**，用来端到端驱动 ``_call``。

    它只被**地址**引用（虚表里存的是回调地址，「对象」里存的是虚表地址），所以
    **必须由调用方一直持有**——被回收之后那块内存就还回去了，再调用轻则读到 0、
    重则段错误。``address`` 才是 :func:`_call` 要的「接口指针」。
    """

    def __init__(self, methods: dict[int, tuple], slots: int = 64) -> None:
        # (下标 -> (restype, argtypes, 实现函数))，实现函数的第一个参数是 this
        self._keep_alive: list[object] = []
        self._vtable = (ctypes.c_void_p * slots)()
        for index, (restype, argtypes, implementation) in methods.items():
            callback = uia_module._FUNCTYPE(restype, ctypes.c_void_p, *argtypes)(implementation)
            self._keep_alive.append(callback)
            self._vtable[index] = ctypes.cast(callback, ctypes.c_void_p).value
        self._object = (ctypes.c_void_p * 1)()
        self._object[0] = ctypes.addressof(self._vtable)

    @property
    def address(self) -> int:
        return ctypes.addressof(self._object)


# ================================================================ COM 客户端
class TestGuid(unittest.TestCase):
    """GUID 是 COM 用来点名接口的，写错就 `CoCreateInstance` 直接失败。"""

    def test_round_trip(self):
        text = "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
        self.assertEqual(str(GUID.parse(text)), text)

    def test_braces_are_tolerated(self):
        self.assertEqual(str(GUID.parse("{ff48dba4-60ef-4201-aa87-54103eef594e}")),
                         "ff48dba4-60ef-4201-aa87-54103eef594e")

    def test_declared_guids_match_their_text(self):
        for name, text in uia_module.GUIDS.items():
            with self.subTest(guid=name):
                self.assertEqual(str(uia_module.guid(name)), text)

    def test_garbage_is_rejected(self):
        for bad in ("", "not-a-guid", "30cbe57d-d9d0-452a-ab13", "zzzzzzzz-d9d0-452a-ab13-7ac5ac4825ee"):
            with self.subTest(text=bad):
                with self.assertRaises(ValueError):
                    GUID.parse(bad)


class TestVtable(unittest.TestCase):
    """虚表下标。

    这张表是从 SDK 的 ``UIAutomationClient.idl`` 按**声明顺序**核出来的（微软的接口
    文档页是按字母序排的，定不了下标）。COM 的 ABI 只认顺序：错一个数就会静默调到隔壁
    函数上，所以这里逐条钉住，改表必须是有意识的。
    """

    EXPECTED = {
        "IUnknown.QueryInterface": 0,
        "IUnknown.AddRef": 1,
        "IUnknown.Release": 2,
        "IUIAutomation.ElementFromPoint": 7,
        "IUIAutomation.ControlViewWalker": 14,
        "IUIAutomationElement.GetCurrentPattern": 16,
        "IUIAutomationElement.CurrentProcessId": 20,
        "IUIAutomationElement.CurrentControlType": 21,
        "IUIAutomationElement.CurrentName": 23,
        "IUIAutomationElement.CurrentClassName": 30,
        "IUIAutomationElement.CurrentNativeWindowHandle": 36,
        "IUIAutomationElement.CurrentIsOffscreen": 38,
        "IUIAutomationElement.CurrentBoundingRectangle": 43,
        "IUIAutomationTreeWalker.GetParentElement": 3,
        "IUIAutomationInvokePattern.Invoke": 3,
    }

    def test_indices_are_exactly_as_derived_from_the_idl(self):
        self.assertEqual(uia_module.VTABLE, self.EXPECTED)

    def test_every_interface_starts_after_iunknown(self):
        for key, index in uia_module.VTABLE.items():
            if key.startswith("IUnknown."):
                continue
            with self.subTest(method=key):
                self.assertGreaterEqual(index, 3, "IUnknown 占 0/1/2，其余方法从 3 开始")

    def test_release_is_two_on_every_interface(self):
        self.assertEqual(uia_module.VTABLE["IUnknown.Release"], 2)


class TestHresultNotes(unittest.TestCase):
    def test_known_code_is_translated(self):
        self.assertIn("套间", hresult_note(uia_module.RPC_E_CHANGED_MODE))

    def test_unknown_code_still_prints_hex(self):
        self.assertEqual(hresult_note(0), "0x00000000")

    def test_negative_codes_print_as_unsigned_hex(self):
        self.assertEqual(hresult_note(uia_module.RPC_E_CHANGED_MODE), "0x80010106（本线程已用别的套间模型初始化过 COM）")


class TestElementInfo(unittest.TestCase):
    def test_control_type_name(self):
        info = ElementInfo(control_type=UIA_ButtonControlTypeId)
        self.assertEqual(info.control_type_name, "按钮")

    def test_unknown_control_type_falls_back_to_number(self):
        self.assertEqual(ElementInfo(control_type=12345).control_type_name, "#12345")

    def test_describe_carries_the_useful_bits(self):
        text = ElementInfo(
            name="继续", control_type=UIA_ButtonControlTypeId,
            class_name="SunAwtCanvas", process_id=1234, native_window=0xAB,
        ).describe()
        self.assertIn("继续", text)
        self.assertIn("按钮", text)
        self.assertIn("SunAwtCanvas", text)
        self.assertIn("1234", text)

    def test_describe_handles_missing_name(self):
        self.assertIn("无名称", ElementInfo(name="").describe())

    def test_button_id_is_the_documented_one(self):
        self.assertEqual(UIA_ButtonControlTypeId, 50000)
        self.assertEqual(CONTROL_TYPE_NAMES[UIA_ButtonControlTypeId], "按钮")


class TestElementPropertyReading(unittest.TestCase):
    """读属性时的边界。

    ``CurrentNativeWindowHandle`` 是句柄类出参：**空句柄时 ``c_void_p.value`` 是 ``None``**，
    而这恰恰是常态——按钮内部的文本节点都没有自己的 HWND。所以「读出来是 0」必须走通，
    不能抛 ``TypeError``。
    """

    def _element(self):
        # 地址是假的：_call 被替换掉了，永远不会真的去解引用它
        return Element(ctypes.c_void_p(0x1000), None)

    def test_null_native_window_handle_reads_as_zero(self):
        with mock.patch.object(uia_module, "_call", return_value=0):
            self.assertEqual(self._element().native_window(), 0)

    def test_failed_property_read_is_none(self):
        with mock.patch.object(uia_module, "_call", return_value=-1):
            self.assertIsNone(
                self._element()._out_int("IUIAutomationElement.CurrentProcessId", ctypes.c_int)
            )

    def test_info_survives_null_property_reads(self):
        """一整套属性都读不到东西时，info() 要给出一份空快照，而不是崩掉。"""
        with mock.patch.object(uia_module, "_call", return_value=0):
            info = self._element().info()
        self.assertEqual(info.native_window, 0)
        self.assertEqual(info.name, "")
        self.assertEqual(info.process_id, 0)
        self.assertEqual(info.rect, (0, 0, 0, 0))


class TestAddressOf(unittest.TestCase):
    """把 COM 指针统一成 Python int 的那一步（``_address_of``）。

    看着琐碎，但整条 COM 调用链都踩在它上面：虚表项取出来可能是 ``c_void_p``，而
    ``CFUNCTYPE(c_void_p(...))`` 会抛 ``TypeError: argument must be callable or
    integer function address``（甚至直接崩进程）；换用 ``int(...)`` 又会抛
    ``ValueError``（它走缓冲区协议，把字节当十进制字符串解析）。所以必须显式走
    ``.value``。
    """

    def test_none_and_nulls_are_zero(self):
        self.assertEqual(uia_module._address_of(None), 0)
        self.assertEqual(uia_module._address_of(0), 0)
        self.assertEqual(uia_module._address_of(ctypes.c_void_p()), 0)

    def test_plain_integers_pass_through(self):
        self.assertEqual(uia_module._address_of(0x1234), 0x1234)

    def test_ctypes_pointers_go_through_value(self):
        self.assertEqual(uia_module._address_of(ctypes.c_void_p(0x1234)), 0x1234)

    def test_int_of_a_ctypes_pointer_is_the_trap_we_avoid(self):
        with self.assertRaises(ValueError):
            int(ctypes.c_void_p(0x1234))


class TestVtableCall(unittest.TestCase):
    """``_call`` 的端到端验证：搭一个假的 COM 对象，把方法**真的调起来**。

    这一层最容易错在「解引用几层」上。COM 的内存布局是「对象的第一个字段才是虚表
    指针」，所以是**两层**：先解出虚表地址，再按下标取函数地址。少解一层不会报错
    ——它会把虚表指针本身当成函数地址，实测读出来是 ``0xfd010303`` 这种垃圾，一调
    就崩进程。段错误是最难查的一类故障，所以这里用**真回调**把布局钉死，而不是只
    断言几个数字。
    """

    SLOT = 3
    """假方法放在虚表的第几项。故意不用 0/1/2，免得把「对象第一个字段」和
    「虚表第 0 项」搞混时还能碰巧通过。"""

    def _fake_object(self, *, retval=1234, slots=5):
        """返回 ``(对象地址, 调用记录, 保命对象)``。

        第三项是**必须被调用方一直持有**的那几个 ctypes 缓冲区和回调。它们彼此之间
        只靠**地址**引用（虚表里存的是回调地址，「对象」里存的是虚表地址），没有 Python
        引用链，所以函数一返回就可能被回收、内存被还回去：轻则读到 0（表现为 E_POINTER），
        重则踩到已释放的页——段错误 / 总线错误。这正是 :func:`_call` 的失败模式：
        **不会报错，直接崩**。
        """
        calls = []

        def implementation(this, value):
            calls.append((this, value))
            return retval

        callback = uia_module._FUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int)(
            implementation
        )
        vtable = (ctypes.c_void_p * slots)()
        vtable[self.SLOT] = ctypes.cast(callback, ctypes.c_void_p).value
        fake_object = (ctypes.c_void_p * 1)()  # 第一个字段 = 虚表指针
        fake_object[0] = ctypes.addressof(vtable)
        keep_alive = (callback, vtable, fake_object)
        return ctypes.addressof(fake_object), calls, keep_alive

    def test_calls_the_method_at_the_given_index(self):
        address, calls, keep_alive = self._fake_object()
        result = uia_module._call(address, self.SLOT, ctypes.c_long, [ctypes.c_int], 42)

        self.assertEqual(result, 1234)
        self.assertEqual([value for _this, value in calls], [42])
        self.assertEqual(calls[0][0], address, "第一个参数必须是接口指针本身（COM 的 this）")
        self.assertTrue(keep_alive)

    def test_the_return_value_comes_straight_back(self):
        address, _calls, keep_alive = self._fake_object(retval=uia_module.S_OK)
        self.assertEqual(
            uia_module._call(address, self.SLOT, uia_module.HRESULT, [ctypes.c_int], 7),
            uia_module.S_OK,
        )
        self.assertTrue(keep_alive)

    def test_a_null_vtable_slot_is_refused_instead_of_calling_address_zero(self):
        address, calls, keep_alive = self._fake_object()
        self.assertEqual(
            uia_module._call(address, 1, ctypes.c_long, [ctypes.c_int], 42),
            uia_module.E_POINTER,
        )
        self.assertEqual(calls, [])
        self.assertTrue(keep_alive)

    def test_a_missing_vtable_pointer_is_refused(self):
        empty_object = (ctypes.c_void_p * 1)(0)
        self.assertEqual(
            uia_module._call(
                ctypes.addressof(empty_object), 0, ctypes.c_long, [ctypes.c_int], 1
            ),
            uia_module.E_POINTER,
        )
        self.assertIsNotNone(empty_object)


class TestElementFromPointPlumbing(unittest.TestCase):
    """「屏幕坐标 → 控件」的入口：出参指针的收发。

    这一段出错的表现是**静默拿到 None**（点不下去，日志里只有一句「没找到控件」）或者
    更糟——拿到错指针。所以用假 automation 对象把收发真跑一遍。
    """

    def _client_for(self, fake: FakeComObject) -> UiaClient:
        """造一个「已经初始化好」的客户端，``_state()`` 直接返回这个假 automation。"""
        client = UiaClient()
        client._local.state = (ctypes.c_void_p(fake.address), "")
        client._keep_fake = fake  # 假对象必须活着
        return client

    def test_the_point_goes_in_and_the_out_pointer_comes_back(self):
        seen: list[tuple[int, int]] = []

        def element_from_point(_this, point, out):
            seen.append((point.x, point.y))
            out[0] = 0xDEAD0000  # 「控件接口指针」
            return uia_module.S_OK

        fake = FakeComObject(
            {
                uia_module.VTABLE["IUIAutomation.ElementFromPoint"]: (
                    uia_module.HRESULT,
                    [wintypes.POINT, ctypes.POINTER(ctypes.c_void_p)],
                    element_from_point,
                )
            }
        )
        element = self._client_for(fake).element_from_point(1234, 567)

        self.assertEqual(seen, [(1234, 567)])
        self.assertIsNotNone(element)
        self.assertEqual(element._ptr.value, 0xDEAD0000)

    def test_a_failure_code_yields_none(self):
        def element_from_point(_this, _point, out):
            out[0] = 0xDEAD0000
            return uia_module.E_POINTER

        fake = FakeComObject(
            {
                uia_module.VTABLE["IUIAutomation.ElementFromPoint"]: (
                    uia_module.HRESULT,
                    [wintypes.POINT, ctypes.POINTER(ctypes.c_void_p)],
                    element_from_point,
                )
            }
        )
        self.assertIsNone(self._client_for(fake).element_from_point(1, 1))

    def test_a_null_out_pointer_yields_none(self):
        """成功了但没给控件（点在桌面空白处时真会这样）→ None，而不是包一个空指针。"""

        def element_from_point(_this, _point, _out):
            return uia_module.S_OK

        fake = FakeComObject(
            {
                uia_module.VTABLE["IUIAutomation.ElementFromPoint"]: (
                    uia_module.HRESULT,
                    [wintypes.POINT, ctypes.POINTER(ctypes.c_void_p)],
                    element_from_point,
                )
            }
        )
        self.assertIsNone(self._client_for(fake).element_from_point(1, 1))

    def test_the_walker_is_fetched_once_and_then_cached(self):
        fetched: list[int] = []

        def control_view_walker(_this, out):
            fetched.append(1)
            out[0] = 0xCAFE0000
            return uia_module.S_OK

        fake = FakeComObject(
            {
                uia_module.VTABLE["IUIAutomation.ControlViewWalker"]: (
                    uia_module.HRESULT,
                    [ctypes.POINTER(ctypes.c_void_p)],
                    control_view_walker,
                )
            }
        )
        client = self._client_for(fake)
        first = client.control_view_walker()
        second = client.control_view_walker()

        self.assertEqual(fetched, [1], "TreeWalker 只该取一次，之后走缓存")
        self.assertIs(first, second)
        self.assertEqual(first.value, 0xCAFE0000)

    def test_a_failed_walker_fetch_is_not_cached(self):
        """取失败不该被缓存：下次点击还有机会（只有 UIA 整体不可用时才会一直失败）。"""

        def control_view_walker(_this, _out):
            return uia_module.E_POINTER

        fake = FakeComObject(
            {
                uia_module.VTABLE["IUIAutomation.ControlViewWalker"]: (
                    uia_module.HRESULT,
                    [ctypes.POINTER(ctypes.c_void_p)],
                    control_view_walker,
                )
            }
        )
        client = self._client_for(fake)
        self.assertIsNone(client.control_view_walker())
        self.assertIsNone(client.control_view_walker())


class TestElementAgainstAFakeComObject(unittest.TestCase):
    """把 ``Element`` 的属性读取与 ``Invoke`` 对着**假 COM 元素**跑一遍。

    虚表下标之外，这里是最容易出错的地方：每个属性的出参类型都不一样（``BSTR*``、
    ``int*``、``BOOL*``、``RECT*``、句柄）。类型写错**不会报错**——8 字节写到 4 字节的
    变量上（或反过来）照样能跑完，属于「看起来是对的」的内存破坏。所以每种类型都真的走
    一遍，BSTR 还要验它是被 ``SysFreeString`` 放掉的。
    """

    def setUp(self):
        self.freed: list[int] = []

        def free_bstr(pointer):
            self.freed.append(uia_module._address_of(pointer))

        patcher = mock.patch.object(
            uia_module, "_oleaut32", mock.Mock(SysFreeString=free_bstr)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_element(self, *, name="继续", class_name="Chrome_WidgetWin_1", process_id=4242):
        # 这两个缓冲区必须活着：BSTR 的地址是从它们身上取的
        self._buffers = [ctypes.create_unicode_buffer(name), ctypes.create_unicode_buffer(class_name)]
        name_address = ctypes.cast(self._buffers[0], ctypes.c_void_p).value
        class_address = ctypes.cast(self._buffers[1], ctypes.c_void_p).value
        index = uia_module.VTABLE

        def current_name(_this, out):
            out[0] = name_address
            return uia_module.S_OK

        def current_class_name(_this, out):
            out[0] = class_address
            return uia_module.S_OK

        def current_process_id(_this, out):
            out[0] = process_id
            return uia_module.S_OK

        def current_control_type(_this, out):
            out[0] = uia_module.UIA_ButtonControlTypeId
            return uia_module.S_OK

        def current_native_window(_this, out):
            out[0] = 0x1234
            return uia_module.S_OK

        def current_is_offscreen(_this, out):
            out[0] = 0
            return uia_module.S_OK

        def current_bounding_rect(_this, out):
            out[0] = wintypes.RECT(10, 20, 110, 60)
            return uia_module.S_OK

        return FakeComObject(
            {
                index["IUIAutomationElement.CurrentName"]: (
                    uia_module.HRESULT, [ctypes.POINTER(ctypes.c_void_p)], current_name
                ),
                index["IUIAutomationElement.CurrentClassName"]: (
                    uia_module.HRESULT, [ctypes.POINTER(ctypes.c_void_p)], current_class_name
                ),
                index["IUIAutomationElement.CurrentProcessId"]: (
                    uia_module.HRESULT, [ctypes.POINTER(ctypes.c_int)], current_process_id
                ),
                index["IUIAutomationElement.CurrentControlType"]: (
                    uia_module.HRESULT, [ctypes.POINTER(ctypes.c_int)], current_control_type
                ),
                index["IUIAutomationElement.CurrentNativeWindowHandle"]: (
                    uia_module.HRESULT, [ctypes.POINTER(ctypes.c_void_p)], current_native_window
                ),
                index["IUIAutomationElement.CurrentIsOffscreen"]: (
                    uia_module.HRESULT, [ctypes.POINTER(wintypes.BOOL)], current_is_offscreen
                ),
                index["IUIAutomationElement.CurrentBoundingRectangle"]: (
                    uia_module.HRESULT, [ctypes.POINTER(wintypes.RECT)], current_bounding_rect
                ),
            }
        )

    def _element_for(self, fake: FakeComObject) -> Element:
        # 假对象只被**地址**引用（Element 里存的是一个整数指针），所以测试得自己
        # 把它拿住——不然它会被回收，读到的全是已被还回去的内存。
        self._fakes = getattr(self, "_fakes", [])
        self._fakes.append(fake)
        return Element(ctypes.c_void_p(fake.address), None)

    def test_info_reads_every_property_type(self):
        element = self._element_for(self._fake_element())
        info = element.info()

        self.assertEqual(info.name, "继续")
        self.assertEqual(info.class_name, "Chrome_WidgetWin_1")
        self.assertEqual(info.process_id, 4242)
        self.assertEqual(info.control_type, uia_module.UIA_ButtonControlTypeId)
        self.assertEqual(info.control_type_name, "按钮")
        self.assertEqual(info.native_window, 0x1234)
        self.assertFalse(info.offscreen)
        self.assertEqual(info.rect, (10, 20, 110, 60))
        self.assertEqual(len(self.freed), 2, "两个 BSTR 都该被 SysFreeString 放掉")

    def test_describe_uses_what_was_read(self):
        info = self._element_for(self._fake_element()).info()
        self.assertIn("『继续』", info.describe())
        self.assertIn("按钮", info.describe())
        self.assertIn("pid=4242", info.describe())

    def test_invoke_goes_through_get_current_pattern_and_releases_it(self):
        invoked: list[int] = []
        released: list[int] = []

        def release(_this):
            released.append(1)
            return 1

        def invoke(_this):
            invoked.append(1)
            return uia_module.S_OK

        pattern = FakeComObject(
            {
                uia_module.VTABLE["IUnknown.Release"]: (ctypes.c_ulong, [], release),
                uia_module.VTABLE["IUIAutomationInvokePattern.Invoke"]: (
                    uia_module.HRESULT, [], invoke
                ),
            }
        )
        asked: list[int] = []

        def get_current_pattern(_this, pattern_id, out):
            asked.append(pattern_id)
            out[0] = pattern.address
            return uia_module.S_OK

        element_com = FakeComObject(
            {
                uia_module.VTABLE["IUIAutomationElement.GetCurrentPattern"]: (
                    uia_module.HRESULT,
                    [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                    get_current_pattern,
                )
            }
        )
        element = self._element_for(element_com)

        self.assertTrue(element.can_invoke())
        self.assertTrue(element.invoke())
        self.assertEqual(asked, [uia_module.UIA_InvokePatternId, uia_module.UIA_InvokePatternId])
        self.assertEqual(invoked, [1])
        self.assertEqual(len(released), 2, "探测和调用各拿了一个图案对象，都要还")

    def test_invoke_returns_false_when_get_current_pattern_fails(self):
        def get_current_pattern(_this, _pattern_id, _out):
            return uia_module.E_POINTER

        element_com = FakeComObject(
            {
                uia_module.VTABLE["IUIAutomationElement.GetCurrentPattern"]: (
                    uia_module.HRESULT,
                    [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                    get_current_pattern,
                )
            }
        )
        element = self._element_for(element_com)

        self.assertFalse(element.can_invoke())
        self.assertFalse(element.invoke())


class TestClientOffWindows(unittest.TestCase):
    """非 Windows 上必须「优雅不可用」，而不是 import 或调用就炸。"""

    def test_win32_libraries_are_guarded(self):
        if sys.platform == "win32":  # pragma: no cover - Windows
            self.assertTrue(uia_module.OLE_AVAILABLE)
        else:  # pragma: no cover - Linux / macOS
            self.assertFalse(uia_module.OLE_AVAILABLE)

    def test_null_pointer_call_is_refused(self):
        # 空指针在碰 WINFUNCTYPE 之前就被挡掉，所以在任何平台上都安全
        self.assertEqual(uia_module._call(None, 2, uia_module.HRESULT, []), uia_module.E_POINTER)
        self.assertEqual(uia_module._call(0, 2, uia_module.HRESULT, []), uia_module.E_POINTER)

    def test_releasing_nothing_is_a_no_op(self):
        uia_module._release(None)
        uia_module._release(0)

    def test_client_reports_unavailable_without_raising(self):
        if sys.platform == "win32":  # pragma: no cover - Windows
            self.skipTest("Windows 上 UIA 是可用的")
        client = UiaClient()
        self.assertFalse(client.available)
        self.assertIn("非 Windows", client.last_error)
        self.assertIsNone(client.element_from_point(10, 10))
        self.assertIsNone(client.control_view_walker())
        client.close()


class TestWalkToInvokable(unittest.TestCase):
    """公用函数：探针与后端都靠它找「真正能点的那个控件」，所以释放责任要钉死。"""

    def test_the_element_itself_when_it_can_invoke(self):
        element = FakeElement(invokable=True)
        target, hops = uia_module.walk_to_invokable(element, 3)
        self.assertIs(target, element)
        self.assertEqual(hops, 0)

    def test_finds_an_ancestor_and_releases_only_the_middle(self):
        child = FakeElement(invokable=False)
        middle = FakeElement(invokable=False)
        button = FakeElement(invokable=True)
        link(child, middle, button)
        target, hops = uia_module.walk_to_invokable(child, 4)
        self.assertIs(target, button)
        self.assertEqual(hops, 2)
        self.assertEqual(middle.closed, 1, "中间那层在这里就该放掉")
        self.assertEqual(child.closed, 0, "element 由调用方释放")
        self.assertEqual(button.closed, 0, "返回的控件由调用方释放")

    def test_gives_up_at_the_top(self):
        child = FakeElement(invokable=False)
        parent = FakeElement(invokable=False)
        link(child, parent)
        target, hops = uia_module.walk_to_invokable(child, 5)
        self.assertIsNone(target)
        self.assertEqual(hops, 1)
        self.assertEqual(parent.closed, 1)
        self.assertEqual(child.closed, 0)

    def test_respects_the_hop_limit(self):
        child = FakeElement(invokable=False)
        middle = FakeElement(invokable=False)
        button = FakeElement(invokable=True)
        link(child, middle, button)
        target, _hops = uia_module.walk_to_invokable(child, 1)
        self.assertIsNone(target, "只准上 1 层，按钮在第 2 层，够不到")
        self.assertEqual(middle.closed, 1)
        self.assertEqual(button.closed, 1, "取出来才看得到它，所以也得放掉")

    def test_zero_hops_means_the_element_only(self):
        child = FakeElement(invokable=False)
        parent = FakeElement(invokable=True)
        link(child, parent)
        target, hops = uia_module.walk_to_invokable(child, 0)
        self.assertIsNone(target)
        self.assertEqual(hops, 0)
        self.assertEqual(parent.closed, 1)
        self.assertEqual(child.closed, 0)


# ================================================================ 后端
class TestUiaBackendClick(unittest.TestCase):
    """左键点击：取坐标下的控件，Invoke 它。"""

    def setUp(self):
        self.element = FakeElement()
        self.client = FakeClient(self.element)
        self.backend = UiaBackend(client=self.client)

    def test_clicks_by_invoking_the_element_under_the_point(self):
        self.backend.click(120, 340)
        self.assertEqual(self.client.asked, [(120, 340)])
        self.assertEqual(self.element.invoked, 1)

    def test_never_moves_the_mouse(self):
        """UIA 的全部意义就在这儿：不碰鼠标，所以没有回落调用。"""
        fallback = FakeFallback()
        backend = UiaBackend(client=self.client, fallback=fallback)
        backend.click(10, 10, MouseButton.LEFT)
        self.assertEqual(fallback.calls, [])

    def test_element_is_released_after_a_successful_click(self):
        self.backend.click(10, 10)
        self.assertEqual(self.element.closed, 1, "COM 指针必须释放，否则长时间挂机会泄漏")

    def test_element_is_released_even_when_invokable_is_absent(self):
        without = FakeElement(invokable=False)
        self.backend._client = FakeClient(without)
        self.backend.click(10, 10)
        self.assertEqual(without.invoked, 0)
        self.assertEqual(without.closed, 1)

    def test_element_is_released_when_invoke_fails(self):
        failing = FakeElement(invoke_ok=False)
        self.backend._client = FakeClient(failing)
        self.backend.click(10, 10)
        self.assertEqual(failing.invoked, 1)
        self.assertEqual(failing.closed, 1)

    def test_missing_element_warns_and_does_nothing(self):
        self.backend._client = FakeClient(None)
        with self.assertLogs("app.input.uia", level="WARNING") as captured:
            self.backend.click(10, 10)
        self.assertIn("没取到控件", captured.records[0].getMessage())

    def test_missing_element_warns_only_once_per_run(self):
        self.backend._client = FakeClient(None)
        with self.assertLogs("app.input.uia", level="WARNING") as captured:
            for _ in range(5):
                self.backend.click(10, 10)
        self.assertEqual(len(captured.records), 1, "一个宏里成百次点击不该刷爆日志")

    def test_notices_can_be_reset_for_the_next_run(self):
        self.backend._client = FakeClient(None)
        with self.assertLogs("app.input.uia", level="WARNING") as captured:
            self.backend.click(10, 10)
            self.backend.reset_notices()
            self.backend.click(10, 10)
        self.assertEqual(len(captured.records), 2, "每次运行都该看到关键提示")

    def test_successful_click_is_logged_once(self):
        with self.assertLogs("app.input.uia", level="INFO") as captured:
            self.backend.click(1, 1)
            self.backend.click(1, 1)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("继续", captured.records[0].getMessage())
        self.assertIn("按钮", captured.records[0].getMessage())

    def test_invoke_failure_is_reported_distinctly(self):
        self.backend._client = FakeClient(FakeElement(invoke_ok=False))
        with self.assertLogs("app.input.uia", level="WARNING") as captured:
            self.backend.click(10, 10)
        self.assertIn("调用失败", captured.records[0].getMessage())


class TestUiaBackendParentWalk(unittest.TestCase):
    """点到的常常是按钮内部的「文本」节点，要往上找真正能 Invoke 的那个。"""

    def setUp(self):
        self.child = FakeElement(name="继续", invokable=False, control_type=50020)  # 文本
        self.button = FakeElement(name="继续", invokable=True)  # 按钮
        link(self.child, self.button)
        self.client = FakeClient(self.child)
        self.backend = UiaBackend(client=self.client)

    def test_walks_up_to_the_button_and_invokes_it(self):
        self.backend.click(10, 10)
        self.assertEqual(self.button.invoked, 1, "应当在祖先里找到能 Invoke 的控件")
        self.assertEqual(self.child.invoked, 0)

    def test_releases_every_element_it_took_along_the_way(self):
        self.backend.click(10, 10)
        self.assertEqual(self.child.closed, 1)
        self.assertEqual(self.button.closed, 1, "自己取的父控件也要释放")

    def test_hop_limit_is_respected(self):
        """层级太深就放弃，别一路往上点到整个面板上。"""
        grand = FakeElement(name="面板", invokable=True)
        link(self.child, self.button, grand)
        self.button._invokable = False
        shallow = UiaBackend(client=self.client, max_parent_hops=1)
        with self.assertLogs("app.input.uia", level="WARNING") as captured:
            shallow.click(10, 10)
        self.assertEqual(grand.invoked, 0)
        self.assertIn("都不支持", captured.records[0].getMessage())

    def test_hop_limit_zero_means_element_only(self):
        zero = UiaBackend(client=self.client, max_parent_hops=0)
        with self.assertLogs("app.input.uia", level="WARNING"):
            zero.click(10, 10)
        self.assertEqual(self.button.invoked, 0)

    def test_warn_when_nothing_up_the_chain_can_be_invoked(self):
        self.button._invokable = False
        with self.assertLogs("app.input.uia", level="WARNING") as captured:
            self.backend.click(10, 10)
        message = captured.records[0].getMessage()
        self.assertIn("不支持", message)
        self.assertIn("继续", message)

    def test_walking_stops_at_the_top(self):
        self.button._parent = None
        self.button._invokable = False
        with self.assertLogs("app.input.uia", level="WARNING"):
            self.backend.click(10, 10)
        self.assertEqual(self.child.closed, 1)
        self.assertEqual(self.button.closed, 1)


class TestUiaBackendProcessGuard(unittest.TestCase):
    """绑定窗口后的安全校验：坐标下是别人家的控件就跳过，而不是乱点。"""

    def setUp(self):
        self.client = FakeClient()
        self.backend = UiaBackend(client=self.client)
        with mock.patch.dict(sys.modules, {"app.win.window": fake_window_module()}):
            self.assertTrue(self.backend.bind_window(0x1234))

    def test_binding_records_the_window_and_its_title(self):
        self.assertEqual(self.backend.bound_window, 0x1234)
        self.assertEqual(self.backend.bound_title, "记事本 [Notepad]")

    def test_same_process_is_invoked(self):
        element = FakeElement(process_id=4242)
        self.client.element = element
        self.backend.click(10, 10)
        self.assertEqual(element.invoked, 1)

    def test_foreign_process_is_skipped(self):
        element = FakeElement(process_id=9999)
        self.client.element = element
        with self.assertLogs("app.input.uia", level="WARNING") as captured:
            self.backend.click(10, 10)
        self.assertEqual(element.invoked, 0, "宁可不动手，也不要点到别的窗口的控件上")
        self.assertEqual(element.closed, 1)
        self.assertIn("不属于绑定的窗口", captured.records[0].getMessage())

    def test_guard_is_off_when_not_bound(self):
        backend = UiaBackend(client=self.client)
        element = FakeElement(process_id=9999)
        self.client.element = element
        backend.click(10, 10)
        self.assertEqual(element.invoked, 1)

    def test_unknown_process_id_does_not_block(self):
        """拿不到 pid 时不该把正常点击也拦掉。"""
        element = FakeElement(process_id=0)
        self.client.element = element
        self.backend.click(10, 10)
        self.assertEqual(element.invoked, 1)

    def test_unbind_clears_the_guard(self):
        self.backend.unbind()
        element = FakeElement(process_id=9999)
        self.client.element = element
        self.backend.click(10, 10)
        self.assertEqual(element.invoked, 1)

    def test_binding_a_dead_handle_is_refused(self):
        with mock.patch.dict(sys.modules, {"app.win.window": fake_window_module(is_window=lambda h: False)}):
            self.assertFalse(self.backend.bind_window(0x5555))
        self.assertEqual(self.backend.bound_window, 0x1234, "失败不该动掉原来的绑定")

    def test_bind_point_resolves_the_window_under_the_cursor(self):
        with mock.patch.dict(sys.modules, {"app.win.window": fake_window_module()}):
            self.assertTrue(self.backend.bind_point(50, 60))

    def test_bind_point_fails_when_there_is_no_window(self):
        backend = UiaBackend(client=self.client)
        with mock.patch.dict(
            sys.modules, {"app.win.window": fake_window_module(window_from_point=lambda x, y: None)}
        ):
            self.assertFalse(backend.bind_point(50, 60))
        self.assertIsNone(backend.bound_window)


class TestUiaBackendFallback(unittest.TestCase):
    """UIA 没有对应语义的操作：回落前台，并提示一次。"""

    def setUp(self):
        self.fallback = FakeFallback()
        self.backend = UiaBackend(client=FakeClient(FakeElement()), fallback=self.fallback)

    def test_right_click_falls_back(self):
        with self.assertLogs("app.input.uia", level="INFO") as captured:
            self.backend.click(5, 6, MouseButton.RIGHT)
        self.assertIn(("click", (5, 6, MouseButton.RIGHT)), self.fallback.calls)
        self.assertIn("只处理左键", captured.records[0].getMessage())

    def test_middle_click_falls_back(self):
        self.backend.click(5, 6, MouseButton.MIDDLE)
        self.assertIn(("click", (5, 6, MouseButton.MIDDLE)), self.fallback.calls)

    def test_move_falls_back(self):
        with self.assertLogs("app.input.uia", level="INFO") as captured:
            self.backend.move_to(7, 8)
        self.assertIn(("move_to", (7, 8)), self.fallback.calls)
        self.assertIn("移动", captured.records[0].getMessage())

    def test_key_falls_back(self):
        with self.assertLogs("app.input.uia", level="INFO") as captured:
            self.backend.press_key(0x0D)
        self.assertIn(("press_key", (0x0D,)), self.fallback.calls)
        self.assertIn("按键", captured.records[0].getMessage())

    def test_fallback_notes_are_also_once_per_run(self):
        with self.assertLogs("app.input.uia", level="INFO") as captured:
            self.backend.move_to(1, 1)
            self.backend.move_to(2, 2)
            self.backend.press_key(0x41)
            self.backend.press_key(0x42)
            self.backend.reset_notices()
            self.backend.move_to(3, 3)
        self.assertEqual(len(captured.records), 3, "移动/按键各提示一次，重置后再来一次")

    def test_without_fallback_it_warns_instead_of_crashing(self):
        lonely = UiaBackend(client=FakeClient(FakeElement()))
        with self.assertLogs("app.input.uia", level="WARNING") as captured:
            lonely.move_to(1, 1)
            lonely.press_key(0x41)
            lonely.click(1, 1, MouseButton.RIGHT)
        self.assertEqual(len(captured.records), 3)
        self.assertIn("没有可用的前台后端", captured.records[0].getMessage())


class TestUiaBackendLifecycle(unittest.TestCase):
    def test_close_closes_the_client(self):
        client = FakeClient(FakeElement())
        backend = UiaBackend(client=client)
        backend.close()
        self.assertEqual(client.closed, 1)

    def test_available_delegates_to_the_client(self):
        client = FakeClient()
        self.assertTrue(UiaBackend(client=client).available)
        client.available = False
        client.last_error = "没装/不可用"
        backend = UiaBackend(client=client)
        self.assertFalse(backend.available)
        self.assertEqual(backend.unavailable_reason, "没装/不可用")

    def test_name_marks_it_as_a_third_backend(self):
        self.assertEqual(UiaBackend(client=FakeClient()).name, "uia")


@unittest.skipUnless(sys.platform == "win32", "需要真实的 UI Automation")
class TestRealUia(unittest.TestCase):
    """在真 Windows 上跑一遍真 COM。

    为什么必须有这一组：虚表下标是手写的，而 COM 的 ABI 只认顺序——**错一个数不会报错，
    只会静默调到隔壁函数上**，甚至直接崩掉进程。只有真的调用一次才能证明这张表是对的。

    不需要任何目标程序：桌面本身就是 UIA 树的一部分，坐标 (0, 0) 一定能取到元素。
    这一组会真实走一遍 ``CoInitializeEx`` / ``CoCreateInstance`` / ``ElementFromPoint`` /
    ``Current*`` 属性 / ``ControlViewWalker`` / ``GetCurrentPattern``——也就是除了
    ``Invoke`` 之外的全部链条（``Invoke`` 会真的点到东西，不在这里点）。
    """

    def test_client_can_be_created_on_this_thread(self):
        client = UiaClient()
        try:
            self.assertTrue(client.available, f"UIA 起不来：{client.last_error}")
        finally:
            client.close()

    def test_element_from_point_reads_its_properties(self):
        client = UiaClient()
        try:
            element = client.element_from_point(0, 0)
            self.assertIsNotNone(element, "桌面左上角一定有元素")
            with element:
                info = element.info()
                self.assertGreater(info.control_type, 0, f"控件类型读出来是 {info.control_type}")
                self.assertTrue(info.control_type_name, "控件类型应当能翻译成人话")
                self.assertGreaterEqual(info.process_id, 0)
        finally:
            client.close()

    def test_walking_up_the_tree_does_not_crash(self):
        """``ControlViewWalker`` + ``GetParentElement`` + ``GetCurrentPattern`` 一起过一遍。"""
        client = UiaClient()
        try:
            element = client.element_from_point(0, 0)
            self.assertIsNotNone(element)
            try:
                target, _hops = uia_module.walk_to_invokable(element, 3)
                if target is not None and target is not element:
                    target.close()
            finally:
                element.close()
        finally:
            client.close()

    def test_close_is_idempotent_and_survives_reuse(self):
        client = UiaClient()
        client.close()
        client.close()
        self.assertTrue(client.available, "关掉之后应当还能按需重建")


if __name__ == "__main__":
    logging.getLogger("app.input.uia").addHandler(logging.NullHandler())
    unittest.main(verbosity=2)
