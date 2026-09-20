"""Built-in defaults and environment variables.

This is the lowest two layers of configuration: a setting saved in the app
(database) wins over the environment, and the environment wins over the
defaults below. See settings_store.py.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Paperless
    paperless_url: str = ""  # empty: Paperless is not connected
    paperless_public_url: str = ""
    paperless_token: str = ""
    paperless_document_type: str = "Medical"
    paperless_tags: list[str] = ["Blood Test", "Medical Report", "Prescription", "Imaging"]

    # App
    data_dir: str = "/data"
    secret_key: str = ""
    app_password_hash: str = ""
    default_language: str = "en"
    # Extra origins allowed to send state-changing requests (a reverse proxy in
    # front of the app), as a JSON list: ["https://health.example.com"]
    trusted_origins: list[str] = []
    sync_interval_minutes: int = 240
    session_days: int = 30

    # Reading lab values: defaults only. The Settings page stores its own values
    # in the database, and those win (see reading_settings.py).
    reading_enabled: bool = True
    ollama_url: str = "http://host.docker.internal:11434"
    reader_a_model: str = "qwen3.5:4b"
    reader_b_model: str = "glm-ocr"
    reader_b_enabled: bool = True
    reading_dpi: int = 150
    reading_fallback_dpis: list[int] = [100, 200]
    ollama_num_ctx: int = 8192
    ollama_timeout_seconds: int = 300
    ollama_keep_alive: str = "5m"
    use_paperless_text: bool = True
    auto_read_after_sync: bool = False

    # Unknown keys left in the env file (from earlier versions) are
    # ignored rather than rejected.
    model_config = {"env_file": os.environ.get("VIBEHEALTH_ENV_FILE", ""), "extra": "ignore"}

    @property
    def database_path(self) -> str:
        return os.path.join(self.data_dir, "vibehealth.db")


@lru_cache
def get_settings() -> Settings:
    return Settings()
