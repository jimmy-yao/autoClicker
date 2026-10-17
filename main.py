"""AutoClicker 入口。

启动顺序很关键：
1. 声明 DPI 感知（必须在任何窗口 / QApplication 之前，否则坐标会被系统虚拟化）；
2. 初始化日志；
3. 创建 QApplication；
4. 抢占单实例锁，已被占用则直接提示退出；
5. 装配后端 / 热键 / 主窗口并进入事件循环。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 以源码方式运行时，保证 `import app.xxx` 可用（打包后由 PyInstaller 处理）
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import APP_NAME, __version__  # noqa: E402
from app.win.dpi import enable_dpi_awareness  # noqa: E402

logger = logging.getLogger("autoclicker.main")

# 单实例互斥量句柄必须活到进程结束，因此提升到模块级
_guard = None


def main() -> int:
    global _guard

    # 1. DPI 感知必须最先执行
    awareness = enable_dpi_awareness()

    # 2. 日志
    from app.utils.logger import setup_logging

    setup_logging()
    logger.info("%s v%s 启动（DPI 感知=%s, Python %s）", APP_NAME, __version__, awareness, sys.version.split()[0])

    # 3. Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.input.sendinput import SendInputBackend
    from app.ui.icon import app_icon
    from app.ui.main_window import MainWindow
    from app.win.admin import is_admin
    from app.win.hotkey import HotkeyManager
    from app.win.singleton import SingleInstance

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    # 关闭主窗口只是隐藏到托盘，不能顺带退出进程
    app.setQuitOnLastWindowClosed(False)

    # 4. 单实例
    _guard = SingleInstance()
    if not _guard.acquire():
        logger.warning("检测到已有实例在运行，退出")
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} 已在运行中。")
        return 0

    # 5. 装配
    backend = SendInputBackend()
    hotkeys = HotkeyManager(app)
    window = MainWindow(backend, hotkeys)

    if not is_admin():
        logger.info("当前以普通权限运行")
        window.log_append.emit(
            "[注意] 当前为普通权限。若目标程序以管理员身份运行，"
            "UIPI 会丢弃本工具注入的点击（表现为「点了没反应」），此时请以管理员身份重新运行本工具。"
        )

    window.show()

    exit_code = app.exec()
    logger.info("%s 退出，code=%s", APP_NAME, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
