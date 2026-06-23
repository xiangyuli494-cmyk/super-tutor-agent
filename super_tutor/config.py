"""Configuration management for Super Tutor Agent.

Reads settings from ~/.super-tutor/settings.json with env-var override (TUTOR_ prefix).
"""

import json
import os
from pathlib import Path
from typing import Optional


class ForgeConfig:  # kept for backward compat with LLMClient / Database
    """Singleton config manager.

    Attributes:
        deepseek_api_key: API key for DeepSeek.
        deepseek_base_url: Base URL for DeepSeek API.
        token_budget_default: Default token budget.
    """

    _instance: Optional["ForgeConfig"] = None

    def __init__(self) -> None:
        self.deepseek_api_key: str = ""
        self.deepseek_base_url: str = "https://api.deepseek.com"
        self.token_budget_default: int = 1_000_000

        self._load_from_file()
        self._apply_env_overrides()

    @classmethod
    def get_instance(cls) -> "ForgeConfig":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

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

        self.deepseek_api_key = data.get("deepseek_api_key", self.deepseek_api_key)
        self.deepseek_base_url = data.get("deepseek_base_url", self.deepseek_base_url)
        self.token_budget_default = data.get("token_budget_default", self.token_budget_default)

    def _apply_env_overrides(self) -> None:
        for env_var, attr in [
            ("TUTOR_API_KEY", "deepseek_api_key"),
            ("TUTOR_API_BASE_URL", "deepseek_base_url"),
            ("TUTOR_TOKEN_BUDGET", "token_budget_default"),
        ]:
            value = os.environ.get(env_var)
            if value is not None:
                if attr == "token_budget_default":
                    try:
                        setattr(self, attr, int(value))
                    except ValueError:
                        pass
                else:
                    setattr(self, attr, value)

    @property
    def api_key(self) -> str:
        return self.deepseek_api_key

    @property
    def api_base_url(self) -> str:
        return self.deepseek_base_url

    @property
    def model_heavy(self) -> str | None:
        return None

    @property
    def model_medium(self) -> str | None:
        return None

    @property
    def model_light(self) -> str | None:
        return None

    @classmethod
    def reset(cls) -> None:
        cls._instance = None
