"""权限检测与提权。

UIPI（用户界面特权隔离）会拦截低完整性级别进程向高完整性级别进程注入的输入。
因此当目标程序以管理员身份运行时，本工具也必须以管理员身份运行，否则点击会被静默丢弃。
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)


def is_admin() -> bool:
    """当前进程是否以管理员身份运行。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:  # pragma: no cover
        return False


def _launch_command() -> tuple[str, str]:
    """构造用于提权重启的可执行文件与参数。"""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = sys.argv[1:]
    else:
        exe = sys.executable
        args = sys.argv
    params = " ".join(f'"{a}"' for a in args)
    return exe, params


def relaunch_as_admin() -> bool:
    """以管理员身份重新启动当前程序。成功触发 UAC 时返回 True。

    注意：本函数不会退出当前进程，调用方需要在返回 True 后自行退出。
    """
    exe, params = _launch_command()
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    except OSError as exc:  # pragma: no cover
        logger.error("提权启动失败: %s", exc)
        return False
    # ShellExecuteW 返回值 > 32 表示成功；5 表示用户拒绝了 UAC
    return int(ret) > 32
