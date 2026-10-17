"""后台点击自检探针：先判定「后台点击」在目标程序上到底走哪条路可行。

为什么需要它
------------
``PostMessage`` 后台点击能不能用，**完全取决于目标程序认不认 ``WM_*`` 消息**：

- 经典 Win32 / Qt / Swing(AWT) 一般认；
- Chromium / Electron / JCEF / 游戏 基本不认（它们的输入不走 ``WM_*``）。

所以「我的后台点击对 IDEA 有没有戏」这个问题，不该靠猜，而应该先看一眼按钮
所在窗口到底是哪一类。本脚本把这件事变成一次运行就能看出来。

它做四件事
----------
1. 等你把鼠标移到目标控件上，读光标位置；
2. 打印该位置下的**窗口链与类名**，并判定属于哪一类 UI 框架；
3. 列出目标进程的顶层窗口，标出 **JCEF / Chromium 的痕迹**；
4. 用**项目自己的** ``PostMessageBackend`` 真点一下这个位置（可选），这是最直接的判定。

如果装了 ``uiautomation``，还会额外打印该点的 UIA 元素信息，并在整棵 UIA 树里
搜索「继续 / 取消」这类按钮，判断能不能走无障碍接口那条路。没装就跳过，不影响其它部分。

用法
----
在**项目根目录**运行（Windows + Python 3.10+）::

    python tools\\probe_background.py

常用参数::

    python tools\\probe_background.py --delay 8          # 给更长的准备时间
    python tools\\probe_background.py --point 1200 800   # 跳过等待，直接分析该坐标
    python tools\\probe_background.py --no-click         # 只分析，不做点击测试

只依赖标准库；要 UIA 部分再额外 ``pip install uiautomation``。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import sys
import time
from pathlib import Path

# 允许直接 `python tools\probe_background.py` 运行（否则 import app.* 会失败）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_user32 = ctypes.WinDLL("user32", use_last_error=True)

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.WindowFromPoint.argtypes = (wintypes.POINT,)
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetParent.argtypes = (wintypes.HWND,)
_user32.GetParent.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindowVisible.argtypes = (wintypes.HWND,)
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.EnumWindows.argtypes = (_EnumProc, wintypes.LPARAM)
_user32.EnumWindows.restype = wintypes.BOOL
_user32.EnumChildWindows.argtypes = (wintypes.HWND, _EnumProc, wintypes.LPARAM)
_user32.EnumChildWindows.restype = wintypes.BOOL

GA_PARENT = 1
GA_ROOT = 2

# 类名 → 结论。按前缀匹配，顺序有意义（先匹配到的先用）。
_CLASS_RULES: tuple[tuple[str, str], ...] = (
    ("SunAwt", "Java Swing/AWT  → 消息模拟(PostMessage) 通常【有效】"),
    ("Chrome_RenderWidgetHost", "Chromium 渲染控件 → 消息模拟【无效】，需要 UIA"),
    ("Chrome_WidgetWin", "Chromium 顶层窗口(JCEF/Electron) → 消息模拟【无效】，需要 UIA"),
    ("Chrome_", "Chromium → 消息模拟【无效】，需要 UIA"),
    ("CefBrowserWindow", "CEF 浏览器窗口 → 消息模拟【无效】，需要 UIA"),
    ("Cef", "CEF/Chromium → 消息模拟【无效】，需要 UIA"),
    ("Qt", "Qt → 消息模拟通常【有效】"),
    ("WindowsForms", ".NET WinForms → 消息模拟通常【有效】"),
    ("HwndWrapper", "WPF → 消息模拟通常【有效】"),
    ("ApplicationFrameWindow", "UWP/XAML → 消息模拟基本【无效】"),
    ("WinUIDesktopWin32WindowClass", "WinUI → 消息模拟基本【无效】"),
    ("Button", "经典 Win32 按钮控件 → 消息模拟【有效】"),
    ("Static", "经典 Win32 静态文本"),
    ("Edit", "经典 Win32 编辑框"),
    ("ComboBox", "经典 Win32 下拉框"),
    ("#32770", "经典 Win32 对话框"),
)


def _hwnd_int(handle) -> int | None:
    if not handle:
        return None
    raw = getattr(handle, "value", handle)
    return int(raw) if raw else None


def classify(class_name: str) -> str | None:
    for prefix, verdict in _CLASS_RULES:
        if class_name.startswith(prefix):
            return verdict
    return None


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


def window_from_point(x: int, y: int) -> int | None:
    return _hwnd_int(_user32.WindowFromPoint(wintypes.POINT(int(x), int(y))))


def ancestors(hwnd: int) -> list[int]:
    """从 hwnd 一路往上到顶层（含自身）。"""
    chain: list[int] = []
    current: int | None = hwnd
    seen: set[int] = set()
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        parent = _hwnd_int(_user32.GetAncestor(wintypes.HWND(current), GA_PARENT))
        current = parent
    return chain


def top_level_windows() -> list[int]:
    found: list[int] = []

    @_EnumProc
    def _cb(hwnd, _lparam):
        value = _hwnd_int(hwnd)
        if value:
            found.append(value)
        return True

    _user32.EnumWindows(_cb, 0)
    return found


def child_windows(hwnd: int) -> list[int]:
    found: list[int] = []

    @_EnumProc
    def _cb(child, _lparam):
        value = _hwnd_int(child)
        if value:
            found.append(value)
        return True

    _user32.EnumChildWindows(wintypes.HWND(hwnd), _cb, 0)
    return found


def describe_window(hwnd: int) -> str:
    name = class_of(hwnd)
    title = title_of(hwnd)
    shown = f" \"{title}\"" if title else ""
    return f"0x{hwnd:X}  {name}{shown}  (pid={pid_of(hwnd)}, visible={is_visible(hwnd)})"


def _rule(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def report_point(x: int, y: int) -> tuple[int | None, int | None]:
    """打印该坐标处的窗口链，返回 (最深窗口, 顶层窗口)。"""
    _rule(f"① 坐标 ({x}, {y}) 下的窗口链")
    deepest = window_from_point(x, y)
    if deepest is None:
        print("  该坐标处没有窗口（可能落在桌面/空白上）。")
        return None, None

    chain = ancestors(deepest)
    verdicts: list[str] = []
    for level, hwnd in enumerate(chain):
        cls = class_of(hwnd)
        verdict = classify(cls)
        mark = f"   ← {verdict}" if verdict else ""
        print(f"  第 {level} 层: {describe_window(hwnd)}{mark}")
        if verdict:
            verdicts.append(f"{cls} → {verdict}")

    top = chain[-1]
    print()
    print("  判定（自上而下第一处能定性的窗口类）：")
    if verdicts:
        for item in verdicts:
            print(f"    · {item}")
    else:
        print("    · 未识别出已知框架类名，可能是自绘界面；建议再看下面的 UIA 结果。")

    # 顶层窗口下有没有 Chromium 子窗口 —— JCEF 的典型痕迹
    kids = child_windows(top)
    chromium_kids = [h for h in kids if class_of(h).startswith(("Chrome_", "Cef"))]
    if chromium_kids:
        print()
        print("  ⚠ 顶层窗口里发现 Chromium/CEF 子窗口（JCEF 的典型特征）：")
        for h in chromium_kids[:8]:
            print(f"    · {describe_window(h)}")
        print("    → 说明界面里内嵌了网页，网页里的按钮【收不到】PostMessage，只能用 UIA。")
    return deepest, top


def report_process_windows(pid: int) -> None:
    _rule(f"② 进程 pid={pid} 的顶层窗口")
    wins = [h for h in top_level_windows() if pid_of(h) == pid]
    if not wins:
        print("  没找到该 pid 的顶层窗口。")
        return
    for hwnd in wins:
        print(f"  {describe_window(hwnd)}")
        kids = child_windows(hwnd)
        chromium_kids = [h for h in kids if class_of(h).startswith(("Chrome_", "Cef"))]
        if chromium_kids:
            print(f"      ↳ 内含 {len(chromium_kids)} 个 Chromium/CEF 子窗口（JCEF）")


def report_uia(x: int, y: int) -> None:
    _rule("③ UI Automation（无障碍接口）探测")
    try:
        import uiautomation as auto  # type: ignore
    except Exception:
        print("  未安装 uiautomation，跳过。想测这条路径请先执行：")
        print("      pip install uiautomation")
        print("  装好后重跑本脚本，就能看到按钮能不能被 UIA 找到、支不支持 Invoke。")
        return

    try:
        auto.SetGlobalSearchTimeout(3)
    except Exception:
        pass

    def _attr(ctrl, name: str, default: str = "?") -> str:
        try:
            value = getattr(ctrl, name)
        except Exception:  # noqa: BLE001 - 某些节点取属性就会抛
            return default
        return str(value)

    def _can_invoke(ctrl) -> bool:
        try:
            ctrl.GetInvokePattern()
            return True
        except Exception:  # noqa: BLE001
            return False

    # 3.1 光标下的元素
    print("  · 该坐标下的 UIA 元素：")
    try:
        ctrl = auto.ControlFromPoint(x, y)
        if ctrl is None:
            print("      （没取到元素）")
        else:
            print(f"      名称(Name)      : {_attr(ctrl, 'Name')!r}")
            print(f"      控件类型        : {_attr(ctrl, 'ControlTypeName')}")
            print(f"      类名(ClassName) : {_attr(ctrl, 'ClassName')}")
            print(f"      自动化 Id       : {_attr(ctrl, 'AutomationId')}")
            print(f"      进程 Id         : {_attr(ctrl, 'ProcessId')}")
            can = _can_invoke(ctrl)
            print("      支持 InvokePattern: " + ("是   ← 可以『真后台』调用，不移动鼠标" if can else "否"))
    except Exception as exc:  # noqa: BLE001 - 探针不该因为 UIA 异常中断
        print(f"      （取元素失败：{exc!r}）")

    # 3.2 全桌面找「继续/取消」这类按钮
    print()
    print("  · 在 UIA 树里搜索『继续/取消/Continue/Cancel/Resume』：")
    keywords = {"继续", "取消", "Continue", "Cancel", "Resume", "continue", "cancel"}
    hits = 0
    try:
        root = auto.GetRootControl()
        # 手工广度遍历：只用 GetChildren()，比依赖库里的遍历辅助函数更稳
        queue: list[tuple[object, int]] = [(root, 0)]
        budget = 6000
        while queue and budget > 0:
            ctrl, depth = queue.pop(0)
            budget -= 1
            try:
                name = ctrl.Name
            except Exception:  # noqa: BLE001
                name = ""
            if name in keywords:
                hits += 1
                can = "支持 InvokePattern ✅" if _can_invoke(ctrl) else "不支持 Invoke"
                print(f"      · {name!r}  [{_attr(ctrl, 'ControlTypeName')}]  "
                      f"类名={_attr(ctrl, 'ClassName')}  {can}")
            if depth >= 24 or budget <= 0:
                continue
            try:
                children = ctrl.GetChildren()
            except Exception:  # noqa: BLE001
                children = []
            for child in children or []:
                queue.append((child, depth + 1))
    except Exception as exc:  # noqa: BLE001
        print(f"      （遍历 UIA 树失败：{exc!r}）")

    if hits == 0:
        print("      没找到。可能是：按钮在 JCEF 网页里且 a11y 未激活、")
        print("      或 IDEA 的『Support screen readers』/ Java Access Bridge 没开。")
        print("      参考：Settings → Appearance & Behavior → Appearance → Support screen readers；")
        print("      以及 JBR 的 jabswitch -enable（需重启 IDEA）。")
    else:
        print()
        print(f"      ✅ 找到 {hits} 个候选按钮，说明 UIA 这条路可行。")


def report_backend_click(x: int, y: int) -> None:
    _rule("④ 用项目自带的后台后端真点一下（判定 PostMessage 是否被目标理会）")
    try:
        from app.input.base import MouseButton
        from app.input.postmessage import PostMessageBackend
    except Exception as exc:  # noqa: BLE001
        print(f"  无法导入项目模块：{exc!r}")
        print("  请确认在项目根目录运行，且用的是项目自带的 Python 环境。")
        return

    try:
        from app.win.admin import is_admin
        if not is_admin():
            print("  注意：当前是普通权限。若目标程序以管理员运行，UIPI 会拦掉注入的点击。")
    except Exception:
        pass

    backend = PostMessageBackend()
    if not backend.bind_point(x, y):
        print("  bind_point 失败：该坐标处没有可用的窗口。")
        return
    print(f"  已绑定顶层窗口：{backend.bound_title}")

    target = backend._resolve(x, y)  # noqa: SLF001 - 探针需要展示解析结果
    if target is None:
        print("  目标解析失败（拿不到客户区坐标）。")
        return
    hwnd, cx, cy = target
    print(f"  实际收件窗口：{describe_window(hwnd)}")
    print(f"      ← 类名 {class_of(hwnd)}：{classify(class_of(hwnd)) or '未识别'}")
    print(f"  客户区坐标  ：({cx}, {cy})")

    backend.click(x, y, MouseButton.LEFT)
    print("  已投递：WM_MOUSEMOVE → WM_LBUTTONDOWN → WM_LBUTTONUP")
    print("  → 请观察目标按钮有没有反应。没有反应基本就说明这类窗口不吃 WM_*。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="后台点击自检探针：判定目标程序适不适合 PostMessage / UIA 后台点击"
    )
    parser.add_argument("--point", nargs=2, type=int, metavar=("X", "Y"),
                        help="直接分析该屏幕物理像素坐标，跳过等待")
    parser.add_argument("--delay", type=int, default=5,
                        help="等待你把鼠标移到目标控件上的秒数，默认 5")
    parser.add_argument("--no-click", action="store_true",
                        help="只分析，不做实际的点击测试")
    args = parser.parse_args()

    print("后台点击自检探针")
    print("提示：请先把 IDEA 和那个弹窗摆好，鼠标停在『继续』按钮上。")

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
        print(f"  光标位置：({x}, {y})")

    deepest, top = report_point(x, y)

    pid = pid_of(top) if top else None
    if pid:
        report_process_windows(pid)

    report_uia(x, y)

    if args.no_click:
        print("\n（--no-click：跳过点击测试）")
    else:
        print("\n" + "-" * 72)
        answer = input("要现在用后台方式真点一下这个位置吗？(y/N) ").strip().lower()
        if answer == "y":
            report_backend_click(x, y)
        else:
            print("已跳过点击测试。")

    _rule("怎么读这份结果")
    print("  · 窗口链里出现 SunAwt*/Qt*/Button → PostMessage 有戏；出现 Chrome_*/Cef* → 没戏，走 UIA。")
    print("  · 第 ③ 节能列出『继续』且『支持 InvokePattern』→ UIA 方案可行。")
    print("  · 第 ④ 节点了没反应 → 这条路对你的目标无效。")
    print("  把这份输出贴回来，我按结果给你实现对应的方案。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
