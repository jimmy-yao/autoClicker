"""找图失败时的现场快照。

找图最难受的失败方式是「什么都没发生」：用户看不出是模板过期了、容差太小，
还是目标弹窗根本没出现。超时中止时把最后一帧存下来，打开一看就清楚了。

快照只在**中止**模式下保存（``ON_TIMEOUT_STOP``）。如果是「继续」模式，
一个循环里反复超时的等待步骤会写出一堆几乎一样的图，反而把有用的那张埋掉。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtGui import QImage

from ..utils.logger import app_data_dir

logger = logging.getLogger(__name__)

# 只留最近这么多张，避免长时间挂机把磁盘写满
SHOT_LIMIT = 10


def shots_dir() -> Path:
    return app_data_dir() / "shots"


def save_frame(frame) -> Path | None:
    """把一帧屏幕存成 PNG，返回文件路径。失败只记日志，绝不打断宏执行。"""
    try:
        directory = shots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _unique_path(directory)

        # Format_RGB32 在小端机器上的内存布局正好是 BGRA，和抓屏输出一致；
        # 它忽略 alpha 通道，这也正是我们要的（GDI 抓回来的 alpha 是 0）。
        image = QImage(
            frame.data,
            frame.width,
            frame.height,
            frame.stride,
            QImage.Format.Format_RGB32,
        )
        if image.isNull() or not image.save(str(path), "PNG"):
            raise OSError("Qt 无法编码这张图")

        _prune(directory)
        return path
    except (OSError, ValueError) as exc:
        logger.warning("保存现场快照失败: %s", exc)
        return None


def _unique_path(directory: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"找图失败-{stamp}.png"
    index = 2
    while path.exists():
        path = directory / f"找图失败-{stamp}-{index}.png"
        index += 1
    return path


def _prune(directory: Path) -> None:
    """按修改时间删掉最旧的快照，只留 ``SHOT_LIMIT`` 张。"""
    try:
        shots = sorted(
            directory.glob("*.png"), key=lambda item: item.stat().st_mtime, reverse=True
        )
    except OSError:  # pragma: no cover - 目录刚被删掉之类
        return

    for stale in shots[SHOT_LIMIT:]:
        try:
            stale.unlink()
        except OSError as exc:  # pragma: no cover - 文件被占用
            logger.debug("清理旧快照失败 %s: %s", stale.name, exc)
