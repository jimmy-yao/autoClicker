"""冒烟测试：不依赖 pytest，直接 ``python tests/test_smoke.py`` 运行。

覆盖范围
--------
- Win32 结构体布局（SendInput 最怕结构体大小算错，会导致静默失败）
- 虚拟屏幕坐标读取
- 单实例守卫
- 步骤模型与方案的 JSON 往返（含找图模板的 base64 + zlib 打包）
- 找图匹配：探针挑法、抽样比对、容差、同分取最左上、大面积同色屏幕
- 方案仓库的增删改查与原子写入
- 引擎的步骤顺序、循环、停用跳过、停止响应（用假后端，不会真的点鼠标）
- 找图步骤的等待与超时策略（用注入的假抓屏，不需要真的屏幕）
- 无头模式下主窗口与步骤编辑器能否正常装配

GUI 相关测试使用 Qt 的 offscreen 平台插件，不会真的弹窗。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import Qt  # noqa: E402

from app.core.models import (  # noqa: E402
    ON_TIMEOUT_STOP,
    ClickStep,
    DelayStep,
    KeyStep,
    MoveStep,
    Profile,
    StepKind,
    WaitStep,
)
from app.input.base import InputBackend, MouseButton  # noqa: E402
from app.input.sendinput import INPUT  # noqa: E402
from app.win.hooks import InputEvent  # noqa: E402

# 测试通过信号断言，不需要日志输出；挂个 NullHandler 让 root logger 保持安静
# （存在任何 handler 时，logging 就不会再走 lastResort 直接往 stderr 打）
import logging  # noqa: E402

logging.getLogger().addHandler(logging.NullHandler())


class FakeBackend(InputBackend):
    """记录调用而不真的操作鼠标，保留完整时间线用于校验执行顺序。"""

    name = "fake"

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def click(self, x, y, button=MouseButton.LEFT):
        self.events.append(("click", x, y, button))

    def move_to(self, x, y):
        self.events.append(("move", x, y))

    def press_key(self, vk):
        self.events.append(("key", vk))

    # ---- 便利视图
    @property
    def kinds(self) -> list[str]:
        return [event[0] for event in self.events]

    @property
    def clicks(self) -> list[tuple]:
        return [event for event in self.events if event[0] == "click"]

    @property
    def keys(self) -> list[tuple]:
        return [event for event in self.events if event[0] == "key"]


# ==================================================================== 假屏幕
def _pixel(b, g, r):
    """一个 BGRA 像素。抓屏输出就是 BGRA，这里保持一致。"""
    return bytes((b, g, r, 255))


def _frame(width, height, color=(18, 18, 22), x=0, y=0):
    from app.win.screen import ScreenFrame

    return ScreenFrame(
        x=x, y=y, width=width, height=height, data=_pixel(*color) * (width * height)
    )


def _paint(frame, left, top, width, height, color):
    """在帧上刷一块矩形。``ScreenFrame`` 是 frozen 的，所以返回新的一帧。"""
    from app.win.screen import ScreenFrame

    data = bytearray(frame.data)
    stride = frame.stride
    block = _pixel(*color) * width
    for row in range(top, top + height):
        start = row * stride + left * 4
        data[start : start + width * 4] = block
    return ScreenFrame(
        x=frame.x, y=frame.y, width=frame.width, height=frame.height, data=bytes(data)
    )


def _crop_frame(frame, left, top, width, height):
    """从帧上抠一块出来（等价于抓屏的 ``region`` 参数）。"""
    from app.win.screen import ScreenFrame

    stride = frame.stride
    rows = [
        frame.data[(top + row) * stride + left * 4 : (top + row) * stride + (left + width) * 4]
        for row in range(height)
    ]
    return ScreenFrame(x=left, y=top, width=width, height=height, data=b"".join(rows))


def _crop(frame, left, top, width, height):
    from app.core.matching import Template

    piece = _crop_frame(frame, left, top, width, height)
    return Template(width=piece.width, height=piece.height, data=piece.data)


BUTTON_AT = (70, 50)
BUTTON_SIZE = (40, 20)
BUTTON_COLOR = (212, 120, 0)  # BGRA：蓝底
BUTTON_TEXT_COLOR = (255, 255, 255)
# 按钮里的「文字」块。刻意做大一点：它决定了抽样校验有多少个点落在非纯色区
BUTTON_TEXT_AT = (5, 4)
BUTTON_TEXT_SIZE = (30, 12)


def _screen_with_button(position=BUTTON_AT, origin=(0, 0), size=(200, 120)):
    """造一屏：深色背景上有一个蓝底白字的按钮。"""
    frame = _frame(size[0], size[1], x=origin[0], y=origin[1])
    left, top = position
    frame = _paint(frame, left, top, BUTTON_SIZE[0], BUTTON_SIZE[1], BUTTON_COLOR)
    return _paint(
        frame,
        left + BUTTON_TEXT_AT[0],
        top + BUTTON_TEXT_AT[1],
        BUTTON_TEXT_SIZE[0],
        BUTTON_TEXT_SIZE[1],
        BUTTON_TEXT_COLOR,
    )


def _button_template(origin=(0, 0)):
    """从假屏幕里抠出那个按钮当模板。"""
    frame = _screen_with_button(origin=origin)
    return _crop(frame, BUTTON_AT[0], BUTTON_AT[1], BUTTON_SIZE[0], BUTTON_SIZE[1])


def _gradient_template():
    """一行渐变：没有任何纯色横线，用来验证「这模板不好找」的提示。"""
    from app.core.matching import Template

    data = b"".join(
        _pixel(255 - x * 10, x * 7, x * 3) for x in range(20)
    )
    return Template(width=20, height=3, data=data * 3)


# ==================================================================== 结构体
class TestStructLayout(unittest.TestCase):
    def test_input_struct_size(self):
        """INPUT 大小必须和 Win32 一致，否则 SendInput 会静默失败。"""
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(INPUT), expected)

    def test_anonymous_union_shares_memory(self):
        """``mi`` / ``ki`` / ``hi`` 共用同一块内存 —— 这是 INPUT 的设计，不是 bug。

        这个测试把该行为固定下来：以后如果有人「顺手」把联合体拆成独立字段，
        结构体大小就会变，SendInput 随即静默失败。
        """
        item = INPUT()
        item.type = 0

        item.mi.dx = 123
        self.assertEqual(item.mi.dx, 123)

        item.ki.wVk = 5
        self.assertEqual(item.ki.wVk, 5)
        self.assertNotEqual(item.mi.dx, 123, "ki 与 mi 应共享同一块内存")


# ==================================================================== 平台
class TestGeometry(unittest.TestCase):
    def test_virtual_screen_is_sane(self):
        from app.win.geometry import get_cursor_pos, virtual_screen_rect

        rect = virtual_screen_rect()
        self.assertGreater(rect.width, 0)
        self.assertGreater(rect.height, 0)

        x, y = get_cursor_pos()
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)


class TestSingleInstance(unittest.TestCase):
    def test_second_acquire_fails(self):
        from app.win.singleton import SingleInstance

        name = r"Local\AutoClicker.Test.SmokeTest"
        first, second = SingleInstance(name), SingleInstance(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            first.release()
            second.release()

        third = SingleInstance(name)
        try:
            self.assertTrue(third.acquire())
        finally:
            third.release()


class TestHotkeyTable(unittest.TestCase):
    def test_vk_mapping(self):
        from app.win.hotkey import name_of_vk, vk_names, vk_of

        self.assertEqual(vk_of("F9"), 0x78)
        self.assertEqual(vk_of("f8"), 0x77)
        self.assertEqual(vk_of("A"), 0x41)
        self.assertEqual(vk_of("ESC"), 0x1B)
        self.assertIsNone(vk_of("NOPE"))

        self.assertEqual(name_of_vk(0x78), "F9")
        self.assertTrue(name_of_vk(0xFF).startswith("0x"))

        names = vk_names()
        self.assertEqual(names[0], "F1")
        self.assertIn("A", names)
        self.assertIn("ENTER", names)
        self.assertEqual(len(names), len(set(names)), "下拉列表不应有重复项")


# ==================================================================== 模型
class TestModels(unittest.TestCase):
    def test_click_step_validation(self):
        ClickStep(x=1, y=2, interval_ms=100).validate()
        with self.assertRaises(ValueError):
            ClickStep(x=0, y=0, interval_ms=0).validate()
        with self.assertRaises(ValueError):
            ClickStep(x=0, y=0, repeat=0).validate()
        with self.assertRaises(ValueError):
            ClickStep(x=0, y=0, jitter_px=-1).validate()

    def test_key_step_validation(self):
        from app.win.hotkey import vk_of

        KeyStep(vk=vk_of("F5"), repeat=1, interval_ms=50).validate()
        with self.assertRaises(ValueError):
            KeyStep(vk=0).validate()

    def test_step_roundtrip(self):
        from app.core.models import step_from_dict

        originals = [
            ClickStep(x=-12, y=34, button=MouseButton.RIGHT, repeat=3, interval_ms=250,
                      jitter_ms=40, jitter_px=2, note="登录按钮"),
            DelayStep(delay_ms=1500, jitter_ms=200),
            KeyStep(vk=0x74, repeat=2, interval_ms=30, enabled=False),
            MoveStep(points=[(10, 20), (30, 40), (50, 60)], duration_ms=420, note="划过去"),
            MoveStep(points=[(7, 7)], duration_ms=0),
            WaitStep(template=_button_template().encode(), tolerance=40, timeout_ms=15000,
                     poll_interval_ms=250, button=MouseButton.RIGHT, offset_x=3, offset_y=-3,
                     on_timeout=ON_TIMEOUT_STOP, note="等继续弹窗"),
        ]
        for original in originals:
            restored = step_from_dict(original.to_dict())
            self.assertIs(type(restored), type(original))
            self.assertEqual(restored.to_dict(), original.to_dict())

    def test_wait_step_validation(self):
        WaitStep(template=_button_template().encode()).validate()

        with self.assertRaises(ValueError):
            WaitStep().validate()  # 还没截模板
        with self.assertRaises(ValueError):
            WaitStep(template="这不是模板").validate()  # 模板数据坏了
        with self.assertRaises(ValueError):
            WaitStep(template=_button_template().encode(), tolerance=300).validate()
        with self.assertRaises(ValueError):
            WaitStep(template=_button_template().encode(), poll_interval_ms=1).validate()
        with self.assertRaises(ValueError):
            WaitStep(template=_button_template().encode(), on_timeout="随便").validate()

    def test_wait_step_summary(self):
        step = WaitStep(template=_button_template().encode(), timeout_ms=30000)
        text = step.summary()
        self.assertIn("40 × 20", text)
        self.assertIn("左键", text)
        self.assertIn("30s", text)

        self.assertEqual(WaitStep().summary(), "（未截取模板）")
        self.assertIn(
            "超时中止", WaitStep(template=step.template, on_timeout=ON_TIMEOUT_STOP).summary()
        )

    def test_wait_step_without_template_cannot_be_saved(self):
        """没截模板的找图步骤算「不完整」：能存在，但不让存成方案。

        界面在对话框那一层就拦住了（不截模板点不了「确定」）。这里守的是手改 JSON
        之类的旁路：拦下来时还必须点名是哪一步，否则用户根本不知道该修哪个。
        """
        profile = Profile(name="半成品", steps=[WaitStep(note="还没截图")])
        with self.assertRaises(ValueError) as ctx:
            profile.validate(require_steps=False)

        self.assertIn("第 1 步", str(ctx.exception))
        self.assertIn("找图", str(ctx.exception))

    def test_move_step_validation(self):
        MoveStep(points=[(1, 2)], duration_ms=0).validate()
        with self.assertRaises(ValueError):
            MoveStep(points=[], duration_ms=100).validate()
        with self.assertRaises(ValueError):
            MoveStep(points=[(1, 2)], duration_ms=-1).validate()

    def test_move_step_ignores_broken_points(self):
        profile = Profile.from_dict(
            {
                "name": "容错",
                "steps": [{"kind": "move", "points": [[1, 2], [3], "x", [4, 5]], "duration_ms": 10}],
            }
        )
        step = profile.steps[0]
        self.assertIsInstance(step, MoveStep)
        self.assertEqual(step.points, [(1, 2), (4, 5)])

    def test_profile_roundtrip_and_active_steps(self):
        profile = Profile(
            name="测试方案",
            steps=[
                ClickStep(x=1, y=1, repeat=2),
                DelayStep(delay_ms=10, enabled=False),
                KeyStep(vk=0x41),
            ],
            repeat=5,
            repeat_delay_ms=100,
            start_delay_ms=700,
        )
        restored = Profile.from_dict(profile.to_dict())

        self.assertEqual(restored.name, "测试方案")
        self.assertEqual(restored.repeat, 5)
        self.assertEqual(restored.repeat_delay_ms, 100)
        self.assertEqual(restored.start_delay_ms, 700)
        self.assertEqual(len(restored.steps), 3)
        self.assertEqual([step.kind for step in restored.active_steps()], [StepKind.CLICK, StepKind.KEY])

    def test_validate_reports_step_index(self):
        profile = Profile(steps=[ClickStep(x=0, y=0), DelayStep(delay_ms=-1)])
        with self.assertRaises(ValueError) as ctx:
            profile.validate()
        self.assertIn("第 2 步", str(ctx.exception))

    def test_validate_requires_enabled_steps(self):
        profile = Profile(steps=[ClickStep(x=0, y=0, enabled=False)])
        with self.assertRaises(ValueError):
            profile.validate()
        profile.validate(require_steps=False)  # 保存时允许半成品

    def test_from_dict_skips_broken_steps(self):
        profile = Profile.from_dict(
            {
                "name": "容错",
                "steps": [
                    {"kind": "click", "x": 5, "y": 6},
                    {"kind": "unknown-thing"},
                    "not-a-dict",
                ],
            }
        )
        self.assertEqual(len(profile.steps), 1)
        self.assertEqual(profile.steps[0].x, 5)

    def test_default_profile_points_at_primary_screen(self):
        profile = Profile.default()
        self.assertTrue(profile.steps)
        step = profile.steps[0]
        self.assertIsInstance(step, ClickStep)
        self.assertGreaterEqual(step.x, 0)
        self.assertGreaterEqual(step.y, 0)


# ==================================================================== 找图
class TestMatching(unittest.TestCase):
    """模板匹配全部在内存里跑，不需要真的屏幕。"""

    def test_template_encode_decode_roundtrip(self):
        from app.core.matching import Template

        template = _button_template()
        restored = Template.decode(template.encode())

        self.assertEqual((restored.width, restored.height), BUTTON_SIZE)
        self.assertEqual(restored.data, template.data)

    def test_template_decode_rejects_broken_data(self):
        import base64

        from app.core.matching import Template

        bad_inputs = [
            "",
            "这不是-base64!!",
            base64.b64encode(b"XXXX" + b"\x00" * 20).decode(),  # magic 不对
            base64.b64encode(b"ACT1" + b"\x00" * 8).decode(),  # 宽高为 0
            base64.b64encode(b"ACT1" + b"\x04\x00\x04\x00" + "垃圾".encode()).decode(),  # zlib 坏
            _button_template().encode()[:-6],  # 被截断
        ]
        for bad in bad_inputs:
            with self.assertRaises(ValueError, msg=f"{bad!r} 应该被拒绝"):
                Template.decode(bad)

    def test_template_rejects_wrong_pixel_count(self):
        from app.core.matching import Template

        with self.assertRaises(ValueError):
            Template(width=4, height=4, data=b"\x00" * 10)
        with self.assertRaises(ValueError):
            Template(width=0, height=1, data=b"")

    def test_probes_put_selectivity_first(self):
        """第一个签名要横跨文字（不是纯色，因此几乎不会误命中），第二个才是纯色退路。"""
        from app.core.matching import MIN_PROBE_PX, pick_probes

        probes = pick_probes(_button_template())
        self.assertEqual(len(probes), 2, "按钮模板应该同时给出整行签名和纯色签名")

        first = [
            probes[0].signature[index * 4 : index * 4 + 4]
            for index in range(probes[0].length)
        ]
        self.assertGreater(len(set(first)), 1, "第一个签名要横跨文字，不能是一整段纯色")

        second = [
            probes[1].signature[index * 4 : index * 4 + 4]
            for index in range(probes[1].length)
        ]
        self.assertEqual(len(set(second)), 1, "最后那个签名必须是纯色段")
        self.assertGreaterEqual(probes[1].length, MIN_PROBE_PX)

    def test_probes_skip_useless_solid_signature(self):
        """没有纯色段时不该硬塞一个 1 像素的签名进去——那玩意儿满屏都是。"""
        from app.core.matching import pick_probes

        probes = pick_probes(_gradient_template())
        self.assertEqual(len(probes), 1)

    def test_solid_run_length_flags_photo_like_template(self):
        from app.core.matching import MIN_PROBE_PX, solid_run_length

        self.assertGreaterEqual(solid_run_length(_button_template()), 40)
        self.assertEqual(solid_run_length(_gradient_template()), 1)
        self.assertLess(solid_run_length(_gradient_template()), MIN_PROBE_PX)

    def test_find_template_locates_the_button(self):
        from app.core.matching import find_template

        result = find_template(_screen_with_button(), _button_template())

        self.assertTrue(result.found)
        self.assertEqual((result.match.x, result.match.y), BUTTON_AT)
        self.assertEqual(result.match.similarity, 1.0)

    def test_find_template_on_a_flat_screen_does_not_starve(self):
        """整屏一个颜色时，签名会在每一行都命中。

        扫描区间必须裁到「模板左上角的合法范围」里，否则前面几千个候选全是越界的废命中，
        配额被烧光之后，正确答案明明在屏幕中间也会报「没找到」——真机上踩过这个坑。
        """
        from app.core.matching import find_template

        flat = _frame(600, 400)
        template = _crop(flat, 300, 200, 120, 64)  # 从纯色背景里抠，最没特征的一种模板

        result = find_template(flat, template)

        self.assertTrue(result.found, "大面积同色背景也必须能找到")
        self.assertEqual((result.match.x, result.match.y), (0, 0), "同分取最左上")

    def test_find_template_prefers_the_distinctive_row_over_the_flat_one(self):
        """先锐利后退让：整行签名先命中时，就不该再去跑纯色签名那一大堆候选。"""
        from app.core.matching import find_template

        result = find_template(_screen_with_button(), _button_template())

        self.assertTrue(result.found)
        self.assertEqual(result.match.similarity, 1.0)
        self.assertLess(result.candidates, 10, "整行签名很锐利，候选应该很少")


        from app.core.matching import find_template

        result = find_template(_frame(200, 120), _button_template())

        self.assertFalse(result.found)
        self.assertIsNone(result.match)
        self.assertEqual(result.candidates, 0, "屏幕上没有同色区域，应该一个候选都没有")
        self.assertEqual(result.best_similarity, 0.0)

    def test_find_template_rejects_oversized_template(self):
        from app.core.matching import find_template

        result = find_template(_frame(20, 20), _button_template())

        self.assertFalse(result.found)
        self.assertEqual(result.candidates, 0)

    def test_find_template_verification_honours_tolerance(self):
        """探针所在的那一行没变、只是别处颜色微调时，容差就该起作用。"""
        from app.core.matching import find_template

        template = _button_template()
        shifted = _paint(
            _screen_with_button(),
            BUTTON_AT[0] + BUTTON_TEXT_AT[0],
            BUTTON_AT[1] + BUTTON_TEXT_AT[1],
            *BUTTON_TEXT_SIZE,
            (235, 235, 235),
        )

        self.assertTrue(find_template(shifted, template, tolerance=32).found)

        strict = find_template(shifted, template, tolerance=0)
        self.assertFalse(strict.found)
        self.assertGreater(strict.best_similarity, 0.5, "该找到候选，只是被容差否决了")

    def test_probe_is_byte_exact(self):
        """探针是逐字节比对：底色整体换了色调后，连候选都找不出来。

        这是刻意的取舍——不做多尺度、多色调搜索，才能保住「零依赖 + 每帧几十毫秒」。
        代价是目标程序换主题/DPI 缩放后必须重新截模板。这条测试把这个契约钉住，
        免得以后有人为了「更宽容一点」偷偷改成模糊搜索，把性能赔进去。
        """
        from app.core.matching import find_template

        shifted = _paint(
            _screen_with_button(), BUTTON_AT[0], BUTTON_AT[1], *BUTTON_SIZE, (200, 112, 8)
        )
        result = find_template(shifted, _button_template(), tolerance=200)

        self.assertFalse(result.found)
        self.assertEqual(result.candidates, 0)

    def test_find_template_picks_top_left_on_ties(self):
        from app.core.matching import find_template

        frame = _screen_with_button(position=(120, 60))
        frame = _paint(frame, BUTTON_AT[0], BUTTON_AT[1], *BUTTON_SIZE, BUTTON_COLOR)
        frame = _paint(
            frame,
            BUTTON_AT[0] + BUTTON_TEXT_AT[0],
            BUTTON_AT[1] + BUTTON_TEXT_AT[1],
            *BUTTON_TEXT_SIZE,
            BUTTON_TEXT_COLOR,
        )

        result = find_template(frame, _button_template())

        self.assertEqual(
            (result.match.x, result.match.y), BUTTON_AT, "同分时取最靠上、再最靠左的那个"
        )

    def test_find_template_coordinates_are_frame_local(self):
        """匹配坐标相对帧左上角；屏幕原点由调用方（引擎）加回去。"""
        from app.core.matching import find_template

        frame = _screen_with_button(origin=(-1920, -200))
        result = find_template(frame, _button_template())

        self.assertEqual((result.match.x, result.match.y), BUTTON_AT)
        self.assertEqual((frame.x + result.match.x, frame.y + result.match.y), (-1850, -150))


# ==================================================================== 存储
class TestProfileStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self):
        from app.storage.config import ProfileStore

        return ProfileStore(self.base)

    def test_save_load_list_delete(self):
        store = self._store()
        self.assertEqual(store.list_names(), [])

        store.save(Profile(name="方案A", steps=[ClickStep(x=9, y=8, repeat=2)]))
        store.save(Profile(name="方案B", steps=[DelayStep(delay_ms=50)]))

        self.assertEqual(store.list_names(), ["方案A", "方案B"])
        self.assertTrue(store.exists("方案A"))

        loaded = store.load("方案A")
        self.assertEqual(loaded.name, "方案A")
        self.assertEqual(loaded.steps[0].x, 9)

        self.assertTrue(store.delete("方案A"))
        self.assertFalse(store.delete("方案A"))
        self.assertEqual(store.list_names(), ["方案B"])

        # 原子写入不应留下临时文件
        leftovers = list(store.profiles_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_load_missing_raises(self):
        with self.assertRaises(ValueError):
            self._store().load("不存在的方案")

    def test_load_corrupted_raises(self):
        store = self._store()
        store.profiles_dir.mkdir(parents=True, exist_ok=True)
        (store.profiles_dir / "坏文件.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            store.load("坏文件")

    def test_invalid_filename_chars_are_sanitized(self):
        store = self._store()
        store.save(Profile(name="a/b:c*d", steps=[ClickStep()]))
        self.assertEqual(len(store.list_names()), 1)
        self.assertNotIn("/", store.list_names()[0])

    def test_rename(self):
        store = self._store()
        store.save(Profile(name="旧名", steps=[ClickStep()]))
        self.assertTrue(store.rename("旧名", "新名"))
        self.assertEqual(store.list_names(), ["新名"])
        self.assertFalse(store.rename("不存在", "别的"))

    def test_last_profile_setting(self):
        store = self._store()
        self.assertIsNone(store.get_last_profile())
        store.set_last_profile("方案B")
        self.assertEqual(store.get_last_profile(), "方案B")

        # 新实例应能从磁盘读回
        self.assertEqual(self._store().get_last_profile(), "方案B")


# ==================================================================== 引擎
class TestEngine(unittest.TestCase):
    """同一线程内直接调用 run()，信号走直连，无需事件循环。"""

    def setUp(self):
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])

    def _engine(self, backend, screen=None):
        from app.core.engine import MacroEngine

        return MacroEngine(backend, screen=screen)

    def test_step_order_and_rounds(self):
        backend = FakeBackend()
        engine = self._engine(backend)
        engine.run(
            Profile(
                name="顺序",
                steps=[
                    ClickStep(x=10, y=20, repeat=2, interval_ms=1),
                    DelayStep(delay_ms=5),
                    KeyStep(vk=0x41, repeat=1, interval_ms=1),
                ],
                repeat=2,
                start_delay_ms=0,
            )
        )

        # 每轮: click click key；共两轮
        self.assertEqual(backend.kinds, ["click", "click", "key", "click", "click", "key"])
        self.assertEqual(len(backend.clicks), 4)
        self.assertEqual([c[1] for c in backend.clicks], [10, 10, 10, 10])
        self.assertTrue(all(c[3] is MouseButton.LEFT for c in backend.clicks))

    def test_start_delay_is_applied(self):
        backend = FakeBackend()
        engine = self._engine(backend)

        begin = time.perf_counter()
        engine.run(
            Profile(steps=[ClickStep(x=1, y=1, repeat=1, interval_ms=1)], repeat=1, start_delay_ms=120)
        )
        elapsed = time.perf_counter() - begin

        self.assertEqual(len(backend.clicks), 1)
        self.assertGreaterEqual(elapsed, 0.10)

    def test_disabled_steps_are_skipped(self):
        backend = FakeBackend()
        engine = self._engine(backend)
        engine.run(
            Profile(
                steps=[
                    ClickStep(x=1, y=1, repeat=1, interval_ms=1),
                    ClickStep(x=2, y=2, repeat=1, interval_ms=1, enabled=False),
                ],
                repeat=1,
                start_delay_ms=0,
            )
        )
        self.assertEqual([c[1] for c in backend.clicks], [1])

    def test_coordinate_jitter_stays_in_range(self):
        backend = FakeBackend()
        engine = self._engine(backend)
        engine.run(
            Profile(
                steps=[ClickStep(x=100, y=100, repeat=20, interval_ms=1, jitter_px=3)],
                repeat=1,
                start_delay_ms=0,
            )
        )
        self.assertEqual(len(backend.clicks), 20)
        for _, x, y, _ in backend.clicks:
            self.assertIn(x, range(97, 104))
            self.assertIn(y, range(97, 104))

    def test_long_delay_can_be_interrupted(self):
        """停在 5 秒的延时步骤里也必须能立刻被 F10 打断。"""
        backend = FakeBackend()
        engine = self._engine(backend)
        engine.step_changed.connect(lambda index, total: engine.request_stop())

        begin = time.perf_counter()
        engine.run(
            Profile(steps=[DelayStep(delay_ms=5000)], repeat=0, start_delay_ms=0)
        )
        elapsed = time.perf_counter() - begin

        self.assertLess(elapsed, 1.0)
        self.assertFalse(engine.running)

    def test_infinite_repeat_until_stopped(self):
        backend = FakeBackend()
        engine = self._engine(backend)
        engine.progress.connect(lambda actions, *_rest: engine.request_stop() if actions >= 3 else None)

        begin = time.perf_counter()
        engine.run(Profile(steps=[ClickStep(x=1, y=1, repeat=1, interval_ms=50)], repeat=0))
        elapsed = time.perf_counter() - begin

        self.assertGreaterEqual(len(backend.clicks), 3)
        self.assertLess(elapsed, 3.0)

    def test_invalid_profile_reports_failure(self):
        backend = FakeBackend()
        engine = self._engine(backend)
        messages: list[str] = []
        engine.failed.connect(messages.append)

        engine.run(Profile(steps=[], repeat=1))  # 没有任何步骤

        self.assertEqual(backend.events, [])
        self.assertEqual(len(messages), 1)
        self.assertFalse(engine.running)

    def test_progress_signal_reports_rounds(self):
        backend = FakeBackend()
        engine = self._engine(backend)
        seen: list[tuple[int, int, int]] = []
        engine.progress.connect(lambda a, r, t: seen.append((a, r, t)))

        engine.run(
            Profile(steps=[ClickStep(x=1, y=1, repeat=2, interval_ms=1)], repeat=3, start_delay_ms=0)
        )

        self.assertTrue(seen)
        self.assertEqual(seen[-1][1], 3, "最后一轮应报告已完成 3 轮")
        self.assertEqual(seen[-1][2], 3, "总轮数应为 3")

    # ---------------------------------------------------------------- 移动轨迹
    def test_move_step_interpolates_along_polyline(self):
        """轨迹步骤要在起点和终点之间真的插值，而不是一步跳过去。"""
        backend = FakeBackend()
        engine = self._engine(backend)
        engine.run(
            Profile(
                steps=[MoveStep(points=[(0, 0), (100, 0), (100, 100)], duration_ms=80)],
                repeat=1,
                start_delay_ms=0,
            )
        )

        moves = [event for event in backend.events if event[0] == "move"]
        self.assertGreater(len(moves), 3, "应当产生一串中间点")
        self.assertEqual(moves[-1][1:], (100, 100), "最后必须精确落在终点")
        self.assertTrue(
            any(x == 100 and 0 < y < 100 for _, x, y in moves),
            "折线第二段（竖直方向）应当被走到",
        )
        self.assertNotIn("click", backend.kinds)

    def test_move_step_with_single_point_is_instant(self):
        backend = FakeBackend()
        engine = self._engine(backend)
        engine.run(
            Profile(
                steps=[MoveStep(points=[(5, 6)], duration_ms=500)],
                repeat=1,
                start_delay_ms=0,
            )
        )
        self.assertEqual(backend.events, [("move", 5, 6)])

    def test_move_step_zero_duration_jumps_to_end(self):
        """时长为 0 时不做插值，直接落到终点（避免忙循环）。"""
        backend = FakeBackend()
        engine = self._engine(backend)
        begin = time.perf_counter()
        engine.run(
            Profile(
                steps=[MoveStep(points=[(0, 0), (10, 0), (20, 0)], duration_ms=0)],
                repeat=1,
                start_delay_ms=0,
            )
        )
        self.assertLess(time.perf_counter() - begin, 1.0)
        self.assertEqual(backend.events, [("move", 20, 0)])

    def test_move_step_is_interruptible(self):
        """一条很长的轨迹也要能被 F10 立刻打断。"""
        backend = FakeBackend()
        engine = self._engine(backend)
        engine.step_changed.connect(lambda index, total: engine.request_stop())

        begin = time.perf_counter()
        engine.run(
            Profile(
                steps=[MoveStep(points=[(0, 0), (5000, 0)], duration_ms=30000)],
                repeat=0,
                start_delay_ms=0,
            )
        )
        self.assertLess(time.perf_counter() - begin, 1.0)
        self.assertFalse(engine.running)

    # ---------------------------------------------------------------- 找图
    def _wait_step(self, **overrides):
        params = {
            "template": _button_template().encode(),
            "timeout_ms": 500,
            "poll_interval_ms": 20,
        }
        params.update(overrides)
        return WaitStep(**params)

    def _sequence(self, frames):
        """按顺序吐帧：前面几帧没有目标，之后才出现目标。"""
        pending = list(frames)
        return lambda: pending.pop(0)

    def test_wait_step_waits_then_clicks_the_center(self):
        """目标出现前一直等，出现后点模板中心，坐标要带上虚拟屏幕原点。"""
        backend = FakeBackend()
        empty = _frame(200, 120, x=-1920, y=-200)
        screen = self._sequence([empty, empty, _screen_with_button(origin=(-1920, -200))])
        engine = self._engine(backend, screen=screen)

        engine.run(Profile(steps=[self._wait_step()], repeat=1, start_delay_ms=0))

        self.assertEqual(len(backend.clicks), 1)
        _, x, y, button = backend.clicks[0]
        self.assertEqual(
            (x, y), (-1920 + BUTTON_AT[0] + 20, -200 + BUTTON_AT[1] + 10), "应点击模板中心"
        )
        self.assertIs(button, MouseButton.LEFT)

    def test_wait_step_can_skip_clicking(self):
        backend = FakeBackend()
        engine = self._engine(backend, screen=_screen_with_button)

        engine.run(Profile(steps=[self._wait_step(click=False)], repeat=1, start_delay_ms=0))

        self.assertEqual(backend.events, [], "关掉点击后只等不点")

    def test_wait_step_applies_click_offset(self):
        backend = FakeBackend()
        engine = self._engine(backend, screen=_screen_with_button)

        engine.run(
            Profile(
                steps=[self._wait_step(button=MouseButton.RIGHT, offset_x=5, offset_y=-4)],
                repeat=1,
                start_delay_ms=0,
            )
        )

        _, x, y, button = backend.clicks[0]
        self.assertEqual((x, y), (BUTTON_AT[0] + 20 + 5, BUTTON_AT[1] + 10 - 4))
        self.assertIs(button, MouseButton.RIGHT)

    def test_wait_step_timeout_continues_by_default(self):
        backend = FakeBackend()
        engine = self._engine(backend, screen=lambda: _frame(200, 120))
        dumps: list = []
        engine.wait_timeout.connect(dumps.append)

        engine.run(
            Profile(
                steps=[
                    self._wait_step(timeout_ms=30),
                    ClickStep(x=9, y=9, repeat=1, interval_ms=1),
                ],
                repeat=1,
                start_delay_ms=0,
            )
        )

        self.assertEqual([event[0] for event in backend.events], ["click"])
        self.assertEqual(backend.clicks[0][1:3], (9, 9), "超时后应继续执行下一步")
        self.assertEqual(dumps, [], "「继续」模式下不该每次超时都写截图")

    def test_wait_step_timeout_stops_and_hands_over_last_frame(self):
        backend = FakeBackend()
        frame = _frame(200, 120)
        engine = self._engine(backend, screen=lambda: frame)
        failures: list[str] = []
        dumps: list = []
        engine.failed.connect(failures.append)
        engine.wait_timeout.connect(dumps.append)

        engine.run(
            Profile(
                steps=[
                    self._wait_step(timeout_ms=30, on_timeout=ON_TIMEOUT_STOP),
                    ClickStep(x=9, y=9, repeat=1, interval_ms=1),
                ],
                repeat=1,
                start_delay_ms=0,
            )
        )

        self.assertEqual(backend.events, [], "中止时后面的步骤不该再执行")
        self.assertEqual(len(failures), 1)
        self.assertIn("超时", failures[0])
        self.assertEqual(len(dumps), 1, "中止时应把最后一帧交出去供落盘诊断")
        self.assertIs(dumps[0], frame)

    def test_wait_step_timeout_zero_searches_once(self):
        """超时填 0 = 只找一次，不等待。"""
        backend = FakeBackend()
        calls: list[int] = []

        def screen():
            calls.append(1)
            return _frame(200, 120)

        engine = self._engine(backend, screen=screen)
        engine.run(Profile(steps=[self._wait_step(timeout_ms=0)], repeat=1, start_delay_ms=0))

        self.assertEqual(len(calls), 1)
        self.assertEqual(backend.events, [])

    def test_wait_step_is_interruptible(self):
        """一个上限 60 秒的等待也必须能被 F10 立刻打断。"""
        backend = FakeBackend()
        engine = self._engine(backend, screen=lambda: _frame(200, 120))
        engine.step_changed.connect(lambda index, total: engine.request_stop())

        begin = time.perf_counter()
        engine.run(
            Profile(steps=[self._wait_step(timeout_ms=60000)], repeat=0, start_delay_ms=0)
        )

        self.assertLess(time.perf_counter() - begin, 1.0)
        self.assertEqual(backend.events, [])
        self.assertFalse(engine.running)

    def test_wait_step_reports_capture_failure(self):
        def broken():
            raise OSError("被系统拒绝")

        backend = FakeBackend()
        engine = self._engine(backend, screen=broken)
        failures: list[str] = []
        engine.failed.connect(failures.append)

        engine.run(Profile(steps=[self._wait_step()], repeat=1, start_delay_ms=0))

        self.assertEqual(len(failures), 1)
        self.assertIn("抓取屏幕", failures[0])
        self.assertFalse(engine.running)

    def test_engine_switches_backend_only_when_idle(self):
        """前端可在 start 前换后端；运行途中必须拒绝，免得同一次运行混用两种注入。"""
        from app.core.engine import MacroEngine

        engine = MacroEngine(FakeBackend())
        other = FakeBackend()
        engine.set_backend(other)
        self.assertIs(engine._backend, other)

        engine._running = True
        try:
            with self.assertRaises(RuntimeError):
                engine.set_backend(FakeBackend())
        finally:
            engine._running = False


# ==================================================================== 录制转换
def _down(t, x=0, y=0, button="left"):
    return InputEvent("click", t, x=x, y=y, button=button, is_down=True)


def _up(t, x=0, y=0, button="left"):
    return InputEvent("click", t, x=x, y=y, button=button, is_down=False)


def _kdown(t, vk):
    return InputEvent("key", t, vk=vk, is_down=True)


def _kup(t, vk):
    return InputEvent("key", t, vk=vk, is_down=False)


def _move(t, x, y):
    return InputEvent("move", t, x=x, y=y)


class TestRecording(unittest.TestCase):
    """录制事件 → 方案步骤的转换规则。"""

    def _build(self, events, **kwargs):
        from app.core.recording import RecordSettings, build_profile

        return build_profile(events, name="测试录制", settings=RecordSettings(**kwargs))

    def test_single_click_becomes_one_step(self):
        profile = self._build([_down(1.0, 10, 20), _up(1.05, 10, 20)])
        self.assertEqual(len(profile.steps), 1)
        step = profile.steps[0]
        self.assertIsInstance(step, ClickStep)
        self.assertEqual((step.x, step.y), (10, 20))
        self.assertEqual(step.repeat, 1)
        self.assertIs(step.button, MouseButton.LEFT)
        profile.validate()  # 生成结果必须能直接回放

    def test_click_uses_press_position(self):
        """坐标取「按下」那一刻，抬起时手指/鼠标可能已经飘了。"""
        profile = self._build([_down(1.0, 10, 20), _up(1.05, 13, 27)])
        step = profile.steps[0]
        self.assertEqual((step.x, step.y), (10, 20))

    def test_adjacent_clicks_merge_into_repeat(self):
        """连点两下同一个点 = 一个 repeat=2 的步骤，而不是两个步骤。"""
        profile = self._build(
            [
                _down(1.00, 10, 20),
                _up(1.05, 10, 20),
                _down(1.15, 11, 20),  # 有 1px 抖动，仍应合并
                _up(1.20, 11, 20),
            ]
        )
        self.assertEqual(len(profile.steps), 1)
        step = profile.steps[0]
        self.assertIsInstance(step, ClickStep)
        self.assertEqual(step.repeat, 2)
        self.assertEqual(step.interval_ms, 150)

    def test_distant_clicks_do_not_merge(self):
        profile = self._build(
            [
                _down(1.00, 10, 20),
                _up(1.05, 10, 20),
                _down(1.10, 400, 500),
                _up(1.15, 400, 500),
            ]
        )
        clicks = [s for s in profile.steps if isinstance(s, ClickStep)]
        self.assertEqual(len(clicks), 2)
        self.assertEqual(clicks[1].repeat, 1)

    def test_slow_clicks_do_not_merge(self):
        """间隔超过 merge_max_gap_ms 就当成两次独立点击（中间还会有延时步骤）。"""
        profile = self._build(
            [
                _down(1.0, 10, 20),
                _up(1.05, 10, 20),
                _down(3.0, 10, 20),
                _up(3.05, 10, 20),
            ]
        )
        clicks = [s for s in profile.steps if isinstance(s, ClickStep)]
        self.assertEqual(len(clicks), 2)
        self.assertGreaterEqual(len([s for s in profile.steps if isinstance(s, DelayStep)]), 1)

    def test_gap_becomes_delay_step(self):
        profile = self._build(
            [
                _down(1.0, 10, 10),
                _up(1.05, 10, 10),
                _down(1.5, 500, 500),
                _up(1.55, 500, 500),
            ]
        )
        delays = [s for s in profile.steps if isinstance(s, DelayStep)]
        self.assertEqual(len(delays), 1)
        self.assertEqual(delays[0].delay_ms, 450)
        self.assertEqual([type(s) for s in profile.steps], [ClickStep, DelayStep, ClickStep])

    def test_short_gap_does_not_become_delay(self):
        """小于 min_delay_ms 的间隔不值得单独占一行。"""
        profile = self._build(
            [
                _down(1.000, 10, 10),
                _up(1.005, 10, 10),
                _down(1.020, 10, 10),
                _up(1.025, 10, 10),
            ],
            merge_max_gap_ms=1,
        )
        self.assertEqual([type(s) for s in profile.steps], [ClickStep, ClickStep])

    def test_key_press_becomes_key_step(self):
        profile = self._build([_kdown(2.0, 0x41), _kup(2.05, 0x41)])
        self.assertEqual(len(profile.steps), 1)
        step = profile.steps[0]
        self.assertIsInstance(step, KeyStep)
        self.assertEqual(step.vk, 0x41)
        self.assertEqual(step.repeat, 1)

    def test_auto_repeat_keydowns_are_ignored(self):
        """按住不放会产生一串 WM_KEYDOWN，只能记一次。"""
        profile = self._build(
            [
                _kdown(2.00, 0x41),
                _kdown(2.03, 0x41),
                _kdown(2.06, 0x41),
                _kup(2.09, 0x41),
                _kdown(2.10, 0x41),
                _kup(2.15, 0x41),
            ]
        )
        keys = [s for s in profile.steps if isinstance(s, KeyStep)]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0].repeat, 2)
        self.assertEqual(keys[0].interval_ms, 100)

    def test_key_still_held_at_end_is_kept(self):
        """录制结束时还按着键，也不能把这次按键整段丢掉。"""
        profile = self._build([_kdown(2.0, 0x41)])
        keys = [s for s in profile.steps if isinstance(s, KeyStep)]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0].vk, 0x41)

    def test_click_without_release_is_dropped(self):
        """只有按下没有抬起（异常序列）时忽略，而不是崩掉。"""
        profile = self._build([_down(1.0, 10, 20)])
        self.assertEqual(profile.steps, [])

    # ---------------------------------------------------------------- 轨迹
    def test_moves_collapse_into_single_trajectory_step(self):
        profile = self._build(
            [_move(1.00, 0, 0), _move(1.01, 5, 0), _move(1.02, 20, 0), _move(1.03, 40, 0)]
        )
        self.assertEqual(len(profile.steps), 1)
        step = profile.steps[0]
        self.assertIsInstance(step, MoveStep)
        self.assertEqual(step.points[0], (0, 0))
        self.assertEqual(step.points[-1], (40, 0))
        self.assertLess(len(step.points), 4, "抖动点应被抽稀")
        self.assertEqual(step.duration_ms, 30)

    def test_tiny_movement_is_dropped(self):
        """2px 的手抖不该产生一个步骤。"""
        profile = self._build([_move(1.00, 0, 0), _move(1.01, 1, 0), _move(1.02, 2, 0)])
        self.assertEqual(profile.steps, [])

    def test_moves_can_be_disabled(self):
        profile = self._build(
            [_move(1.00, 0, 0), _move(1.02, 80, 0), _down(1.10, 80, 0), _up(1.15, 80, 0)],
            record_moves=False,
        )
        self.assertEqual([type(s) for s in profile.steps], [ClickStep])

    def test_move_precedes_the_click_that_ends_it(self):
        profile = self._build(
            [_move(1.00, 0, 0), _move(1.05, 60, 0), _down(1.10, 60, 0), _up(1.15, 60, 0)]
        )
        self.assertEqual(
            [type(s) for s in profile.steps if not isinstance(s, DelayStep)],
            [MoveStep, ClickStep],
        )

    def test_trajectory_can_be_interrupted_by_key(self):
        profile = self._build(
            [
                _move(1.00, 0, 0),
                _move(1.05, 60, 0),
                _kdown(1.10, 0x0D),
                _kup(1.15, 0x0D),
                _move(1.20, 60, 0),
                _move(1.25, 120, 0),
            ]
        )
        self.assertEqual(
            [type(s) for s in profile.steps if not isinstance(s, DelayStep)],
            [MoveStep, KeyStep, MoveStep],
        )
        self.assertGreaterEqual(
            len([s for s in profile.steps if isinstance(s, DelayStep)]), 1
        )

    def test_recording_gets_a_start_delay(self):
        from app.core.recording import RECORDING_START_DELAY_MS

        profile = self._build([_down(1.0, 1, 1), _up(1.05, 1, 1)])
        self.assertEqual(profile.start_delay_ms, RECORDING_START_DELAY_MS)
        self.assertEqual(profile.repeat, 1)

    def test_summarize_is_readable(self):
        from app.core.recording import summarize

        profile = self._build(
            [
                _down(1.00, 10, 10),
                _up(1.05, 10, 10),
                _kdown(1.50, 0x41),
                _kup(1.55, 0x41),
            ]
        )
        text = summarize(profile)
        self.assertIn("点击 1 个", text)
        self.assertIn("按键 1 个", text)
        self.assertEqual(summarize(Profile()), "没有记录到任何动作")

    def test_empty_recording_produces_empty_profile(self):
        profile = self._build([])
        self.assertEqual(profile.steps, [])


# ==================================================================== Win32 钩子
class TestHooks(unittest.TestCase):
    """真实安装一次低级钩子，验证 ctypes 声明和回调引用都是对的。"""

    def test_install_and_unhook(self):
        from app.win.hooks import InputRecorder

        recorder = InputRecorder()
        self.assertFalse(recorder.recording)
        try:
            self.assertTrue(recorder.start(swallow=True, passthrough_vks=[0x79]))
        except OSError as exc:  # pragma: no cover - 受限环境
            self.skipTest(f"当前环境无法安装低级钩子: {exc}")

        self.assertTrue(recorder.recording)
        self.assertTrue(recorder.swallow)
        events = recorder.stop()
        self.assertFalse(recorder.recording)
        self.assertEqual(events, [])
        self.assertEqual(recorder.action_count, 0)

    def test_stop_without_start_is_safe(self):
        from app.win.hooks import InputRecorder

        recorder = InputRecorder()
        self.assertEqual(recorder.stop(), [])
        self.assertFalse(recorder.recording)

    def test_double_start_is_rejected(self):
        from app.win.hooks import InputRecorder

        recorder = InputRecorder()
        try:
            self.assertTrue(recorder.start())
        except OSError as exc:  # pragma: no cover - 受限环境
            self.skipTest(f"当前环境无法安装低级钩子: {exc}")
        try:
            self.assertFalse(recorder.start())
        finally:
            recorder.stop()

    def test_control_hotkeys_are_not_recorded(self):
        """F8/F9/F10/F11 是工具自己的控制键，不能混进录制的宏里。"""
        from app.win.hooks import WM_KEYDOWN, WM_KEYUP, InputRecorder

        recorder = InputRecorder()
        recorder._passthrough_vks = {0x79}  # F10
        recorder._handle_key(WM_KEYDOWN, 0x79)
        recorder._handle_key(WM_KEYUP, 0x79)
        recorder._handle_key(WM_KEYDOWN, 0x41)
        recorder._handle_key(WM_KEYUP, 0x41)

        self.assertEqual([event.vk for event in recorder.events], [0x41, 0x41])
        self.assertEqual(recorder.action_count, 1)

    def test_auto_repeat_is_recorded_once(self):
        """按住不放只会产生一串 WM_KEYDOWN，动作计数只能加一次。"""
        from app.win.hooks import WM_KEYDOWN, WM_KEYUP, InputRecorder

        recorder = InputRecorder()
        for _ in range(5):
            recorder._handle_key(WM_KEYDOWN, 0x41)
        recorder._handle_key(WM_KEYUP, 0x41)

        self.assertEqual(recorder.action_count, 1)
        self.assertEqual(len(recorder.events), 2)

    def test_click_counts_as_one_action(self):
        from app.win.hooks import (
            WM_LBUTTONDOWN,
            WM_LBUTTONUP,
            WM_MOUSEMOVE,
            InputRecorder,
        )

        recorder = InputRecorder()
        recorder._handle_mouse(WM_MOUSEMOVE, 1, 2)
        recorder._handle_mouse(WM_MOUSEMOVE, 3, 4)
        recorder._handle_mouse(WM_LBUTTONDOWN, 5, 6)
        recorder._handle_mouse(WM_LBUTTONUP, 5, 6)

        self.assertEqual(recorder.action_count, 1, "按下+抬起算一个动作")
        self.assertEqual(recorder.move_count, 2)
        kinds = [event.kind for event in recorder.events]
        self.assertEqual(kinds, ["move", "move", "click", "click"])
        self.assertTrue(recorder.events[2].is_down)
        self.assertFalse(recorder.events[3].is_down)

    def test_passthrough_hotkeys_are_kept_by_start(self):
        from app.win.hooks import InputRecorder

        recorder = InputRecorder()
        try:
            self.assertTrue(recorder.start(swallow=False, passthrough_vks=[0x78, 0x79]))
        except OSError as exc:  # pragma: no cover - 受限环境
            self.skipTest(f"当前环境无法安装低级钩子: {exc}")
        try:
            self.assertEqual(recorder._passthrough_vks, {0x78, 0x79})
            self.assertFalse(recorder.swallow)
        finally:
            recorder.stop()

    # ---------------------------------------------------------------- 钩子回调
    # 直接调用钩子过程，绕开真实钩子，验证「吃事件 / 放行」的判定逻辑。
    def _mouse_struct(self, x, y, flags=0):
        from app.win.hooks import MSLLHOOKSTRUCT

        struct = MSLLHOOKSTRUCT()
        struct.pt.x = x
        struct.pt.y = y
        struct.flags = flags
        return struct

    def _key_struct(self, vk, flags=0):
        from app.win.hooks import KBDLLHOOKSTRUCT

        struct = KBDLLHOOKSTRUCT()
        struct.vkCode = vk
        struct.flags = flags
        return struct

    def test_mouse_hook_records_and_swallows(self):
        from app.win.hooks import (
            LLMHF_INJECTED,
            WM_LBUTTONDOWN,
            WM_MOUSEMOVE,
            InputRecorder,
        )

        recorder = InputRecorder()
        recorder._recording = True
        recorder._swallow = True

        move = self._mouse_struct(11, 22)
        self.assertEqual(recorder._mouse_hook_proc(0, WM_MOUSEMOVE, ctypes.addressof(move)), 1)
        self.assertEqual(recorder.move_count, 1)

        down = self._mouse_struct(33, 44)
        self.assertEqual(recorder._mouse_hook_proc(0, WM_LBUTTONDOWN, ctypes.addressof(down)), 1)
        self.assertEqual(recorder.action_count, 1)
        self.assertEqual((recorder.events[-1].x, recorder.events[-1].y), (33, 44))

        # 注入事件必须放行且不记录，否则回放会把自己录进去、也会和其它工具打架
        injected = self._mouse_struct(55, 66, flags=LLMHF_INJECTED)
        self.assertEqual(
            recorder._mouse_hook_proc(0, WM_LBUTTONDOWN, ctypes.addressof(injected)), 0
        )
        self.assertEqual(recorder.action_count, 1)
        self.assertEqual(recorder.move_count, 1)

    def test_mouse_hook_passes_through_when_not_swallowing(self):
        from app.win.hooks import WM_LBUTTONDOWN, InputRecorder

        recorder = InputRecorder()
        recorder._recording = True
        recorder._swallow = False

        down = self._mouse_struct(1, 2)
        self.assertEqual(recorder._mouse_hook_proc(0, WM_LBUTTONDOWN, ctypes.addressof(down)), 0)
        self.assertEqual(recorder.action_count, 1, "不屏蔽时依然要记录")

    def test_key_hook_passes_control_hotkeys(self):
        from app.win.hooks import LLKHF_INJECTED, WM_KEYDOWN, WM_SYSKEYDOWN, InputRecorder

        recorder = InputRecorder()
        recorder._recording = True
        recorder._swallow = True
        recorder._passthrough_vks = {0x79}  # F10

        hotkey = self._key_struct(0x79)
        self.assertEqual(
            recorder._key_hook_proc(0, WM_KEYDOWN, ctypes.addressof(hotkey)), 0,
            "控制热键必须放行，否则 RegisterHotKey 收不到、录制停不下来",
        )
        self.assertEqual(recorder.action_count, 0, "控制热键不该被录进宏里")

        alt_enter = self._key_struct(0x0D)
        self.assertEqual(
            recorder._key_hook_proc(0, WM_SYSKEYDOWN, ctypes.addressof(alt_enter)), 1,
            "Alt+Enter 这类系统按键同样要能被录",
        )
        self.assertEqual(recorder.action_count, 1)

        injected = self._key_struct(0x41, flags=LLKHF_INJECTED)
        self.assertEqual(recorder._key_hook_proc(0, WM_KEYDOWN, ctypes.addressof(injected)), 0)
        self.assertEqual(recorder.action_count, 1)

    def test_hook_proc_survives_recorder_errors(self):
        """钩子里抛异常会让系统摘掉钩子，所以必须吞掉记录过程中的任何错误。"""
        from app.win.hooks import WM_LBUTTONDOWN, InputRecorder

        recorder = InputRecorder()
        recorder._recording = True
        recorder._swallow = True

        def boom(*_args):
            raise RuntimeError("模拟记录失败")

        recorder._handle_mouse = boom
        down = self._mouse_struct(1, 2)
        self.assertEqual(
            recorder._mouse_hook_proc(0, WM_LBUTTONDOWN, ctypes.addressof(down)), 1
        )

    def test_hook_proc_ignores_negative_code(self):
        """n_code < 0 表示必须原样传给下一个钩子，不能做任何处理。"""
        from app.win.hooks import WM_LBUTTONDOWN, InputRecorder

        recorder = InputRecorder()
        recorder._recording = True
        recorder._swallow = True
        down = self._mouse_struct(1, 2)
        self.assertEqual(
            recorder._mouse_hook_proc(-1, WM_LBUTTONDOWN, ctypes.addressof(down)), 0
        )
        self.assertEqual(recorder.events, [])


# ==================================================================== UI 装配
class TestUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_step_editor_crud(self):
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_editor import StepEditor

        editor = StepEditor(PickCoordinator())
        try:
            editor.set_steps(
                [ClickStep(x=1, y=1), DelayStep(delay_ms=5), KeyStep(vk=0x41)]
            )
            self.assertEqual(editor._table.rowCount(), 3)
            self.assertEqual(len(editor.steps()), 3)

            # 上移/下移
            editor.select_row(0)
            editor.move_selected(1)
            self.assertIsInstance(editor.steps()[1], ClickStep)

            # F8 回写坐标
            row = next(
                index for index, step in enumerate(editor.steps()) if isinstance(step, ClickStep)
            )
            self.assertTrue(editor.update_click_coords(row, 777, 888))
            step = editor.step_at(row)
            self.assertEqual((step.x, step.y), (777, 888))

            # 非点击步骤拒绝写入坐标
            delay_row = next(
                index for index, step in enumerate(editor.steps()) if isinstance(step, DelayStep)
            )
            self.assertFalse(editor.update_click_coords(delay_row, 1, 1))

            # 取消勾选会改变 enabled
            item = editor._table.item(row, 0)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.assertFalse(editor.step_at(row).enabled)
        finally:
            editor.deleteLater()

    def test_step_dialog_pick_roundtrip(self):
        """对话框点「拾取坐标」时应先隐藏自己，拾取后自动恢复并回填坐标。"""
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        picker = PickCoordinator()
        dialog = StepDialog(ClickStep(x=1, y=2), picker)
        try:
            dialog.show()
            self.assertTrue(dialog.isVisible())

            dialog.start_pick()
            self.assertFalse(dialog.isVisible(), "取点时对话框必须让开，否则会挡住目标位置")
            self.assertTrue(picker.busy)

            picker._picker.picked.emit(321, 654)  # 模拟鼠标左键拾取
            self.assertTrue(dialog.isVisible(), "拾取后应恢复对话框")
            self.assertFalse(picker.busy)

            step = dialog.result_step()
            self.assertEqual((step.x, step.y), (321, 654))
            self.assertIs(step.button, MouseButton.LEFT)
            self.assertIn("左键", step.summary(), "按键应显示中文，而不是枚举的原始值")
        finally:
            dialog.deleteLater()

    def test_step_dialog_cancel_pick_restores_dialog(self):
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        picker = PickCoordinator()
        dialog = StepDialog(ClickStep(x=7, y=8), picker)
        try:
            dialog.show()
            dialog.start_pick()
            picker._picker.canceled.emit()

            self.assertTrue(dialog.isVisible())
            self.assertEqual((dialog.result_step().x, dialog.result_step().y), (7, 8))
        finally:
            dialog.deleteLater()

    def test_non_click_dialog_ignores_pick(self):
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        picker = PickCoordinator()
        dialog = StepDialog(DelayStep(delay_ms=5), picker)
        try:
            dialog.show()
            dialog.start_pick()
            self.assertFalse(picker.busy, "延时步骤没有坐标可填，不应启动遮罩")
            self.assertTrue(dialog.isVisible())
        finally:
            dialog.deleteLater()

    def test_step_editor_supports_move_steps(self):
        from unittest import mock

        from app.ui.position_picker import PickCoordinator
        from app.ui.step_editor import StepEditor

        editor = StepEditor(PickCoordinator())
        try:
            editor.set_steps([])
            # add_step 会弹出模态编辑框；测试里直接放行，只验证默认值的构造
            with mock.patch.object(StepEditor, "_open_dialog", lambda self, step: step):
                editor.add_step(StepKind.MOVE)
            self.assertEqual(len(editor.steps()), 1)
            step = editor.steps()[0]
            self.assertIsInstance(step, MoveStep)
            step.validate()

            # 移动步骤：F8 改的是终点，起点保留、拐点拉直
            step.points = [(10, 10), (50, 50), (90, 20)]
            self.assertTrue(editor.update_click_coords(0, 400, 300))
            self.assertEqual(editor.step_at(0).points, [(10, 10), (400, 300)])

            # 空轨迹也能被拾取成一个单点移动
            editor.step_at(0).points = []
            self.assertTrue(editor.update_click_coords(0, 7, 8))
            self.assertEqual(editor.step_at(0).points, [(7, 8)])
        finally:
            editor.deleteLater()

    def test_move_dialog_pick_replaces_endpoint(self):
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        picker = PickCoordinator()
        dialog = StepDialog(MoveStep(points=[(10, 10), (50, 50), (90, 20)], duration_ms=700), picker)
        try:
            dialog.show()
            dialog.start_pick()
            self.assertTrue(picker.busy, "移动步骤也要能取点")
            picker._picker.picked.emit(400, 300)

            step = dialog.result_step()
            self.assertIsInstance(step, MoveStep)
            self.assertEqual(step.points, [(10, 10), (400, 300)], "起点保留，终点换成拾取点")
            self.assertEqual(step.duration_ms, 700, "时长不应被取点改动")
        finally:
            dialog.deleteLater()

    def test_move_dialog_without_points_shows_hint(self):
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        dialog = StepDialog(MoveStep(points=[], duration_ms=100), PickCoordinator())
        try:
            self.assertIn("拾取", dialog._path_label.text())
        finally:
            dialog.deleteLater()

    def test_pick_position_accepts_move_and_rejects_delay(self):
        """主窗口的 F8 也要能改轨迹终点，但延时/按键步骤没有坐标可改。"""
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                window._editor.set_steps([DelayStep(delay_ms=5)])
                window._editor.select_row(0)
                window.pick_position()
                self.assertFalse(window._picker.busy, "延时步骤不该启动取点遮罩")

                window._editor.set_steps([MoveStep(points=[(1, 1), (2, 2)], duration_ms=100)])
                window._editor.select_row(0)
                window.pick_position()
                self.assertTrue(window._picker.busy)

                window._picker._picker.picked.emit(500, 600)
                self.assertEqual(window._editor.step_at(0).points, [(1, 1), (500, 600)])
                self.assertFalse(window._picker.busy)
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_recording_flow_appends_steps(self):
        """F11 开始录制 → F10 结束 → 录到的动作进步骤表。"""
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import HK_RECORD, HK_START, HK_STOP, MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                before = len(window._editor.steps())

                window._hotkey_ready[HK_STOP] = True
                window._on_hotkey(HK_RECORD)
                self.assertTrue(window._record.recording)
                self.assertTrue(window._record.recorder.recording)
                self.assertFalse(window._record_button.isEnabled())
                self.assertTrue(window._stop_button.isEnabled())

                # 录制中其它热键应被忽略
                window._on_hotkey(HK_START)
                self.assertFalse(window._run.running)

                # 冒充两条已录到的事件
                window._record.recorder._events.extend(
                    [_down(1.0, 11, 22), _up(1.05, 11, 22)]
                )
                window._record.recorder._action_count = 1

                window._record.ask_how_to_apply = lambda summary: "append"
                window._on_hotkey(HK_STOP)

                self.assertFalse(window._record.recording)
                self.assertFalse(window._record.recorder.recording)
                self.assertFalse(window._record.overlay.isVisible())
                self.assertGreater(len(window._editor.steps()), before)

                appended = window._editor.steps()[-1]
                self.assertIsInstance(appended, ClickStep)
                self.assertEqual((appended.x, appended.y), (11, 22))
                self.assertTrue(window._record_button.isEnabled())
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_recording_flow_can_replace_steps(self):
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import HK_STOP, MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                window._hotkey_ready[HK_STOP] = True
                window.start_recording()
                window._record.recorder._events.extend([_down(1.0, 5, 5), _up(1.05, 5, 5)])
                window._record.ask_how_to_apply = lambda summary: "replace"
                window.stop_recording()

                steps = window._editor.steps()
                self.assertEqual(len(steps), 1)
                self.assertEqual((steps[0].x, steps[0].y), (5, 5))
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_recording_flow_can_discard(self):
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import HK_STOP, MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                window._hotkey_ready[HK_STOP] = True
                before = [step.to_dict() for step in window._editor.steps()]
                window.start_recording()
                window._record.recorder._events.extend([_down(1.0, 5, 5), _up(1.05, 5, 5)])
                window._record.ask_how_to_apply = lambda summary: "discard"
                window.stop_recording()
                self.assertEqual([step.to_dict() for step in window._editor.steps()], before)
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_recording_ignores_empty_capture(self):
        """什么都没录到时只提示，绝不清空用户已有的步骤。"""
        from unittest import mock

        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import HK_STOP, MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                window._hotkey_ready[HK_STOP] = True
                before = len(window._editor.steps())
                window.start_recording()
                with mock.patch("app.ui.recording_controller.QMessageBox") as box:
                    window.stop_recording()
                self.assertEqual(len(window._editor.steps()), before)
                self.assertFalse(window._record.recording)
                self.assertFalse(window._record.overlay.isVisible())
                self.assertEqual(box.information.call_count, 1)
                self.assertEqual(box.question.call_count, 0)
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_recording_is_refused_without_stop_hotkey(self):
        """「屏蔽输入」下 F10 不可用时必须拒绝开录，否则用户会被锁在点不动的系统里。"""
        from unittest import mock

        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import HK_STOP, MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                window._hotkey_ready[HK_STOP] = False
                window._record_swallow_check.setChecked(True)
                with mock.patch("app.ui.recording_controller.QMessageBox") as box:
                    window.start_recording()

                self.assertFalse(window._record.recording)
                self.assertFalse(window._record.recorder.recording)
                self.assertEqual(box.warning.call_count, 1)
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_recording_without_swallow_only_warns_without_stop_hotkey(self):
        """不屏蔽输入时 F10 只是「方便」，缺了也能录（还可以从托盘结束）。"""
        from unittest import mock

        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import HK_STOP, MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                window._hotkey_ready[HK_STOP] = False
                with mock.patch("app.ui.recording_controller.QMessageBox") as box:
                    window.start_recording()

                self.assertTrue(window._record.recording)
                self.assertTrue(window._record.recorder.recording)
                self.assertFalse(window._record.recorder.swallow)
                box.warning.assert_not_called()

                window._record.recorder._events.extend([_down(1.0, 1, 1), _up(1.05, 1, 1)])
                window._record.ask_how_to_apply = lambda summary: "discard"
                window.toggle_recording()
                self.assertFalse(window._record.recording)
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_recording_swallow_mode_is_honoured(self):
        """勾了「屏蔽输入」，钩子就必须真的吃掉事件。"""
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                window._hotkey_ready[2] = True
                window._record_swallow_check.setChecked(True)
                window.start_recording()
                self.assertTrue(window._record.recorder.swallow)
                self.assertTrue(window._record.overlay._swallow)

                window._record.recorder._events.extend([_down(1.0, 1, 1), _up(1.05, 1, 1)])
                window._record.ask_how_to_apply = lambda summary: "discard"
                window.stop_recording()
                self.assertFalse(window._record.recording)
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_recording_is_refused_while_engine_runs(self):
        from unittest import mock

        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import MainWindow
        from app.ui.run_controller import RunController
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                # 让回放看起来正在跑，但不动真线程
                with mock.patch.object(
                    RunController, "running", new_callable=mock.PropertyMock, return_value=True
                ):
                    with mock.patch("app.ui.recording_controller.QMessageBox") as box:
                        window.start_recording()
                    self.assertFalse(window._record.recording)
                    self.assertFalse(window._record.recorder.recording)
                    box.warning.assert_not_called()  # 只提示日志，不弹窗
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_toggle_recording(self):
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                window._hotkey_ready[2] = True
                window.toggle_recording()
                self.assertTrue(window._record.recording)
                self.assertTrue(window._record.recorder.recording)

                window._record.recorder._events.extend([_down(1.0, 3, 4), _up(1.05, 3, 4)])
                window._record.ask_how_to_apply = lambda summary: "discard"
                window.toggle_recording()
                self.assertFalse(window._record.recording)
                self.assertFalse(window._record.recorder.recording)
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_main_window_exposes_record_controls(self):
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            window = MainWindow(SendInputBackend(), HotkeyManager(self.app), store=store)
            try:
                self.assertIn("录制", window._record_button.text())
                self.assertTrue(window._record_moves_check.isChecked())
                self.assertFalse(
                    window._record_swallow_check.isChecked(),
                    "默认不屏蔽输入：屏蔽后依赖上一步结果的界面就没法录了",
                )
                self.assertIn("F11", window.windowTitle() + window.statusBar().currentMessage())
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_main_window_backend_switch(self):
        """输入方式下拉：前台 → 后台 PostMessage（可绑定窗口）→ UIA（校验窗口）。"""
        from unittest import mock

        from app.input import postmessage as pm
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import MODE_POSTMESSAGE, MODE_UIA, MainWindow
        from app.win.hotkey import HotkeyManager

        def select(window, mode):
            """像用户那样在下拉框里选一项。"""
            window._mode_combo.setCurrentIndex(window._mode_combo.findData(mode))
            self.app.processEvents()

        with tempfile.TemporaryDirectory() as tmp:
            foreground = SendInputBackend()
            window = MainWindow(
                foreground, HotkeyManager(self.app), store=ProfileStore(Path(tmp))
            )
            try:
                # 默认前台注入：不需要绑定窗口
                self.assertEqual(window._mode, "foreground")
                self.assertIs(window._active_backend(), foreground)
                self.assertFalse(
                    window._bind_button.isEnabled(), "前台模式没有可绑定的东西"
                )

                # 切到后台：换成 PostMessage 后端，绑定按钮可用
                select(window, MODE_POSTMESSAGE)
                backend = window._active_backend()
                self.assertIsNot(backend, foreground)
                self.assertEqual(backend.name, "postmessage")
                self.assertTrue(window._bind_button.isEnabled())
                self.assertIn("未绑定", window._bind_label.text())

                # 绑定窗口：坐标点 → 顶层窗口，成功后标签显示目标
                with mock.patch.multiple(
                    pm,
                    window_from_point=lambda x, y: 0x1234,
                    is_window=lambda hwnd: True,
                    root_window=lambda hwnd: 0x1234,
                    describe=lambda hwnd: "记事本 [Notepad]",
                ):
                    window._bind_window_at(10, 10)
                self.assertEqual(backend.bound_window, 0x1234)
                self.assertIn("记事本", window._bind_label.text())

                # start 之前会把选中的后端交给引擎
                with mock.patch.object(window._run, "set_backend") as set_backend, mock.patch.object(
                    window._run, "start"
                ):
                    window._minimize_check.setChecked(False)
                    window.start_clicking()
                set_backend.assert_called_once()
                self.assertIs(set_backend.call_args.args[0], backend)

                # 切到 UIA：另一个后端，并把前台后端作为「回落」目标
                select(window, MODE_UIA)
                uia = window._active_backend()
                self.assertIsNot(uia, backend)
                self.assertEqual(uia.name, "uia")
                self.assertIs(uia._fallback, foreground)
                self.assertTrue(window._bind_button.isEnabled())
                self.assertIn("未绑定", window._bind_label.text())
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_main_window_assembly(self):
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp))
            hotkeys = HotkeyManager(self.app)
            window = MainWindow(SendInputBackend(), hotkeys, store=store)
            try:
                window.show()
                self.app.processEvents()

                # 首次启动应落盘默认方案
                self.assertTrue(store.list_names())
                self.assertTrue(window._profiles.current_name)

                # 方案能被收集并校验通过
                profile = window._profiles.collect()
                profile.validate()

                # 拾取遮罩几何应覆盖整个虚拟桌面
                window._picker._picker._apply_geometry()
                self.assertGreater(window._picker._picker.width(), 0)

                # 未保存标记
                window._profiles.mark_dirty()
                self.assertIn("*", window.windowTitle())
            finally:
                window._shutdown()
                self.app.processEvents()

            # 退出时应自动保存，不留下未落盘内容
            self.assertTrue(store.list_names())

    # ---------------------------------------------------------------- 拆分出来的子系统
    def test_tray_menu_forwards_signals(self):
        """托盘菜单项 → 信号。``QAction.triggered`` 会捎带 checked 参数，必须被丢掉。"""
        from unittest import mock

        from PySide6.QtWidgets import QSystemTrayIcon, QWidget

        from app.ui.tray_icon import TrayIcon

        parent = QWidget()
        with mock.patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            tray = TrayIcon(parent)
        try:
            self.assertTrue(tray.available)

            fired: list[str] = []
            tray.restore_requested.connect(lambda: fired.append("restore"))
            tray.start_requested.connect(lambda: fired.append("start"))
            tray.stop_requested.connect(lambda: fired.append("stop"))
            tray.pick_requested.connect(lambda: fired.append("pick"))
            tray.record_toggle_requested.connect(lambda: fired.append("record"))
            tray.quit_requested.connect(lambda: fired.append("quit"))

            actions = tray.actions()
            self.assertEqual(len(actions), 6, "六个动作，分隔线应被跳过")
            for action in actions:
                action.trigger()
            self.assertEqual(
                fired, ["restore", "start", "stop", "pick", "record", "quit"]
            )

            tray.notify_once("dup", "只弹一次")
            tray.notify_once("dup", "不该再弹")
            self.assertEqual(tray._notified, {"dup"})
        finally:
            tray.hide()
            parent.deleteLater()

    def test_tray_degrades_without_system_tray(self):
        """没有托盘时（远程桌面等）整个对象退化成空壳，调用方不用判空。"""
        from unittest import mock

        from PySide6.QtWidgets import QSystemTrayIcon, QWidget

        from app.ui.tray_icon import TrayIcon

        parent = QWidget()
        with mock.patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            tray = TrayIcon(parent)
        try:
            self.assertFalse(tray.available)
            self.assertEqual(tray.actions(), [])
            tray.notify("没有托盘时应当静默")
            tray.notify_once("k", "同上")
            tray.hide()
        finally:
            parent.deleteLater()

    def test_profile_controller_recording_landing(self):
        """录制落地：replace 连运行参数一起换；append 只追加并保留用户调好的轮数。"""
        from app.input.sendinput import SendInputBackend
        from app.storage.config import ProfileStore
        from app.ui.main_window import MainWindow
        from app.win.hotkey import HotkeyManager

        with tempfile.TemporaryDirectory() as tmp:
            window = MainWindow(
                SendInputBackend(), HotkeyManager(self.app), store=ProfileStore(Path(tmp))
            )
            try:
                recorded = Profile(
                    name="录制方案",
                    steps=[ClickStep(x=5, y=6), ClickStep(x=7, y=8)],
                    repeat=1,
                    repeat_delay_ms=0,
                    start_delay_ms=500,
                )

                window._repeat_spin.setValue(3)
                window._profiles.set_dirty(False)
                window._profiles.apply_recording(recorded, "replace")
                self.assertEqual(len(window._editor.steps()), 2)
                self.assertEqual(window._repeat_spin.value(), 1, "替换模式应连运行参数一起换")
                self.assertTrue(window._profiles.dirty, "录制落地后必然有未保存改动")

                window._repeat_spin.setValue(3)
                window._profiles.apply_recording(recorded, "append")
                self.assertEqual(len(window._editor.steps()), 4)
                self.assertEqual(window._repeat_spin.value(), 3, "追加模式不该动用户调好的轮数")
                self.assertEqual(window._editor.current_row(), 2, "光标该停在新增的第一行")
            finally:
                window._shutdown()
                self.app.processEvents()

    def test_log_panel_pairs_handler_with_lifetime(self):
        """handler 必须成对挂/摘，否则窗口销毁后 logging 会写进已释放的控件。"""
        import logging as _logging

        from app.ui.log_panel import LogPanel

        panel = LogPanel()
        root = _logging.getLogger()
        before = len(root.handlers)
        lines: list[str] = []
        try:
            panel.attach(lines.append)
            panel.attach(lines.append)  # 重复挂载应被忽略
            self.assertEqual(len(root.handlers), before + 1)

            root.warning("hello")
            self.assertTrue(any("hello" in line for line in lines))

            panel.detach()
            panel.detach()  # 重复摘除也应安全
            self.assertEqual(len(root.handlers), before)

            lines.clear()
            root.warning("after")
            self.assertEqual(lines, [], "摘掉之后不该再收到日志")
        finally:
            panel.detach()
            panel.deleteLater()

    # ---------------------------------------------------------------- 找图
    def _pump(self, predicate, timeout=2.0):
        """推动事件循环直到条件成立（计时器、队列信号都需要它）。"""
        deadline = time.perf_counter() + timeout
        while not predicate() and time.perf_counter() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        return predicate()

    def test_step_editor_adds_find_image_step(self):
        from unittest import mock

        from app.ui.position_picker import PickCoordinator
        from app.ui.step_editor import StepEditor

        editor = StepEditor(PickCoordinator())
        try:
            editor.set_steps([])
            with mock.patch.object(StepEditor, "_open_dialog", lambda self, step: step):
                editor.add_step(StepKind.WAIT)

            self.assertEqual(len(editor.steps()), 1)
            self.assertIsInstance(editor.steps()[0], WaitStep)
            self.assertEqual(editor._table.item(0, 1).text(), "找图")
            self.assertIn("未截取", editor._table.item(0, 2).text())
        finally:
            editor.deleteLater()

    def test_find_image_dialog_shows_preview_and_roundtrips(self):
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        encoded = _button_template().encode()
        dialog = StepDialog(WaitStep(template=encoded, timeout_ms=15000), PickCoordinator())
        try:
            self.assertIn("40 × 20", dialog._template_hint.text())
            self.assertIn("纯色", dialog._template_hint.text())
            self.assertFalse(dialog._template_label.pixmap().isNull())

            step = dialog.result_step()
            self.assertIsInstance(step, WaitStep)
            self.assertEqual(step.template, encoded)
            self.assertEqual(step.timeout_ms, 15000)
            step.validate()
        finally:
            dialog.deleteLater()

    def test_find_image_dialog_warns_without_solid_area(self):
        """模板里没有纯色横线时要提前告警，别让用户事后猜为什么找不到。"""
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        dialog = StepDialog(
            WaitStep(template=_gradient_template().encode()), PickCoordinator()
        )
        try:
            self.assertIn("没有连续", dialog._template_hint.text())
        finally:
            dialog.deleteLater()

    def test_find_image_dialog_refuses_to_save_without_template(self):
        from unittest import mock

        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        dialog = StepDialog(WaitStep(), PickCoordinator())
        try:
            with mock.patch("app.ui.step_dialog.QMessageBox") as box:
                dialog.accept()
            self.assertTrue(box.warning.called)
            self.assertIn("截取", box.warning.call_args[0][2])
            self.assertEqual(dialog.result(), 0, "参数不合法时对话框不该关闭")
        finally:
            dialog.deleteLater()

    def test_find_image_dialog_pick_uses_template_overlay(self):
        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        picker = PickCoordinator()
        dialog = StepDialog(WaitStep(), picker)
        try:
            dialog.show()
            dialog.pick_template()

            self.assertFalse(dialog.isVisible(), "拖框时对话框必须让开")
            self.assertTrue(picker.busy)

            picker.cancel()  # 等价于用户按 Esc
            self.assertTrue(dialog.isVisible(), "取消后应恢复对话框")
            self.assertFalse(picker.busy)
        finally:
            dialog.deleteLater()

    def test_find_image_dialog_test_match_reports_hit(self):
        from unittest import mock

        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        dialog = StepDialog(
            WaitStep(template=_button_template().encode()), PickCoordinator()
        )

        def fake_capture(picker, callback, hide_widget=None):
            callback(_screen_with_button(origin=(-1920, -200)))
            return True

        try:
            with mock.patch.object(
                PickCoordinator, "capture_screen_after_hide", fake_capture
            ), mock.patch("app.ui.step_dialog.QMessageBox") as box:
                dialog.test_match()

            self.assertTrue(box.information.called)
            message = box.information.call_args[0][2]
            self.assertIn("找到了", message)
            self.assertIn("(-1850, -150)", message, "命中的应是屏幕绝对坐标")
        finally:
            dialog.deleteLater()

    def test_find_image_dialog_test_match_explains_a_miss(self):
        from unittest import mock

        from app.ui.position_picker import PickCoordinator
        from app.ui.step_dialog import StepDialog

        dialog = StepDialog(
            WaitStep(template=_button_template().encode()), PickCoordinator()
        )

        def fake_capture(picker, callback, hide_widget=None):
            callback(_frame(200, 120))
            return True

        try:
            with mock.patch.object(
                PickCoordinator, "capture_screen_after_hide", fake_capture
            ), mock.patch("app.ui.step_dialog.QMessageBox") as box:
                dialog.test_match()

            self.assertTrue(box.warning.called)
            message = box.warning.call_args[0][2]
            self.assertIn("没找到", message)
            self.assertIn("颜色", message, "候选数为 0 时要区分「颜色都没对上」这种情况")
        finally:
            dialog.deleteLater()

    def test_pick_coordinator_extracts_template(self):
        """松手后要隐藏遮罩、等合成器重绘、抓图、再把请求方窗口恢复。"""
        from unittest import mock

        from app.ui import position_picker
        from app.ui.position_picker import PickCoordinator
        from app.win.geometry import ScreenRect
        from PySide6.QtWidgets import QWidget

        picker = PickCoordinator()
        widget = QWidget()
        received: list = []

        def fake_capture(region=None):
            frame = _screen_with_button()
            return _crop_frame(frame, region.x, region.y, region.width, region.height)

        try:
            with mock.patch.object(
                position_picker, "capture_screen", fake_capture
            ), mock.patch.object(position_picker, "HIDE_SETTLE_MS", 0):
                self.assertTrue(picker.request_template(received.append, hide_widget=widget))
                self.assertTrue(picker.busy, "截取还没完成时也应算「忙」")

                picker._template_picker.selected.emit(ScreenRect(*BUTTON_AT, *BUTTON_SIZE))
                self.assertTrue(self._pump(lambda: bool(received)))

            self.assertEqual((received[0].width, received[0].height), BUTTON_SIZE)
            self.assertEqual(received[0].data, _button_template().data)
            self.assertFalse(picker.busy)
            self.assertTrue(widget.isVisible(), "截完后请求方窗口要恢复")
        finally:
            widget.deleteLater()
            picker.deleteLater()

    def test_pick_coordinator_reports_capture_failure(self):
        from unittest import mock

        from app.ui import position_picker
        from app.ui.position_picker import PickCoordinator
        from app.win.geometry import ScreenRect

        def broken(region=None):
            raise OSError("被系统拒绝")

        picker = PickCoordinator()
        failures: list[str] = []
        canceled: list[int] = []
        picker.failed.connect(failures.append)
        try:
            with mock.patch.object(position_picker, "capture_screen", broken), mock.patch.object(
                position_picker, "HIDE_SETTLE_MS", 0
            ):
                picker.request_template(lambda template: None, lambda: canceled.append(1))
                picker._template_picker.selected.emit(ScreenRect(0, 0, 30, 30))
                self.assertTrue(self._pump(lambda: bool(failures)))

            self.assertIn("截取模板失败", failures[0])
            self.assertEqual(canceled, [1], "失败也应该让调用方收尾")
            self.assertFalse(picker.busy)
        finally:
            picker.deleteLater()

    def test_pick_coordinator_rejects_second_flow(self):
        from app.ui.position_picker import PickCoordinator

        picker = PickCoordinator()
        try:
            self.assertTrue(picker.request(lambda x, y: None))
            self.assertFalse(
                picker.request_template(lambda template: None), "取点期间不该再开拖框"
            )
            picker.cancel()
            self.assertFalse(picker.busy)
            self.assertTrue(picker.request_template(lambda template: None))
            picker.cancel()
        finally:
            picker.deleteLater()

    # ---------------------------------------------------------------- 快照
    def test_save_frame_writes_png_with_correct_channels(self):
        """快照的通道顺序必须是 RGB 而不是 BGR —— 画面偏蓝这种事光看大小发现不了。"""
        from unittest import mock

        from PySide6.QtGui import QImage

        from app.ui import shot

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(shot, "app_data_dir", lambda: Path(tmp)):
                path = shot.save_frame(_screen_with_button())

            self.assertIsNotNone(path)
            self.assertEqual(path.suffix, ".png")
            self.assertTrue(path.is_file())

            image = QImage(str(path))
            self.assertEqual((image.width(), image.height()), (200, 120))
            self.assertEqual(
                image.pixelColor(BUTTON_AT[0] + 2, BUTTON_AT[1] + 2).getRgb()[:3],
                (0, 120, 212),
                "BGRA 应按 RGB32 解读，而不是把蓝红调个个儿",
            )

    def test_save_frame_prunes_old_shots(self):
        from unittest import mock

        from app.ui import shot

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(shot, "app_data_dir", lambda: Path(tmp)):
                for index in range(shot.SHOT_LIMIT + 3):
                    self.assertIsNotNone(shot.save_frame(_frame(8, 8 + index)))
                shots = list((Path(tmp) / "shots").glob("*.png"))

            self.assertLessEqual(len(shots), shot.SHOT_LIMIT)
            self.assertGreater(len(shots), 0)

    def test_save_frame_never_raises(self):
        """落盘失败（磁盘满/权限）绝不能连带把宏打断。"""
        from unittest import mock

        from app.ui import shot

        with mock.patch.object(shot, "app_data_dir", side_effect=OSError("磁盘满了")):
            self.assertIsNone(shot.save_frame(_frame(4, 4)))


# ==================================================================== 后台点击
class TestPostMessageBackend(unittest.TestCase):
    """PostMessage 后端的消息构造与目标解析。

    不真的往别的窗口投消息：把 ``post_message`` 和窗口查询函数全部换成假的，
    只校验「发给了谁、发了什么消息、坐标换算对不对」。
    """

    def test_make_lparam_packs_client_coords(self):
        from app.input.postmessage import _make_lparam

        self.assertEqual(_make_lparam(0x0102, 0x0304), 0x03040102, "低 16 位 x、高 16 位 y")
        # 点在客户区外时坐标为负，按 16 位截断即可，不能抛
        self.assertEqual(_make_lparam(-1, -1), 0xFFFFFFFF)

    def test_key_lparam_marks_keyup(self):
        from app.input.postmessage import _key_lparam

        down = _key_lparam(0x1E, up=False)
        up = _key_lparam(0x1E, up=True)
        self.assertEqual(down & 0xFFFF, 1, "重复次数应为 1")
        self.assertEqual((down >> 16) & 0xFF, 0x1E, "lParam 里要带扫描码")
        self.assertEqual(up - down, (1 << 30) | (1 << 31), "抬起要置 bit30/31")

    def test_click_posts_move_down_up_in_client_coords(self):
        from unittest import mock

        from app.input import postmessage as pm

        sent = []
        with mock.patch.multiple(
            pm,
            is_window=lambda hwnd: True,
            root_window=lambda hwnd: 0x1234,
            describe=lambda hwnd: "目标 [TestClass]",
            deepest_child_at=lambda base, x, y: 0x5678,
            screen_to_client=lambda hwnd, x, y: (10, 20),
            post_message=lambda hwnd, msg, wp=0, lp=0: sent.append((hwnd, msg, wp, lp)) or True,
        ):
            backend = pm.PostMessageBackend()
            self.assertTrue(backend.bind_window(0x1234))
            backend.click(500, 400, MouseButton.LEFT)

        inner = pm._make_lparam(10, 20)
        self.assertEqual(
            sent,
            [
                (0x5678, pm.WM_MOUSEMOVE, 0, inner),
                (0x5678, pm.WM_LBUTTONDOWN, pm.MK_LBUTTON, inner),
                (0x5678, pm.WM_LBUTTONUP, 0, inner),
            ],
            "应先 WM_MOUSEMOVE 再按下/抬起，并投递给该点下最内层的子窗口",
        )

    def test_click_uses_button_specific_messages(self):
        from unittest import mock

        from app.input import postmessage as pm

        sent = []
        with mock.patch.multiple(
            pm,
            is_window=lambda hwnd: True,
            root_window=lambda hwnd: 0x10,
            describe=lambda hwnd: "w",
            deepest_child_at=lambda base, x, y: base,
            screen_to_client=lambda hwnd, x, y: (0, 0),
            post_message=lambda hwnd, msg, wp=0, lp=0: sent.append((msg, wp)) or True,
        ):
            backend = pm.PostMessageBackend()
            backend.bind_window(0x10)
            backend.click(1, 1, MouseButton.RIGHT)

        self.assertEqual(
            sent[1], (pm.WM_RBUTTONDOWN, pm.MK_RBUTTON)
        )
        self.assertEqual(sent[2], (pm.WM_RBUTTONUP, 0))

    def test_move_to_posts_only_mousemove(self):
        from unittest import mock

        from app.input import postmessage as pm

        sent = []
        with mock.patch.multiple(
            pm,
            is_window=lambda hwnd: True,
            root_window=lambda hwnd: 0x10,
            describe=lambda hwnd: "w",
            deepest_child_at=lambda base, x, y: base,
            screen_to_client=lambda hwnd, x, y: (7, 8),
            post_message=lambda hwnd, msg, wp=0, lp=0: sent.append((hwnd, msg, wp, lp)) or True,
        ):
            backend = pm.PostMessageBackend()
            backend.bind_window(0x10)
            backend.move_to(100, 200)

        self.assertEqual(sent, [(0x10, pm.WM_MOUSEMOVE, 0, pm._make_lparam(7, 8))])

    def test_press_key_posts_keydown_keyup_with_scancode(self):
        from unittest import mock

        from app.input import postmessage as pm

        sent = []
        with mock.patch.multiple(
            pm,
            is_window=lambda hwnd: True,
            root_window=lambda hwnd: 0x10,
            describe=lambda hwnd: "w",
            map_virtual_key=lambda vk: 0x1E,
            foreground_window=lambda: None,
            post_message=lambda hwnd, msg, wp=0, lp=0: sent.append((hwnd, msg, wp, lp)) or True,
        ):
            backend = pm.PostMessageBackend()
            backend.bind_window(0x10)
            backend.press_key(0x41)

        self.assertEqual(
            sent,
            [
                (0x10, pm.WM_KEYDOWN, 0x41, pm._key_lparam(0x1E, up=False)),
                (0x10, pm.WM_KEYUP, 0x41, pm._key_lparam(0x1E, up=True)),
            ],
        )

    def test_bind_window_rejects_dead_handle(self):
        from unittest import mock

        from app.input import postmessage as pm

        with mock.patch.multiple(pm, is_window=lambda hwnd: False):
            backend = pm.PostMessageBackend()
            self.assertFalse(backend.bind_window(0))
            self.assertFalse(backend.bind_window(0xDEAD))
            self.assertIsNone(backend.bound_window)
            self.assertEqual(backend.bound_title, "")

    def test_bind_window_normalizes_to_root(self):
        from unittest import mock

        from app.input import postmessage as pm

        with mock.patch.multiple(
            pm, is_window=lambda hwnd: True, root_window=lambda hwnd: 0x2200, describe=lambda hwnd: "win"
        ):
            backend = pm.PostMessageBackend()
            self.assertTrue(backend.bind_window(0x22FF))
            self.assertEqual(backend.bound_window, 0x2200, "应归一化到顶层窗口")
            self.assertEqual(backend.bound_title, "win")

    def test_bind_point_uses_window_under_cursor(self):
        from unittest import mock

        from app.input import postmessage as pm

        with mock.patch.multiple(
            pm,
            window_from_point=lambda x, y: 0x2222,
            is_window=lambda hwnd: True,
            root_window=lambda hwnd: 0x2200,
            describe=lambda hwnd: "win",
        ):
            backend = pm.PostMessageBackend()
            self.assertTrue(backend.bind_point(10, 10))
            self.assertEqual(backend.bound_window, 0x2200)

    def test_unbound_click_falls_back_to_window_at_point(self):
        """没绑定窗口时应退化为「按坐标找当时最顶层的窗口」，而不是直接失灵。"""
        from unittest import mock

        from app.input import postmessage as pm

        sent = []
        with mock.patch.multiple(
            pm,
            window_from_point=lambda x, y: 0x9999,
            deepest_child_at=lambda base, x, y: base,
            screen_to_client=lambda hwnd, x, y: (1, 2),
            post_message=lambda hwnd, msg, wp=0, lp=0: sent.append((hwnd, msg)) or True,
        ):
            backend = pm.PostMessageBackend()
            backend.click(3, 4)

        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0][0], 0x9999)

    def test_no_target_warns_once_and_posts_nothing(self):
        from unittest import mock

        from app.input import postmessage as pm

        sent = []
        with mock.patch.multiple(
            pm,
            window_from_point=lambda x, y: None,
            post_message=lambda *args: sent.append(args) or True,
        ):
            backend = pm.PostMessageBackend()
            with self.assertLogs("app.input.postmessage", level="WARNING") as captured:
                backend.click(1, 2)
                backend.click(3, 4)

        self.assertEqual(sent, [], "没有目标窗口时不应投递任何消息")
        self.assertEqual(len(captured.records), 1, "同一个原因只应提醒一次，不刷屏")

    def test_unbind_clears_target(self):
        from unittest import mock

        from app.input import postmessage as pm

        with mock.patch.multiple(
            pm, is_window=lambda hwnd: True, root_window=lambda hwnd: 0x10, describe=lambda hwnd: "w"
        ):
            backend = pm.PostMessageBackend()
            backend.bind_window(0x10)
            backend.unbind()

        self.assertIsNone(backend.bound_window)
        self.assertEqual(backend.bound_title, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
