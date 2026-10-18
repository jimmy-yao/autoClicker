"""基于 UI Automation 的输入后端：按「控件」点击，不移动鼠标、不抢焦点。

它解决的是 PostMessage 解决不了的那一类目标
--------------------------------------------
``PostMessage`` 只对自己处理 ``WM_*`` 消息的经典 Win32 窗口有效。Chromium / Electron /
JCEF / UWP 这类程序的输入不走消息队列，投进去等于石沉大海——IDEA 的 AI 面板（JCEF）就是
典型例子。

UIA 换了个思路：不问「坐标下是哪个窗口」，而问「坐标下是哪个**控件**」，然后调用这个控件的
``InvokePattern.Invoke()``。Invoke 是控件在无障碍接口上暴露的「我被按下了」这个语义，由
控件自己的代码处理，**不经过鼠标、不经过消息队列**，因此：

- 不移动系统光标，宏跑着的时候鼠标照常用；
- 不改变焦点、不要求目标窗口在前台；
- 不受「认不认 ``WM_*``」的限制（网页控件照点）。

所以它和「找图」是绝配：找图负责**在哪儿**（纯像素，与框架无关），UIA 负责**怎么点**
（控件语义，与框架无关）。IDEA 那个「继续」按钮正是这么点下来的。

代价与边界（不是 bug，是这条路的固有约束）
------------------------------------------
- **要求目标程序暴露无障碍树**：Swing 要开 Java Access Bridge + IDEA 的
  「Support screen readers」，Chromium 要 a11y 被激活。树里没有这个控件就点不了。
- **只能点有 Invoke 语义的控件**：按钮、链接、菜单项一般支持；纯自绘控件、文本框的
  任意位置不支持。点到的常常是按钮内部的**文本**子节点，所以本后端会往上找几层，
  点第一个支持 Invoke 的祖先。
- **靠屏幕坐标取控件**（``ElementFromPoint``）：该点被别的窗口挡住时会取到遮挡者的控件。
  绑定窗口之后会核对进程号，不匹配就跳过而不是乱点。
- **右键/中键、移动光标、按键在 UIA 里没有对应语义**：这几种操作**回落到前台
  ``SendInput``**，并会在日志里提示一次。换言之 UIA 模式保证的是「左键点击不占鼠标」，
  而不是「整个宏绝对不碰鼠标」。
"""

from __future__ import annotations

import logging

from ..win import uia as uia_module
from .base import InputBackend, MouseButton

logger = logging.getLogger(__name__)


