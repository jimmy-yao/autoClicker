"""探针判定逻辑的测试：``tools/probe_background.py`` 的「自动判断」部分。

为什么单独一个文件
------------------
``probe_background.py`` 里真正会坑人的不是 Win32 调用，而是**判定规则**：
把 JCEF 的按钮判成「能后台点」，用户就会拿着一份跑不通的方案来回折腾。
所以把规则抽成了纯函数（不碰 Win32、不碰 Qt），在这里逐条锁住。

这也是这个文件能同时跑在**任何平台**上的原因：``probe_background.py`` 对
``ctypes.WinDLL`` / ``ctypes.WINFUNCTYPE`` 做了保护（这两个在非 Windows 上根本不存在），
于是判定表、区域夹取、模板质检、配置生成都能在没有 Windows 的机器上被验证。
真窗口那一层由 ``tests/test_win_integration.py`` 负责。

运行
----
::

    python tests/test_probe.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _extra in (_ROOT, _ROOT / "tools"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import probe_background as probe  # noqa: E402


def _solid(width: int, height: int, color: bytes = b"\x10\x20\x30\xff") -> bytes:
    return color * (width * height)


def _few_colors(width: int, height: int, count: int = 5) -> bytes:
    palette = [bytes([i * 40 % 256, i * 70 % 256, i * 11 % 256, 255]) for i in range(count)]
    return b"".join(palette[index % count] for index in range(width * height))


def _rich(width: int, height: int) -> bytes:
    """每个像素都不同，用来代表「有内容的截图」。"""
    out = bytearray()
    for index in range(width * height):
        out += bytes([(index * 37) % 256, (index * 91) % 256, (index * 13) % 256, 255])
    return bytes(out)


def _real_template(width: int = 8, height: int = 6):
    """真造一个 ``Template`` 并 ``encode()``，用于配置闭环测试。"""
    from app.core.matching import Template

    return Template(width=width, height=height, data=_rich(width, height)).encode()


class TestFrameworkTable(unittest.TestCase):
    """类名 → 框架 → 认不认 ``WM_*``。"""

    def test_chromium_classes_are_never_postmessage(self):
        for name in (
            "Chrome_RenderWidgetHostHWND",
            "Chrome_WidgetWin_0",
            "Chrome_WidgetWin_1",
            "CefBrowserWindow",
            "CefRenderWidgetHostWindow",
        ):
            with self.subTest(cls=name):
                found = probe.framework_of_class(name)
                self.assertIsNotNone(found)
                self.assertEqual(found.name, probe._CHROMIUM_FRAMEWORK)
                self.assertFalse(found.postmessage, "网页控件不该被判成能吃 WM_*")

    def test_known_friendly_frameworks_accept_postmessage(self):
        for name in (
            "SunAwtCanvas",
            "SunAwtFrame",
            "SunAwtDialog",
            "Qt5152QWindowIcon",
            "Qt5QWindowToolSaveBits",
            "HwndWrapper[App;;guid]",
            "WindowsForms10.Window.8.app.0.abc",
            "Button",
            "#32770",
            "msctls_progress32",
        ):
            with self.subTest(cls=name):
                found = probe.framework_of_class(name)
                self.assertIsNotNone(found, f"{name} 应该被认出来")
                self.assertTrue(found.postmessage, f"{name} 属于通常认 WM_* 的一类")

    def test_frameworks_that_ignore_postmessage(self):
        for name in ("ApplicationFrameWindow", "WinUIDesktopWin32WindowClass"):
            with self.subTest(cls=name):
                found = probe.framework_of_class(name)
                self.assertIsNotNone(found)
                self.assertFalse(found.postmessage)

    def test_unknown_class_is_not_guessed(self):
        self.assertIsNone(probe.framework_of_class("TdxWClass"))
        self.assertIsNone(probe.framework_of_class(""))

    def test_chromium_wins_over_the_wrapper_framework(self):
        """IDEA 顶层是 Swing 帧，里面嵌了 JCEF —— 按钮本身还是网页画的。"""
        found = probe.identify_framework(
            ["Chrome_RenderWidgetHostHWND", "Chrome_WidgetWin_0", "SunAwtFrame"]
        )
        self.assertEqual(found.name, probe._CHROMIUM_FRAMEWORK)
        self.assertFalse(found.postmessage)

    def test_pure_swing_chain_is_classified_as_swing(self):
        found = probe.identify_framework(["SunAwtCanvas", "SunAwtFrame"])
        self.assertEqual(found.name, "Java Swing / AWT")
        self.assertTrue(found.postmessage)

    def test_unrecognised_chain_yields_nothing(self):
        self.assertIsNone(probe.identify_framework(["TdxWClass", "TdxWClass"]))
        self.assertIsNone(probe.identify_framework([]))

    def test_evidence_is_the_matching_class_name(self):
        found = probe.framework_of_class("Qt5152QWindowIcon")
        self.assertEqual(found.evidence, "Qt5152QWindowIcon")


class TestDecide(unittest.TestCase):
    """四路证据 → 结论。这是整份工具的「大脑」。"""

    swing = probe.Framework("Java Swing / AWT", True, "SunAwtCanvas")
    jcef = probe.Framework(probe._CHROMIUM_FRAMEWORK, False, "Chrome_RenderWidgetHostHWND")
    uwp = probe.Framework("UWP / XAML", False, "ApplicationFrameWindow")

    def _decide(self, framework=None, in_chain=False, under_top=False, effect=None):
        return probe.decide(
            framework=framework,
            chromium_in_chain=in_chain,
            chromium_under_top=under_top,
            click_effect=effect,
        )

    def test_chromium_in_chain_beats_everything(self):
        """哪怕实测「有变化」也不改判：网页里的副作用同样会让画面变。"""
        for effect in (None, True, False):
            with self.subTest(effect=effect):
                decision = self._decide(framework=self.swing, in_chain=True, effect=effect)
                self.assertEqual(decision.route, probe.ROUTE_UIA)
                self.assertEqual(decision.confidence, probe.CONF_HIGH)
                self.assertTrue(decision.blockers)

    def test_chromium_result_warns_against_trusting_the_click(self):
        decision = self._decide(framework=self.jcef, in_chain=True, effect=True)
        self.assertTrue(
            any("别据此认为" in reason for reason in decision.reasons),
            f"实测有变化时必须提醒用户别当成按钮被按下：{decision.reasons}",
        )

    def test_framework_that_ignores_postmessage_goes_to_uia(self):
        decision = self._decide(framework=self.uwp)
        self.assertEqual(decision.route, probe.ROUTE_UIA)
        self.assertEqual(decision.confidence, probe.CONF_HIGH)

    def test_friendly_framework_confirmed_by_measurement(self):
        decision = self._decide(framework=self.swing, effect=True)
        self.assertEqual(decision.route, probe.ROUTE_POSTMESSAGE)
        self.assertEqual(decision.confidence, probe.CONF_HIGH)

    def test_friendly_framework_without_measurement_is_medium_confidence(self):
        decision = self._decide(framework=self.swing, effect=None)
        self.assertEqual(decision.route, probe.ROUTE_POSTMESSAGE)
        self.assertEqual(decision.confidence, probe.CONF_MEDIUM)

    def test_friendly_framework_that_did_not_react_is_downgraded(self):
        decision = self._decide(framework=self.swing, effect=False)
        self.assertEqual(decision.route, probe.ROUTE_UIA)
        self.assertEqual(decision.confidence, probe.CONF_MEDIUM)

    def test_unknown_framework_falls_back_to_measurement(self):
        self.assertEqual(
            self._decide(effect=True).route, probe.ROUTE_POSTMESSAGE
        )
        self.assertEqual(self._decide(effect=False).route, probe.ROUTE_UIA)

    def test_unknown_and_unmeasured_falls_back_to_foreground(self):
        """判不出来时不能瞎推荐：前台一定有效，是唯一安全的默认。"""
        decision = self._decide()
        self.assertEqual(decision.route, probe.ROUTE_FOREGROUND)
        self.assertEqual(decision.confidence, probe.CONF_LOW)

    def test_embedded_web_view_is_reported_even_when_not_on_the_chain(self):
        decision = self._decide(under_top=True)
        self.assertTrue(
            any("内嵌了网页" in reason for reason in decision.reasons),
            "顶层窗口下有 Chromium 子窗口时应当提示（但不足以改判）",
        )

    def test_every_decision_explains_itself(self):
        cases = [
            dict(framework=self.swing, effect=True),
            dict(framework=self.swing, effect=False),
            dict(framework=self.swing),
            dict(framework=self.uwp),
            dict(in_chain=True),
            dict(effect=True),
            dict(),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertTrue(self._decide(**case).reasons, "结论必须给出理由")

    def test_all_routes_have_a_label(self):
        for route in (probe.ROUTE_POSTMESSAGE, probe.ROUTE_UIA, probe.ROUTE_FOREGROUND):
            self.assertIn(route, probe.ROUTE_LABELS)


class TestClampRegion(unittest.TestCase):
    """截图/比对的区域必须整块落在屏幕里。"""

    screen = (0, 0, 1920, 1080)

    def test_centred_region(self):
        self.assertEqual(probe.clamp_region(960, 540, 96, 36, self.screen), (912, 522, 96, 36))

    def test_clamped_at_top_left_corner(self):
        self.assertEqual(probe.clamp_region(2, 2, 96, 36, self.screen), (0, 0, 96, 36))

    def test_clamped_at_bottom_right_corner(self):
        left, top, width, height = probe.clamp_region(1919, 1079, 96, 36, self.screen)
        self.assertEqual((left, top, width, height), (1920 - 96, 1080 - 36, 96, 36))

    def test_negative_origin_screen(self):
        """多显示器在左侧/上方时虚拟桌面原点是负数。"""
        screen = (-1920, -200, 1920, 1080)
        left, top, _, _ = probe.clamp_region(-1900, -180, 96, 36, screen)
        # 竖直方向离屏幕顶还有 18px，36 高的框居中后顶边正好落在 -198，本来就不用夹
        self.assertEqual((left, top), (-1920, -198))

    def test_never_leaves_the_screen_at_the_top_left(self):
        screen = (-1920, -200, 1920, 1080)
        left, top, _, _ = probe.clamp_region(-1919, -199, 96, 36, screen)
        self.assertEqual((left, top), (-1920, -200))

    def test_region_larger_than_screen_still_returns_a_box(self):
        left, top, width, height = probe.clamp_region(10, 10, 4000, 4000, self.screen)
        self.assertEqual((width, height), (4000, 4000))
        self.assertGreaterEqual(left, self.screen[0])
        self.assertGreaterEqual(top, self.screen[1])


class TestTemplateQuality(unittest.TestCase):
    """模板太「平」的话，找图会满屏误命中 —— 截完就检查。"""

    def test_solid_colour_is_rejected(self):
        grade, message = probe.template_quality(16, 16, _solid(16, 16))
        self.assertEqual(grade, "差")
        self.assertIn("误命中", message)

    def test_two_colours_are_rejected(self):
        data = _solid(16, 16) + b"\xff\xff\xff\xff" * (16 * 16 - 1)
        grade, _ = probe.template_quality(16, 16, data)
        self.assertEqual(grade, "差")

    def test_almost_flat_is_rejected_by_dominance(self):
        """颜色种类虽然够，但 99% 是同一个色 —— 还是不能用。"""
        width = height = 40
        data = bytearray(_solid(width, height))
        for index in range(10):
            data[index * 4 : index * 4 + 4] = bytes([index * 20, 200, index * 9, 255])
        grade, message = probe.template_quality(width, height, bytes(data))
        self.assertEqual(grade, "差")
        self.assertIn("纯色块", message)

    def test_a_few_colours_is_acceptable_but_flagged(self):
        grade, _ = probe.template_quality(16, 16, _few_colors(16, 16, 5))
        self.assertEqual(grade, "中")

    def test_rich_template_is_good(self):
        grade, message = probe.template_quality(24, 16, _rich(24, 16))
        self.assertEqual(grade, "好")
        self.assertIn("种颜色", message)

    def test_size_mismatch_is_reported_not_raised(self):
        grade, message = probe.template_quality(16, 16, b"\x00" * 8)
        self.assertEqual(grade, "差")
        self.assertIn("尺寸", message)


class TestAwayFrom(unittest.TestCase):
    """自动截模板前要把光标挪开，否则截到的是悬停态。"""

    screen = (0, 0, 1920, 1080)

    def test_moves_clear_of_the_button(self):
        region = (912, 522, 96, 36)
        _, away_y = probe._away_from(960, 540, region, self.screen)
        self.assertNotAlmostEqual(away_y, 540, delta=region[3])
        self.assertGreater(abs(away_y - 540), region[3] // 2)

    def test_flips_upward_near_the_bottom_edge(self):
        region = (100, 1040, 96, 36)
        _, away_y = probe._away_from(148, 1058, region, self.screen)
        self.assertLess(away_y, 1058)
        self.assertGreaterEqual(away_y, self.screen[1])

    def test_stays_on_screen(self):
        for x, y in ((1, 1), (1918, 1078), (960, 540)):
            with self.subTest(point=(x, y)):
                away_x, away_y = probe._away_from(x, y, (x, y, 96, 36), self.screen)
                self.assertTrue(0 <= away_x < 1920, (x, y, away_x))
                self.assertTrue(0 <= away_y < 1080, (x, y, away_y))


class TestProfileBuild(unittest.TestCase):
    """打印给用户的方案，必须真的能被程序加载并通过校验。"""

    def test_profile_round_trips_through_json(self):
        template = _real_template()
        profile = probe.build_profile("继续续跑", template)

        text = probe.profile_to_json(profile)
        reloaded = type(profile).from_dict(json.loads(text))

        # validate() 会真的去解模板，能过说明这份配置是「能用的」而不只是「像样的」
        reloaded.validate()
        self.assertEqual(reloaded.name, "继续续跑")
        self.assertEqual(reloaded.steps[0].kind.value, "wait")
        self.assertEqual(reloaded.steps[0].template, template)

    def test_profile_matches_the_continue_scenario(self):
        profile = probe.build_profile("继续续跑", _real_template())
        step = profile.steps[0]
        self.assertEqual(profile.repeat, 0, "无限循环")
        self.assertTrue(profile.infinite)
        self.assertTrue(step.click, "找到就点它")
        self.assertEqual(step.on_timeout, "continue", "没出现『继续』不该中止整个任务")
        self.assertEqual(step.button.value, "left")

    def test_template_survives_encoding_unchanged(self):
        from app.core.matching import Template

        template = probe.build_profile("x", _real_template(12, 9)).steps[0].template
        restored = Template.decode(template)
        self.assertEqual((restored.width, restored.height), (12, 9))

    def test_empty_template_cannot_be_used_yet(self):
        """--no-capture 时模板是空的：应当明确「还不能用」，而不是悄悄发一份废配置。"""
        profile = probe.build_profile("x", "")
        with self.assertRaises(ValueError):
            profile.validate()

    def test_json_is_human_readable_chinese(self):
        text = probe.profile_to_json(probe.build_profile("继续续跑", _real_template()))
        self.assertIn("继续续跑", text)
        self.assertIn("等『继续』出现就点它", text, "中文应以原样出现（ensure_ascii=False）")
        self.assertNotIn("\\u", text, "不该出现 \\uXXXX 转义，否则没法直接读")
        self.assertIn('"kind": "wait"', text)


class TestModuleIsImportableEverywhere(unittest.TestCase):
    """非 Windows 上模块仍须可导入，否则这个文件里的用例一条都跑不了。"""

    def test_import_did_not_explode(self):
        self.assertTrue(hasattr(probe, "decide"))

    def test_win32_handle_is_guarded(self):
        if sys.platform == "win32":  # pragma: no cover - Windows
            self.assertIsNotNone(probe._user32)
        else:  # pragma: no cover - Linux / macOS
            self.assertIsNone(probe._user32)
            self.assertIsNone(probe._EnumProc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
