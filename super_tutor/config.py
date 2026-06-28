"""Configuration management for Super Tutor.

Reads settings from ~/.super-tutor/settings.json with env-var override (TUTOR_ prefix).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TutorConfig:
    """Super Tutor 配置。

    Attributes:
        api_key: API 密钥。
        api_base_url: API 基础 URL。
        db_path: SQLite 数据库文件路径。
        model: 默认模型名称。
    """

    api_key: str = ""
    api_base_url: str = "https://api.deepseek.com"
    db_path: str = "~/.super-tutor/super_tutor.db"
    model: str = "deepseek-chat"

    # ------------------------------------------------------------------
    # Factory: load from file + env
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> TutorConfig:
        """从 settings.json 和环境变量加载配置。

        优先级：环境变量 > settings.json > 默认值。
        """
        config = cls()
        config._load_from_file()
        config._apply_env_overrides()
        return config

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _settings_path(self) -> Path:
        return Path.home() / ".super-tutor" / "settings.json"

    def _load_from_file(self) -> None:
        path = self._settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return

        # Map settings.json keys → dataclass fields
        _FILE_KEY_MAP = {
            "deepseek_api_key": "api_key",
            "deepseek_base_url": "api_base_url",
            "api_key": "api_key",
            "api_base_url": "api_base_url",
            "db_path": "db_path",
            "model": "model",
        }
        for file_key, attr in _FILE_KEY_MAP.items():
            if file_key in data:
                setattr(self, attr, data[file_key])

    def _apply_env_overrides(self) -> None:
        _ENV_MAP = {
            "TUTOR_API_KEY": ("api_key", str),
            "TUTOR_API_BASE_URL": ("api_base_url", str),
            "TUTOR_DB_PATH": ("db_path", str),
            "TUTOR_MODEL": ("model", str),
        }
        for env_var, (attr, cast) in _ENV_MAP.items():
            value = os.environ.get(env_var)
            if value is not None:
                try:
                    setattr(self, attr, cast(value))
                except (ValueError, TypeError):
                    pass