class UiaBackend(InputBackend):
    """按控件 Invoke 的点击后端。

    ``fallback`` 用于 UIA 没有对应语义的那几种操作（移动、按键、右键/中键），
    通常传前台 ``SendInputBackend``；不传的话这些操作会被忽略并给出警告。
    """

    name = "uia"

    def __init__(
        self,
        client=None,
        fallback: InputBackend | None = None,
        max_parent_hops: int = uia_module.DEFAULT_MAX_PARENT_HOPS,
    ) -> None:
        self._client = client if client is not None else uia_module.UiaClient()
        self._fallback = fallback
        self._max_parent_hops = max(0, int(max_parent_hops))
        self._hwnd: int | None = None
        self._pid: int = 0
        self._title: str = ""
        self._notices: set[str] = set()

    # ---------------------------------------------------------------- 绑定
    @property
    def bound_window(self) -> int | None:
        return self._hwnd

    @property
    def bound_title(self) -> str:
        """绑定窗口的「标题 [类名]」，未绑定时为空串。"""
        return self._title

    @property
    def available(self) -> bool:
        """UIA 是否可用（会真的去建一次 COM 对象，所以别在界面线程上调）。"""
        return bool(self._client.available)

    @property
    def unavailable_reason(self) -> str:
        return self._client.last_error or ""

    def bind_window(self, hwnd: int | None) -> bool:
        """绑定目标窗口，**只作为安全校验**：坐标下的控件不属于它时跳过。

        UIA 是按屏幕坐标取控件的，不需要绑定也能点；绑定只是为了在「按钮被别的
        窗口挡住」时宁可不动手，也不要点到别的窗口的控件上去。
        """
        # 局部 import：app.win.window 在 import 阶段就调 ctypes.WinDLL（非 Windows 上
        # 不存在），放进来才能让本模块在任何平台上被 import，从而被测试。
        from ..win.window import describe, is_window, process_id, root_window

        if not hwnd or not is_window(hwnd):
            return False
        root = root_window(int(hwnd)) or int(hwnd)
        self._hwnd = root
        self._pid = process_id(root)
        self._title = describe(root)
        self._notices.discard("foreign")
        logger.info("UIA 点击已绑定窗口: %s (hwnd=0x%X, pid=%s)", self._title, root, self._pid)
        return True

    def bind_point(self, x: int, y: int) -> bool:
        from ..win.window import window_from_point

        hwnd = window_from_point(x, y)
        if not hwnd:
            return False
        return self.bind_window(hwnd)

    def unbind(self) -> None:
        self._hwnd = None
        self._pid = 0
        self._title = ""

    def close(self) -> None:
        self.unbind()
        self._client.close()

    def reset_notices(self) -> None:
        """每次开始运行前重置「只提示一次」的标记。"""
        self._notices.clear()

    # ---------------------------------------------------------------- 提示
    def _note(self, key: str, level: int, message: str) -> None:
        """同一个 key 只提示一次（一次宏里可能有成百次点击，别刷屏）。"""
        if key in self._notices:
            return
        self._notices.add(key)
        logger.log(level, message)

    def _delegate(self, key: str, message: str, action) -> None:
        """把 UIA 没有语义的操作交回前台后端，并提示一次。"""
        if self._fallback is None:
            self._note(key, logging.WARNING, f"{message}；但没有可用的前台后端，本次已忽略")
            return
        self._note(key, logging.INFO, message)
        action()

    # ---------------------------------------------------------------- 注入
    def click(self, x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        button = MouseButton(button)
        if button is not MouseButton.LEFT:
            self._delegate(
                f"button-{button.value}",
                f"UIA 只处理左键，{button}点击已回落到前台注入",
                lambda: self._fallback.click(x, y, button),
            )
            return

        element = self._client.element_from_point(x, y)
        if element is None:
            self._note(
                "no-element",
                logging.WARNING,
                f"UIA 在坐标 ({x}, {y}) 处没取到控件：目标可能没暴露无障碍树"
                f"（Swing 需开 Support screen readers + Java Access Bridge），"
                f"或该点当前不可见",
            )
            return

        try:
            info = element.info()
            if self._is_foreign(info):
                self._note(
                    "foreign",
                    logging.WARNING,
                    f"坐标 ({x}, {y}) 下的控件不属于绑定的窗口（{info.describe()}），"
                    f"已跳过；窗口被遮挡或已切换时会出现这个提示",
                )
                return
            self._invoke_chain(element, info)
        finally:
            element.close()

    def _is_foreign(self, info) -> bool:
        """控件是否不属于绑定的窗口（只在绑定过、且双方都拿得到 pid 时才判）。"""
        if self._hwnd is None or not self._pid:
            return False
        return bool(info.process_id) and info.process_id != self._pid

    def _invoke_chain(self, element, info) -> None:
        """从 ``element`` 起往上找第一个支持 ``InvokePattern`` 的控件并点它。

        找的活儿交给 :func:`app.win.uia.walk_to_invokable`（探针也用同一份实现）；
        这里只管点、记日志、以及把多取来的那个指针还掉。
        """
        target, hops = uia_module.walk_to_invokable(element, self._max_parent_hops)
        try:
            if target is None:
                self._note(
                    "no-invoke",
                    logging.WARNING,
                    f"坐标下的控件及其上 {self._max_parent_hops} 层祖先都不支持 "
                    f"InvokePattern（{info.describe()}），UIA 只能点有 Invoke 语义的控件",
                )
                return
            if target.invoke():
                where = f"（从{info.control_type_name}往上 {hops} 层）" if hops else ""
                self._note("invoked", logging.INFO, f"UIA 已点击：{info.describe()}{where}")
            else:
                self._note(
                    "invoke-failed",
                    logging.WARNING,
                    f"找到了 InvokePattern 但调用失败：{info.describe()}",
                )
        finally:
            # element 由 click() 的 finally 释放，这里只还自己多取的那个
            if target is not None and target is not element:
                target.close()

    def move_to(self, x: int, y: int) -> None:
        self._delegate(
            "move",
            "UIA 没有「移动光标」的概念，「移动」步骤已回落到前台注入（会移动真实光标）",
            lambda: self._fallback.move_to(x, y),
        )

    def press_key(self, vk: int) -> None:
        self._delegate(
            "key",
            "UIA 没有按键接口，按键已回落到前台注入（发往当前焦点窗口）",
            lambda: self._fallback.press_key(vk),
        )
