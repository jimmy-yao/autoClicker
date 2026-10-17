"""DPI 感知设置。

必须在创建任何窗口（包括 QApplication）之前调用 ``enable_dpi_awareness()``。
否则 Windows 会对坐标做 DPI 虚拟化，导致点击位置出现比例偏移。

本模块只依赖标准库 ``ctypes``。
"""

from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger(__name__)

# 伪句柄：SetProcessDpiAwarenessContext 接受负值作为“上下文”
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V1 = ctypes.c_void_p(-3)
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = ctypes.c_void_p(-2)

# shcore.SetProcessDpiAwareness 的枚举值
PROCESS_DPI_UNAWARE = 0
PROCESS_SYSTEM_DPI_AWARE = 1
PROCESS_PER_MONITOR_DPI_AWARE = 2

S_OK = 0

_awareness: str | None = None


def enable_dpi_awareness() -> str:
    """按优先级尝试设置进程 DPI 感知级别，返回实际生效的级别。

    返回值为 ``"per-monitor-v2"`` / ``"per-monitor"`` / ``"system"`` / ``"none"``。
    重复调用是安全的（第二次会因权限被拒而返回上一次的结果）。
    """
    global _awareness
    if _awareness is not None:
        return _awareness

    user32 = ctypes.windll.user32

    # Windows 10 1703+：首选 Per-Monitor V2，它同时解决多屏不同缩放的问题
    set_ctx = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_ctx is not None:
        set_ctx.argtypes = [ctypes.c_void_p]
        set_ctx.restype = ctypes.c_bool
        try:
            if set_ctx(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
                _awareness = "per-monitor-v2"
                logger.info("DPI 感知级别: Per-Monitor V2")
                return _awareness
        except OSError:  # pragma: no cover - 仅在异常系统上触发
            pass

    # Windows 8.1+
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == S_OK:
            _awareness = "per-monitor"
            logger.info("DPI 感知级别: Per-Monitor")
            return _awareness
    except OSError:  # pragma: no cover
        pass

    # Vista+
    try:
        if user32.SetProcessDPIAware():
            _awareness = "system"
            logger.info("DPI 感知级别: System")
            return _awareness
    except OSError:  # pragma: no cover
        pass

    _awareness = "none"
    logger.warning("无法设置 DPI 感知，坐标可能出现缩放偏移")
    return _awareness


def current_awareness() -> str | None:
    """返回已生效的 DPI 感知级别，未设置时返回 ``None``。"""
    return _awareness
