"""方案与设置的持久化。

磁盘布局（``base_dir`` 默认是 ``%LOCALAPPDATA%\\AutoClicker``）::

    AutoClicker/
    ├─ settings.json           # 应用设置（最近使用的方案等）
    ├─ profiles/
    │  ├─ 默认方案.json
    │  └─ 日常签到.json
    └─ logs/
       └─ autoclicker.log

写入一律走「临时文件 + ``os.replace``」的原子替换，避免中途崩溃留下半个文件。
单个方案文件损坏时只跳过它，不会连带整个方案列表打不开。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from ..core.models import Profile
from ..utils.logger import app_data_dir

logger = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')
SETTINGS_VERSION = 1


def _safe_filename(name: str, fallback: str = "未命名方案") -> str:
    """把方案名转成安全的文件名（不含扩展名）。"""
    cleaned = _INVALID_CHARS.sub("_", name).strip().strip(".")
    return cleaned or fallback


class ProfileStore:
    """方案仓库。"""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else app_data_dir()
        self.profiles_dir = self.base_dir / "profiles"
        self.settings_path = self.base_dir / "settings.json"
        self._settings: dict | None = None

    # ------------------------------------------------------------------ 方案
    def list_names(self) -> list[str]:
        """列出全部方案名（按名称排序）。目录不存在时返回空列表。"""
        if not self.profiles_dir.is_dir():
            return []
        return sorted(path.stem for path in self.profiles_dir.glob("*.json"))

    def exists(self, name: str) -> bool:
        return self._path_for(name).is_file()

    def load(self, name: str) -> Profile:
        """读取方案。文件不存在或损坏时抛 ``ValueError``。"""
        path = self._path_for(name)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"方案文件不存在: {path.name}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"方案文件无法读取: {path.name}（{exc}）") from exc

        if not isinstance(raw, dict):
            raise ValueError(f"方案文件格式不正确: {path.name}")

        profile = Profile.from_dict(raw)
        # 以文件名为准，避免文件里 name 字段和文件名不一致造成错乱
        profile.name = name
        return profile

    def save(self, profile: Profile) -> Path:
        """保存方案（原子写入），返回落盘路径。"""
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(profile.name)
        self._atomic_write_json(path, profile.to_dict())
        logger.info("方案已保存: %s", path)
        return path

    def delete(self, name: str) -> bool:
        """删除方案。文件不存在时返回 False。"""
        path = self._path_for(name)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.error("删除方案失败 %s: %s", path, exc)
            return False
        logger.info("方案已删除: %s", path)
        return True

    def rename(self, old_name: str, new_name: str) -> bool:
        """重命名方案。"""
        old_path = self._path_for(old_name)
        new_path = self._path_for(new_name)
        if not old_path.is_file() or new_path.exists():
            return False
        try:
            old_path.replace(new_path)
        except OSError as exc:
            logger.error("重命名方案失败 %s -> %s: %s", old_path, new_path, exc)
            return False
        return True

    # ------------------------------------------------------------------ 设置
    @property
    def settings(self) -> dict:
        if self._settings is None:
            self._settings = self._read_settings()
        return self._settings

    def get_last_profile(self) -> str | None:
        value = self.settings.get("last_profile")
        return str(value) if isinstance(value, str) and value else None

    def set_last_profile(self, name: str) -> None:
        self.settings["last_profile"] = name
        self._write_settings()

    # ------------------------------------------------------------------ 内部
    def _path_for(self, name: str) -> Path:
        return self.profiles_dir / f"{_safe_filename(name)}.json"

    def _read_settings(self) -> dict:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": SETTINGS_VERSION}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("settings.json 无法读取，使用默认设置: %s", exc)
            return {"version": SETTINGS_VERSION}
        return raw if isinstance(raw, dict) else {"version": SETTINGS_VERSION}

    def _write_settings(self) -> None:
        self.settings.setdefault("version", SETTINGS_VERSION)
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(self.settings_path, self.settings)
        except OSError as exc:  # pragma: no cover - 磁盘不可写时降级
            logger.warning("settings.json 写入失败: %s", exc)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
