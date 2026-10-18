"""后台点击自检探针：自动判定目标程序该走哪条路，并给出**可直接用**的方案配置。

为什么需要它
------------
``PostMessage`` 后台点击能不能用，**完全取决于目标程序认不认 ``WM_*`` 消息**：

- 经典 Win32 / Qt / Swing(AWT) / WPF / WinForms 一般认；
- Chromium / Electron / JCEF / UWP 基本不认（它们的输入不走 ``WM_*``）。

「我那个 IDEA 插件的『继续』按钮能不能后台点」这个问题不该靠猜，也不该让你去读
一堆窗口类名。本脚本跑一次，自己给结论：**路线 + 置信度 + 理由 + 可直接照抄的方案**。

三选一的结论
------------
- ``后台点击（PostMessage）``：本项目现有的「后台点击」就能用，不占鼠标。
- ``UIA 无障碍（按控件 Invoke）``：PostMessage 没戏（网页控件），但 UIA 走得通——
  本项目已内置这个后端，界面上的「输入方式」里选它即可。
- ``前台点击（SendInput）``：一定有效，但执行期间占用鼠标、目标区域必须显示在屏幕上。

判定依据（按可信度从高到低）
----------------------------
1. **实测**：真的投一记 ``WM_LBUTTONDOWN/UP``，再比对点击前后那一小块屏幕的像素。
   这是唯一直接的经验证据。为了不被界面自身的动画骗到，点击前先连拍两帧确认该区域稳定。
2. **关键结构**：坐标所在窗口链里有没有 Chromium/CEF 窗口。有 → 这个按钮是网页画的，
   ``WM_*`` 一定会被渲染器丢掉（JCEF/Electron 的固有行为）。这是决定性判据，**比「像素变了」
   还硬**：网页里焦点/悬停的副作用也会让像素变，拿它反推「点到了」会误判。
3. **框架类名**：``SunAwt*`` / ``Qt*`` / ``WindowsForms*`` / ``HwndWrapper*`` 通常认 ``WM_*``；
   ``ApplicationFrameWindow``(UWP) / ``WinUIDesktopWin32WindowClass``(WinUI3) 不认。
4. **UIA**：坐标下的控件（或它的某个祖先）支不支持 ``InvokePattern``——决定「中路」通不通。
   用的是项目自己的 UIA 层（``app/win/uia.py``），和界面里选 UIA 模式时**完全同一条路**。

PostMessage 没测出变化时，脚本会**接着用 UIA 真 Invoke 一记**再比对一次像素——
「后台点了没反应」之后最该问的就是这个问题，答案直接决定要不要换成 UIA 模式。

顺带做的一件事：**自动截模板**。脚本会把光标从按钮上移开、等界面重绘，再截一小块，
把编码后的模板直接填进下面的方案里——所以打印出的配置是**能直接用的**，不用你再截一次图。
（``--no-capture`` 可跳过：那样光标不会被移动，但配置里的模板是空的。）

用法
----
在**项目根目录**运行（Windows + Python 3.10+）::

    python tools\\probe_background.py                     # 5 秒后读光标位置
    python tools\\probe_background.py --delay 8           # 给更长的准备时间
    python tools\\probe_background.py --point 1200 800    # 跳过等待，直接分析该坐标
    python tools\\probe_background.py --no-click          # 只分析，不做实测点击
    python tools\\probe_background.py --no-capture        # 不自动截模板（也不移动光标）
    python tools\\probe_background.py --crop 120 40       # 指定模板截取尺寸，默认 96x36
    python tools\\probe_background.py --save 继续续跑      # 把方案直接写进 profiles\\

只依赖标准库 + 本项目自己的代码（UIA 那一节也不再需要 ``pip install uiautomation``）。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# 允许直接 `python tools\probe_background.py` 运行（否则 import app.* 会失败）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ============================================================== 路线与置信度
ROUTE_POSTMESSAGE = "postmessage"
ROUTE_UIA = "uia"
ROUTE_FOREGROUND = "foreground"

ROUTE_LABELS = {
    ROUTE_POSTMESSAGE: "后台点击（PostMessage）—— 不占用鼠标",
    ROUTE_UIA: "UIA 无障碍（按控件 Invoke）—— 不移动鼠标、不抢焦点",
    ROUTE_FOREGROUND: "前台点击（SendInput）—— 会占用鼠标",
}

CONF_HIGH = "高"
CONF_MEDIUM = "中"
CONF_LOW = "低"

GA_PARENT = 1

# ============================================================== 框架判定表
# (类名前缀, 框架名, 这类界面认不认 WM_*)。
# 顺序有意义：先匹配到的先用，所以更具体的类名要排在更泛的前面。
_CHROMIUM_PREFIXES = ("Chrome_", "Cef")

_FRAMEWORK_RULES: tuple[tuple[str, str, bool], ...] = (
    ("SunAwt", "Java Swing / AWT", True),
    ("Qt", "Qt", True),
    ("HwndWrapper", "WPF", True),
    ("WindowsForms", ".NET WinForms", True),
    ("ApplicationFrameWindow", "UWP / XAML", False),
    ("WinUIDesktopWin32WindowClass", "WinUI 3", False),
    ("msctls_", "经典 Win32 控件", True),
    ("SysListView32", "经典 Win32 控件", True),
    ("SysTreeView32", "经典 Win32 控件", True),
    ("SysTabControl32", "经典 Win32 控件", True),
    ("Button", "经典 Win32 按钮控件", True),
    ("Static", "经典 Win32 静态文本", True),
    ("Edit", "经典 Win32 编辑框", True),
    ("ComboBox", "经典 Win32 下拉框", True),
    ("#32770", "经典 Win32 对话框", True),
)

_CHROMIUM_FRAMEWORK = "JCEF / Chromium 网页"


@dataclass(frozen=True)
class Framework:
    """一条框架判定：叫什么、认不认 WM_*、依据哪个类名。"""

    name: str
    postmessage: bool
    evidence: str


def is_chromium_class(class_name: str) -> bool:
    """类名是否是 Chromium/CEF 系的窗口。

    这是整份判定里最硬的一条：命中就意味着「这个按钮是网页画的」，
    ``WM_*`` 会被渲染器丢掉，与框架类别无关。
    """
    return class_name.startswith(_CHROMIUM_PREFIXES)


def framework_of_class(class_name: str) -> Framework | None:
    """单个类名 → 框架判定。逐行标注与整条链的判定共用这一张表。"""
    if is_chromium_class(class_name):
        return Framework(_CHROMIUM_FRAMEWORK, False, class_name)
    for prefix, name, capable in _FRAMEWORK_RULES:
        if class_name.startswith(prefix):
            return Framework(name, capable, class_name)
    return None


def identify_framework(chain_classes: Sequence[str]) -> Framework | None:
    """在整条窗口链里定出框架（``chain_classes`` 由深到浅）。

    Chromium 优先于其它一切：窗口链里只要有它，无论外面套的是 Swing 还是 Qt，
    按钮本身都是网页画的。
    """
    for class_name in chain_classes:
        if is_chromium_class(class_name):
            return Framework(_CHROMIUM_FRAMEWORK, False, class_name)
    for class_name in chain_classes:
        found = framework_of_class(class_name)
        if found is not None:
            return found
    return None


# ============================================================== 结论合议
@dataclass
class Decision:
    """合成后的结论。"""

    route: str
    confidence: str
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


def decide(
    *,
    framework: Framework | None,
    chromium_in_chain: bool,
    chromium_under_top: bool,
    click_effect: bool | None,
    uia_effect: bool | None = None,
) -> Decision:
    """把几路证据合成一个结论。

    ``click_effect``：``True`` 实测后台投递后像素有变化，``False`` 没变化，
    ``None`` 没做实测或那块区域自身不稳（测不准）。

    ``uia_effect``：同上，但测的是 **UIA Invoke**。``None`` 表示没测（后台那一路已经
    生效，或者用户跳过了）。

    判断顺序就是证据可信度从高到低，所以 Chromium 的结构判定能压过「像素变了」：
    网页里焦点/悬停的副作用一样会让画面变，拿它反推「点到了」会误判。

    纯函数，不碰 Win32，便于单测。
    """
    reasons: list[str] = []
    blockers: list[str] = []

    # ---- 1/2. 结构上 PostMessage 就不可能生效
    if chromium_in_chain:
        reasons.append(
            "坐标所在窗口链里有 Chromium/CEF 窗口 → 这个按钮是网页画的，"
            "WM_* 会被渲染器丢掉（JCEF/Electron 的固有行为，与外部框架无关）"
        )
        blockers.append("PostMessage 对网页控件无效")
        if click_effect:
            reasons.append(
                "实测后台投递后像素有变化 —— 别据此认为按钮被按到了，"
                "网页里焦点/悬停的副作用也会让画面变"
            )
        return _recommend_uia(reasons, blockers, uia_effect, chromium_under_top, base=CONF_HIGH)

    if framework is not None and not framework.postmessage:
        reasons.append(f"框架是 {framework.name}（依据类名 {framework.evidence}）→ 这类界面不吃 WM_*")
        blockers.append("PostMessage 对这类框架无效")
        return _recommend_uia(reasons, blockers, uia_effect, chromium_under_top, base=CONF_HIGH)

    if framework is not None:
        reasons.append(f"框架是 {framework.name}（依据类名 {framework.evidence}）→ 这类界面通常认 WM_*")
    else:
        reasons.append("没识别出已知框架类名（只能靠实测）")

    # ---- 3. 实测后台投递
    if click_effect is True:
        reasons.append("实测后台投递后像素有变化 → 结构判定与实测一致，后台点击可用")
        confidence = CONF_HIGH if framework is not None else CONF_MEDIUM
        return _finish(Decision(ROUTE_POSTMESSAGE, confidence, reasons, blockers), chromium_under_top)

    if click_effect is False:
        reasons.append("但实测后台投递后那一小块屏幕没有变化 → 后台这条路实测不通")
        blockers.append("PostMessage 实测没看到任何反应")
        base = CONF_MEDIUM if framework is not None else CONF_LOW
        return _recommend_uia(reasons, blockers, uia_effect, chromium_under_top, base=base)

    # ---- 4. 没做后台实测
    if framework is not None:
        reasons.append("没做后台实测（--no-click），置信度降到中")
        return _finish(Decision(ROUTE_POSTMESSAGE, CONF_MEDIUM, reasons, blockers), chromium_under_top)

    if uia_effect is True:
        reasons.append("没做后台实测，但 UIA Invoke 实测有效 → 直接用 UIA")
        return _recommend_uia(reasons, blockers, uia_effect, chromium_under_top, base=CONF_MEDIUM)

    reasons.append(
        "既没识别出已知框架类名，也没做实测 → 判不出来。"
        "先按前台跑最稳（前台 SendInput 一定会生效），或去掉 --no-click 再跑一次"
    )
    return _finish(Decision(ROUTE_FOREGROUND, CONF_LOW, reasons, blockers), chromium_under_top)


def _recommend_uia(
    reasons: list[str],
    blockers: list[str],
    uia_effect: bool | None,
    chromium_under_top: bool,
    *,
    base: str,
) -> Decision:
    """推荐 UIA 的结论，再按 UIA 的实测结果调整置信度。"""
    if uia_effect is True:
        reasons.append("实测 UIA Invoke 让画面变了 → 这条路确实是通的，可以直接用「UIA 无障碍」模式")
        confidence = CONF_HIGH
    elif uia_effect is False:
        reasons.append(
            "实测 UIA Invoke 也没让画面变 —— 可能是取到的控件不对，"
            "或者 Invoke 生效了但影响落在比对区域之外"
        )
        blockers.append("UIA Invoke 实测没看到反应")
        confidence = CONF_LOW
    else:
        reasons.append(
            "没做 UIA 实测（--no-click 会跳过，也可能当时没找到可 Invoke 的控件）："
            "去掉 --no-click 再跑一次就能确认这条路通不通"
        )
        confidence = base
    return _finish(Decision(ROUTE_UIA, confidence, reasons, blockers), chromium_under_top)


def _finish(decision: Decision, chromium_under_top: bool) -> Decision:
    """补一句「顶层窗口下另有网页」的备注——它不是决定性的，只是补充信息。"""
    if chromium_under_top:
        decision.reasons.append(
            "注：该顶层窗口下另有 Chromium/CEF 子窗口，说明界面里内嵌了网页，"
            "但不在这个按钮的窗口链上"
        )
    return decision


# ============================================================== 纯计算小工具
def clamp_region(
    x: int, y: int, width: int, height: int, screen: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """以 ``(x, y)`` 为中心取一块 ``width × height`` 的区域，并夹进屏幕内。

    ``screen`` 是 ``(left, top, right, bottom)``。整块区域必须落在屏幕里，
    否则 BitBlt 抓到的边缘是黑的，像素比对会假报「有变化」。
    """
    left, top, right, bottom = screen
    origin_x = max(left, min(x - width // 2, right - width))
    origin_y = max(top, min(y - height // 2, bottom - height))
    return origin_x, origin_y, width, height


def template_quality(width: int, height: int, data: bytes) -> tuple[str, str]:
    """粗判模板「够不够有特征」。返回 ``(等级, 说明)``。

    纯色块当模板会在屏幕上到处误命中，而这类问题在真跑起来之前很难发现，
    所以截完就顺手检查一下。抽样统计颜色种类与最多那种颜色的占比。
    """
    if width < 1 or height < 1 or len(data) < width * height * 4:
        return "差", "模板尺寸不对"

    step = max(1, min(width, height) // 24)
    counts: dict[bytes, int] = {}
    total = 0
    for row in range(0, height, step):
        base = row * width * 4
        for col in range(0, width, step):
            offset = base + col * 4
            key = data[offset : offset + 3]
            counts[key] = counts.get(key, 0) + 1
            total += 1

    distinct = len(counts)
    dominant = max(counts.values()) / total if total else 1.0

    if distinct <= 2:
        return "差", f"几乎只有 {distinct} 种颜色，当模板会在屏幕上到处误命中"
    if dominant >= 0.95:
        return "差", f"最多的颜色占了 {dominant:.1%} 的像素，基本是个纯色块"
    if distinct <= 8:
        return "中", f"只有 {distinct} 种颜色，特征偏少（能认，但容差别调太大）"
    return "好", f"{distinct} 种颜色（抽样 {total} 点），最多的占 {dominant:.1%}"


def build_profile(name: str, template: str) -> object:
    """生成「等『继续』出现就点它」的方案对象。

    针对的正是「AI 面板上下文满了会冒出『继续』」这种场景：
    ``repeat=0`` 无限循环，``on_timeout=continue`` 让「没出现」不中止任务，
    等图一轮 30 秒、300 毫秒看一次。

    返回 ``app.core.models.Profile``（延迟 import，让本模块在非 Windows 上也能被导入测试）。
    """
    from app.core.models import ON_TIMEOUT_CONTINUE, Profile, WaitStep

    return Profile(
        name=name,
        repeat=0,
        repeat_delay_ms=300,
        start_delay_ms=3000,
        steps=[
            WaitStep(
                note="等『继续』出现就点它；没出现就继续等",
                template=template,
                timeout_ms=30_000,
                poll_interval_ms=300,
                click=True,
                on_timeout=ON_TIMEOUT_CONTINUE,
            )
        ],
    )


def profile_to_json(profile) -> str:
    """方案的 ``to_dict()`` 结果 → 与磁盘上方案文件同格式的 JSON 文本。"""
    return json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)


# ============================================================== Win32 绑定
# 非 Windows 上 `ctypes.WinDLL` / `ctypes.WINFUNCTYPE` 根本不存在。这里不让 import
# 直接失败，是为了让上面的纯逻辑（判定、区域夹取、模板质检、配置生成）在任何平台上
# 都能被 `tests/test_probe.py` 跑起来——那台机器上跑不了真窗口，至少能锁住判定规则。
try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
except (AttributeError, OSError):  # pragma: no cover - 仅非 Windows
    _user32 = None

_EnumProc = None


def _declare(user32) -> None:
    """声明用到的 user32 函数签名。集中在一处，方便对照 MSDN。"""
    user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = (_EnumProc, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = (wintypes.HWND, _EnumProc, wintypes.LPARAM)
    user32.EnumChildWindows.restype = wintypes.BOOL


if _user32 is not None:  # pragma: no cover - 仅在 Windows 上
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _declare(_user32)


def _hwnd_int(handle) -> int | None:
    """ctypes 句柄 → Python int；空句柄返回 ``None``。

    ``int(c_void_p)`` 在部分平台上解释不通（会去解析内存内容），必须走 ``.value``。
    """
    if not handle:
        return None
    raw = getattr(handle, "value", handle)
    return int(raw) if raw else None


def class_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def title_of(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
    return buf.value


def pid_of(hwnd: int) -> int:
    out = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(out))
    return int(out.value)


def is_visible(hwnd: int) -> bool:
    return bool(_user32.IsWindowVisible(wintypes.HWND(hwnd)))


def cursor_pos() -> tuple[int, int] | None:
    point = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(point)):
        return None
    return int(point.x), int(point.y)


def set_cursor_pos(x: int, y: int) -> bool:
    return bool(_user32.SetCursorPos(int(x), int(y)))


def window_from_point(x: int, y: int) -> int | None:
    return _hwnd_int(_user32.WindowFromPoint(wintypes.POINT(int(x), int(y))))


def ancestors(hwnd: int) -> list[int]:
    """从 ``hwnd`` 一路往上到顶层（含自身）。"""
    chain: list[int] = []
    current: int | None = hwnd
    seen: set[int] = set()
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        parent = _hwnd_int(_user32.GetAncestor(wintypes.HWND(current), GA_PARENT))
        current = parent
    return chain


def _enum(api, parent) -> list[int]:
    found: list[int] = []

    @_EnumProc
    def _cb(hwnd, _lparam):
        value = _hwnd_int(hwnd)
        if value:
            found.append(value)
        return True

    if parent is None:
        api(_cb, 0)
    else:
        api(wintypes.HWND(parent), _cb, 0)
    return found


def top_level_windows() -> list[int]:
    return _enum(_user32.EnumWindows, None)


def child_windows(hwnd: int) -> list[int]:
    return _enum(_user32.EnumChildWindows, hwnd)


def describe_window(hwnd: int) -> str:
    name = class_of(hwnd)
    title = title_of(hwnd)
    shown = f' "{title}"' if title else ""
    return f"0x{hwnd:X}  {name}{shown}  (pid={pid_of(hwnd)}, visible={is_visible(hwnd)})"


# ============================================================== 结果结构
@dataclass
class WindowRow:
    hwnd: int
    class_name: str
    depth: int
    framework: Framework | None


@dataclass
class Analysis:
    """坐标处的结构分析结果。"""

    x: int
    y: int
    found: bool = False
    rows: list[WindowRow] = field(default_factory=list)
    top: int | None = None
    framework: Framework | None = None
    chromium_rows: list[WindowRow] = field(default_factory=list)
    chromium_under_top: list[int] = field(default_factory=list)

    @property
    def chain_classes(self) -> list[str]:
        return [row.class_name for row in self.rows]


@dataclass
class TemplateInfo:
    """自动截下来的模板。"""

    ok: bool = False
    template: str = ""
    width: int = 0
    height: int = 0
    region: tuple[int, int, int, int] | None = None
    moved_cursor: bool = False
    grade: str = ""
    quality: str = ""
    note: str = ""


@dataclass
class UiaProbe:
    """UIA 那一节的观察结果。"""

    available: bool = False
    note: str = ""
    max_hops: int = 0
    point_name: str | None = None
    point_type: str | None = None
    point_class: str | None = None
    point_pid: int = 0
    point_offscreen: bool = False
    point_invoke: bool | None = None
    # 往上找到的那个「点了会有反应」的控件 —— 界面里选 UIA 模式后点的就是它
    target_found: bool = False
    target_desc: str = ""
    target_hops: int = 0

    @property
    def viable(self) -> bool:
        """UIA 这条路是否走得通：坐标下的控件**或它的某个祖先**支持 InvokePattern。"""
        return self.target_found


@dataclass
class ClickEffect:
    """实测点击的结果。"""

    tested: bool = False
    region: tuple[int, int, int, int] | None = None
    stable_before: bool | None = None
    changed: bool | None = None
    target_class: str = ""
    target_desc: str = ""
    client: tuple[int, int] | None = None
    note: str = ""


# ============================================================== 结构分析
def analyze_point(x: int, y: int) -> Analysis:
    """读坐标处的窗口链，判定框架，并找出 Chromium/CEF 的痕迹。"""
    analysis = Analysis(x=x, y=y)
    deepest = window_from_point(x, y)
    if deepest is None:
        return analysis

    analysis.found = True
    for depth, hwnd in enumerate(ancestors(deepest)):
        cls = class_of(hwnd)
        row = WindowRow(hwnd=hwnd, class_name=cls, depth=depth, framework=framework_of_class(cls))
        analysis.rows.append(row)
        if is_chromium_class(cls):
            analysis.chromium_rows.append(row)

    analysis.top = analysis.rows[-1].hwnd
    analysis.framework = identify_framework(analysis.chain_classes)

    # 顶层窗口下另有 Chromium 子窗口 → 界面里内嵌了网页（JCEF 的典型特征），
    # 但只有它真的压在按钮的窗口链上才算数，所以这里只作补充信息。
    analysis.chromium_under_top = [
        hwnd for hwnd in child_windows(analysis.top) if is_chromium_class(class_of(hwnd))
    ]
    return analysis


def report_analysis(analysis: Analysis) -> None:
    _rule(f"① 坐标 ({analysis.x}, {analysis.y}) 处的窗口链")
    if not analysis.found:
        print("  该坐标处没有窗口（可能落在桌面或空白上）。")
        return
    for row in analysis.rows:
        mark = ""
        if row.framework is not None:
            verdict = "认 WM_*" if row.framework.postmessage else "不认 WM_*"
            mark = f"   ← {row.framework.name}（{verdict}）"
        print(f"  第 {row.depth} 层: {describe_window(row.hwnd)}{mark}")

    print()
    print(f"  顶层窗口：{describe_window(analysis.top)}")
    if analysis.chromium_under_top:
        print(f"  ⚠ 顶层窗口下另有 {len(analysis.chromium_under_top)} 个 Chromium/CEF 子窗口"
              "（说明界面内嵌了网页）：")
        for hwnd in analysis.chromium_under_top[:6]:
            print(f"      · {describe_window(hwnd)}")


def report_process_windows(pid: int) -> None:
    """列出目标进程的顶层窗口，标出谁内嵌了网页。"""
    _rule(f"② 目标进程 pid={pid} 的顶层窗口")
    wins = [h for h in top_level_windows() if pid_of(h) == pid]
    if not wins:
        print("  没找到该 pid 的顶层窗口。")
        return
    for hwnd in wins:
        print(f"  {describe_window(hwnd)}")
        chromium = [h for h in child_windows(hwnd) if is_chromium_class(class_of(h))]
        if chromium:
            print(f"      ↳ 内含 {len(chromium)} 个 Chromium/CEF 子窗口（JCEF）")


# ============================================================== 模板截取
def capture_template(x: int, y: int, width: int, height: int) -> TemplateInfo:
    """把光标从按钮上挪开、等界面重绘，再截一块当模板。

    为什么要挪开：鼠标停在按钮上时，按钮多半处于**悬停态**（变色/加下划线）。
    拿悬停态当模板，真跑起来（找图那一刻光标并不在按钮上）就匹配不上。
    """
    from app.core.matching import Template
    from app.win.geometry import ScreenRect, virtual_screen_rect
    from app.win.screen import capture_screen

    info = TemplateInfo()
    screen = virtual_screen_rect()
    bounds = (screen.x, screen.y, screen.right, screen.bottom)
    region = clamp_region(x, y, width, height, bounds)
    info.region = region

    original = cursor_pos()
    away_x, away_y = _away_from(x, y, region, bounds)
    if original is not None and (away_x, away_y) != original:
        if set_cursor_pos(away_x, away_y):
            info.moved_cursor = True
            time.sleep(0.30)  # 等界面把悬停态重绘掉

    try:
        frame = capture_screen(ScreenRect(*region))
    except Exception as exc:  # noqa: BLE001 - 抓屏失败不该中断整份报告
        info.note = f"抓屏失败：{exc!r}"
        return info
    finally:
        if info.moved_cursor and original is not None:
            set_cursor_pos(*original)

    template = Template.from_frame(frame)
    info.ok = True
    info.template = template.encode()
    info.width = template.width
    info.height = template.height
    info.grade, info.quality = template_quality(template.width, template.height, template.data)
    return info


def _away_from(
    x: int, y: int, region: tuple[int, int, int, int], screen: tuple[int, int, int, int]
) -> tuple[int, int]:
    """挑一个「肯定不在这个按钮上」的落点（尽量少移动）。"""
    left, top, right, bottom = screen
    offset = (region[3] // 2) + 80
    away_y = y + offset
    if away_y > bottom - 2:
        away_y = max(top + 1, y - offset)
    return max(left + 1, min(x, right - 2)), max(top + 1, min(away_y, bottom - 2))


def report_template(info: TemplateInfo) -> None:
    _rule("③ 顺手截好的模板（用于方案里的「找图」步骤）")
    if not info.ok:
        print(f"  没能自动截取：{info.note}")
        print("  不影响判定；方案里的模板留空，到界面上用「截取」补一张即可。")
        return
    print(f"  截取区域：{info.region}（{info.width} × {info.height} 像素）")
    if info.moved_cursor:
        print("  已先把光标挪开再截 → 拿到的是**非悬停**状态，正是找图时看到的样子。")
    else:
        print("  注意：没能挪开光标，模板可能是**悬停态**；若跑起来匹配不上，重截一张。")
    print(f"  模板质量：{info.grade} —— {info.quality}")
    print(f"  编码后长度：{len(info.template)} 字符（已填进下面的方案里）")


# ============================================================== UIA
def probe_uia(x: int, y: int) -> UiaProbe:
    """用**项目自己的** UIA 层看一眼这个点：能不能拿到控件、支不支持 ``InvokePattern``。

    走的就是 ``UiaBackend`` 的那条路（同一个客户端、同一个 ``walk_to_invokable``），
    所以这里的结论直接等于「界面上选 UIA 模式后会怎么做」——探针不会说一套、跑起来是另一套。
    """
    from app.win.uia import DEFAULT_MAX_PARENT_HOPS, UiaClient, walk_to_invokable

    result = UiaProbe()
    client = UiaClient()
    if not client.available:
        result.note = client.last_error or "UIA 不可用"
        return result
    result.available = True
    result.max_hops = DEFAULT_MAX_PARENT_HOPS

    element = client.element_from_point(x, y)
    if element is None:
        result.note = "该坐标处没取到控件（目标可能没暴露无障碍树）"
        return result

    try:
        info = element.info()
        result.point_name = info.name
        result.point_type = info.control_type_name
        result.point_class = info.class_name
        result.point_pid = info.process_id
        result.point_offscreen = info.offscreen
        result.point_invoke = element.can_invoke()

        target, hops = walk_to_invokable(element, DEFAULT_MAX_PARENT_HOPS)
        if target is not None:
            result.target_found = True
            result.target_hops = hops
            result.target_desc = target.info().describe()
            if target is not element:
                target.close()
    finally:
        element.close()
        client.close()
    return result


def report_uia(uia: UiaProbe) -> None:
    _rule("④ UIA 无障碍接口（PostMessage 走不通时的「中路」）")
    if not uia.available:
        print(f"  跳过：{uia.note}")
        return
    if uia.note:
        print(f"  ⚠ {uia.note}")

    print("  · 坐标下那个控件：")
    if uia.point_name is None and uia.point_type is None:
        print("      （没取到控件）")
    else:
        print(f"      名称                    : {uia.point_name!r}")
        print(f"      控件类型                : {uia.point_type}")
        print(f"      类名                    : {uia.point_class}")
        print(f"      所在进程 pid            : {uia.point_pid}")
        if uia.point_offscreen:
            print("      注意                    : 它当前是「离屏」状态")
        flag = "是" if uia.point_invoke else "否"
        print(f"      它自己支持 InvokePattern : {flag}")

    print()
    print(f"  · 往上 {uia.max_hops} 层找第一个能 Invoke 的控件（UIA 模式会点它）：")
    if uia.target_found:
        where = "就是坐标下这个控件" if uia.target_hops == 0 else f"往上 {uia.target_hops} 层"
        print(f"      ✅ {uia.target_desc}")
        print(f"         （{where}）")
        print("      → UIA 这条路是通的：直接 Invoke 它，不移动鼠标、不抢焦点。")
    else:
        print("      ❌ 没找到。UIA 只能点有 Invoke 语义的控件（按钮/链接/菜单项）。")
        print("         如果目标本该有按钮，多半是无障碍树没暴露出来：")
        print("           a) IDEA：Settings → Appearance & Behavior → Appearance →")
        print("              勾上 Support screen readers")
        print("           b) JBR 的 jabswitch -enable（改完要重启 IDEA）")


# ============================================================== 实测点击
def _pixel_region(x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    """以 ``(x, y)`` 为中心取一块要比对的屏幕区域（整块保证落在屏幕里）。"""
    from app.win.geometry import virtual_screen_rect

    screen = virtual_screen_rect()
    return clamp_region(x, y, width, height, (screen.x, screen.y, screen.right, screen.bottom))


def _capture(region: tuple[int, int, int, int]) -> bytes:
    from app.win.geometry import ScreenRect
    from app.win.screen import capture_screen

    return capture_screen(ScreenRect(*region)).data


def _settle(region: tuple[int, int, int, int]) -> tuple[bool, bytes]:
    """连拍两帧确认这块区域**自己不会变**，返回 ``(稳定吗, 基准像素)``。

    区域自己在动（动画、光标闪烁）时，后面任何「有变化」都是噪声。
    """
    before = _capture(region)
    time.sleep(0.25)
    settled = _capture(region)
    return before == settled, settled


def _judge(effect: ClickEffect, settled: bytes) -> None:
    """拍下动作之后的画面，与基准比对，结论写回 ``effect``。"""
    time.sleep(0.45)
    after = _capture(effect.region)
    if not effect.stable_before:
        effect.changed = None
        effect.note = "动手之前那块区域自己就在变（动画/光标闪烁），实测结果不可信"
    else:
        effect.changed = after != settled


def measure_click_effect(x: int, y: int, width: int, height: int) -> ClickEffect:
    """真的投一记后台点击，并比对点击前后那块屏幕的像素。

    先连拍两帧确认这块区域**自己不会变**（否则动画会让结论失真），再点击、再拍。
    """
    from app.input.base import MouseButton
    from app.input.postmessage import PostMessageBackend

    effect = ClickEffect()
    effect.region = _pixel_region(x, y, width, height)

    backend = PostMessageBackend()
    if not backend.bind_point(x, y):
        effect.note = "该坐标处没有可用的窗口，无法实测"
        return effect
    effect.tested = True

    target = backend._resolve(x, y)  # noqa: SLF001 - 探针需要展示解析结果
    if target is None:
        effect.note = "拿不到客户区坐标，无法实测"
        return effect
    hwnd, cx, cy = target
    effect.target_class = class_of(hwnd)
    effect.target_desc = describe_window(hwnd)
    effect.client = (cx, cy)

    effect.stable_before, settled = _settle(effect.region)
    backend.click(x, y, MouseButton.LEFT)
    _judge(effect, settled)
    return effect


def measure_uia_effect(x: int, y: int, width: int, height: int) -> ClickEffect:
    """用 UIA 真 Invoke 一记，并比对前后像素。

    这是「中路通不通」的唯一硬证据：PostMessage 没反应不代表没救——UIA 的 Invoke 走的是
    控件自己的语义，跟消息队列没关系。点的那个控件和 ``UiaBackend`` 跑起来点的是同一个。
    """
    from app.win.uia import DEFAULT_MAX_PARENT_HOPS, UiaClient, walk_to_invokable

    effect = ClickEffect()
    effect.region = _pixel_region(x, y, width, height)

    client = UiaClient()
    if not client.available:
        effect.note = client.last_error or "UIA 不可用"
        return effect
    element = client.element_from_point(x, y)
    if element is None:
        effect.note = "该坐标处没取到控件，没法实测 UIA"
        return effect

    try:
        target, hops = walk_to_invokable(element, DEFAULT_MAX_PARENT_HOPS)
        if target is None:
            effect.note = "这个控件及其祖先都不支持 InvokePattern，没法实测 UIA"
            return effect
        try:
            info = target.info()
            effect.tested = True
            effect.target_class = info.class_name
            effect.target_desc = info.describe() + (f"（往上 {hops} 层）" if hops else "")

            effect.stable_before, settled = _settle(effect.region)
            if not target.invoke():
                effect.changed = None
                effect.note = "InvokePattern.Invoke() 调用失败"
                return effect
            _judge(effect, settled)
        finally:
            if target is not element:
                target.close()
    finally:
        element.close()
        client.close()
    return effect


def report_effect(name: str, what: str, effect: ClickEffect) -> None:
    """打印一次实测的结果——PostMessage 与 UIA 用同一套说法，好直接对照。"""
    print(f"  · {name} —— {what}")
    if not effect.tested:
        print(f"      没做实测：{effect.note}")
        return
    if effect.target_desc:
        print(f"      实收对象  : {effect.target_desc}")
    if effect.client:
        print(f"      客户区坐标: {effect.client}")
    print(f"      比对区域  : {effect.region}")
    if effect.changed is None:
        print(f"      ⚠ {effect.note}")
        print("      → 这一项按「没做实测」处理。")
    elif effect.changed:
        print("      → 那块区域**变了**：这一路真的生效了。")
    else:
        print("      → 那块区域**没有变化**。注意：按钮被按下可能只闪一下，")
        print("        所以「没变化」是弱证据，「有变化」才是强证据。")


def report_effects(effect: ClickEffect, uia_effect: ClickEffect | None, why_no_uia: str) -> None:
    _rule("⑤ 实测：真点一记，看画面有没有变化")
    report_effect("后台投递 PostMessage", "「后台点击」模式干的就是这件事", effect)
    print()
    if uia_effect is None:
        print(f"  · UIA Invoke —— 没测：{why_no_uia}")
    else:
        report_effect("UIA Invoke", "「UIA 无障碍」模式干的就是这件事", uia_effect)


# ============================================================== 结论
def report_verdict(
    decision: Decision,
    template: TemplateInfo,
    uia: UiaProbe,
    name: str,
    saved: Path | None,
) -> None:
    _rule("结论")
    print(f"  路线：{ROUTE_LABELS[decision.route]}")
    print(f"  置信度：{decision.confidence}")
    print()
    print("  理由：")
    for reason in decision.reasons:
        print(f"    · {reason}")
    if decision.blockers:
        print()
        print("  卡点：")
        for blocker in decision.blockers:
            print(f"    · {blocker}")

    if template.ok:
        print()
        print(f"  模板：{template.grade} —— {template.quality}")
    else:
        print()
        print(f"  模板：没截到（{template.note}），方案里留空，需要到界面上手动截一张")

    print()
    _how_to(decision.route, name, uia)

    print()
    print("  " + "-" * 68)
    print("  可直接照抄的方案（存到 %LOCALAPPDATA%\\AutoClicker\\profiles\\<名字>.json，")
    print("  或加 --save 名字 让脚本直接落盘，然后在界面的方案列表里选它）：")
    print()
    print(profile_to_json(build_profile(name, template.template)))
    print()
    if saved is not None:
        print(f"  ✅ 已写入：{saved}")
        print("     重开 AutoClicker（或在方案下拉里刷新）即可选到它。")


def _how_to(route: str, name: str, uia: UiaProbe) -> None:
    print("  怎么用：")
    if route == ROUTE_POSTMESSAGE:
        print(f"    1. 打开 AutoClicker，载入方案「{name}」")
        print("    2. 输入方式选「后台点击（PostMessage，不占鼠标）」")
        print("    3. 点「绑定窗口」，然后到目标窗口上点一下（标签会显示绑到的标题）")
        print("    4. 按 F9 开始。执行期间鼠标照常用；目标窗口被遮挡甚至最小化也能收消息，")
        print("       但别把它关掉（窗口没了，消息就没地方投）")
        print("    注：输入方式不会被记住，每次启动都要重新选。")
        return

    if route == ROUTE_UIA:
        if uia.viable:
            print("    PostMessage 对这个目标走不通，但 **UIA 是通的**（见第 ④ 节：")
            print("    坐标下的控件或它的祖先支持 InvokePattern，能直接 Invoke）。")
            print("    UIA 不移动鼠标、不抢焦点，网页控件也照点。")
        else:
            print("    PostMessage 走不通，UIA 这边也没找到能 Invoke 的控件。")
            print("    想救活 UIA 这条中路的，先做这两件事再重跑本脚本：")
            print("      a) IDEA：Settings → Appearance & Behavior → Appearance →")
            print("         勾上 Support screen readers")
            print("      b) JBR 的 jabswitch -enable（改完要重启 IDEA）")
        print()
        print(f"    1. 打开 AutoClicker，载入方案「{name}」")
        print("    2. 输入方式选「UIA 无障碍（按控件点击，不移动鼠标）」")
        print("    3.（可选）点「绑定窗口」：UIA 模式下它是**安全校验**——")
        print("       坐标下的控件不属于绑定的窗口就跳过，免得点到遮挡者身上")
        print("    4. 按 F9 开始。执行期间鼠标照常用。")
        print("    注：UIA 模式下的键盘、移动、右键会回落到前台注入（日志里会提示一次）。")
        return

    print("    判不出结构，或者结构与实测都不支持后台点击 —— 先用前台跑起来：")
    print()
    print(f"    1. 打开 AutoClicker，载入方案「{name}」")
    print("    2. 输入方式保持「前台点击（SendInput）」")
    print("    3. 按 F9 开始。执行期间鼠标会被占用；要找图的那个区域必须显示在屏幕上")
    print("       （被别的窗口遮住的像素抓到的是遮挡物，不是按钮）")
    print("    4. 想中止按 F10。")


# ============================================================== 入口
def _rule(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="后台点击自检探针：自动判定该走哪条路，并给出可直接用的方案"
    )
    parser.add_argument("--point", nargs=2, type=int, metavar=("X", "Y"),
                        help="直接分析该屏幕物理像素坐标，跳过等待")
    parser.add_argument("--delay", type=int, default=5,
                        help="等待你把鼠标移到目标控件上的秒数，默认 5")
    parser.add_argument("--no-click", action="store_true",
                        help="不做实测点击（结论会退化为纯结构判定）")
    parser.add_argument("--no-capture", action="store_true",
                        help="不自动截模板，也不会移动光标（方案里的模板留空）")
    parser.add_argument("--crop", nargs=2, type=int, default=(96, 36), metavar=("W", "H"),
                        help="模板截取尺寸，默认 96 36；按钮大/小就调它")
    parser.add_argument("--save", metavar="方案名",
                        help="把生成的方案直接写进 profiles\\，不用手抄 JSON")
    parser.add_argument("--yes", action="store_true",
                        help="实测点击不再询问，直接点")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    if _user32 is None:
        print("这个脚本只能在 Windows 上跑（app/win/* 依赖 ctypes.WinDLL）。")
        return 1

    args = parse_args(argv)
    print("后台点击自检探针")
    print("提示：先把 IDEA 和那个弹窗摆好，鼠标停在『继续』按钮上。")

    if args.point:
        x, y = args.point
    else:
        print(f"\n{args.delay} 秒后读取光标位置，请把鼠标移到『继续』按钮上……")
        for remaining in range(args.delay, 0, -1):
            print(f"  {remaining}…", end="\r", flush=True)
            time.sleep(1)
        pos = cursor_pos()
        if pos is None:
            print("读取光标位置失败。")
            return 1
        x, y = pos
        print(f"  光标位置：({x}, {y})      ")

    # ---- 结构
    analysis = analyze_point(x, y)
    report_analysis(analysis)
    if not analysis.found:
        print("\n这个坐标上没有窗口，剩下的分析没意义。请把鼠标对准按钮重跑。")
        return 1
    pid = pid_of(analysis.top)
    if pid:
        report_process_windows(pid)

    # ---- 模板（必须在实测点击之前截，免得点击把界面改了）
    width, height = args.crop
    if args.no_capture:
        template = TemplateInfo(note="--no-capture")
    else:
        template = capture_template(x, y, width, height)
    report_template(template)

    # ---- UIA
    uia = probe_uia(x, y)
    report_uia(uia)

    # ---- 实测
    uia_effect: ClickEffect | None = None
    why_no_uia = ""
    if args.no_click:
        effect = ClickEffect(note="--no-click")
        why_no_uia = "--no-click"
    else:
        if not args.yes:
            print()
            answer = input("要现在用后台方式真点一下这个位置吗？(y/N) ").strip().lower()
        else:
            answer = "y"
        if answer != "y":
            effect = ClickEffect(note="你选择了跳过")
            why_no_uia = "你跳过了后台实测"
        else:
            effect = measure_click_effect(x, y, width, height)
            # 后台没生效时，接着试「中路」——「点了没反应」之后最该问的就是这个问题，
            # 答案直接决定要不要换成 UIA 模式。
            if effect.changed is True:
                why_no_uia = "后台投递已经看到变化，不用再试"
            elif not uia.viable:
                why_no_uia = "第 ④ 节里没找到能 Invoke 的控件"
            else:
                if args.yes:
                    more = "y"
                else:
                    print()
                    more = input(
                        "后台投递没看到变化。要用 UIA 真 Invoke 一下试试吗？(y/N) "
                    ).strip().lower()
                if more == "y":
                    uia_effect = measure_uia_effect(x, y, width, height)
                else:
                    why_no_uia = "你跳过了"
    report_effects(effect, uia_effect, why_no_uia)

    # ---- 结论
    decision = decide(
        framework=analysis.framework,
        chromium_in_chain=bool(analysis.chromium_rows),
        chromium_under_top=bool(analysis.chromium_under_top),
        click_effect=effect.changed,
        uia_effect=None if uia_effect is None else uia_effect.changed,
    )

    name = args.save or f"探针方案-{time.strftime('%m%d-%H%M')}"
    saved: Path | None = None
    if args.save:
        try:
            from app.storage.config import ProfileStore

            saved = ProfileStore().save(build_profile(args.save, template.template))
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该把结论也吃掉
            print(f"\n[警告] 写方案失败：{exc!r}（下面还是把 JSON 打出来给你手抄）")

    report_verdict(decision, template, uia, name, saved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
