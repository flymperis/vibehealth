"""Settings for reading lab values: stored in the database, env vars as defaults."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from . import settings_store
from .config import get_settings
from .db import engine

PREFIX = "reading."
MODEL_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


class ReadingSettings(BaseModel):
    enabled: bool = True
    ollama_url: str = "http://host.docker.internal:11434"
    reader_a_model: str = "qwen3.5:4b"
    reader_b_enabled: bool = True
    reader_b_model: str = "glm-ocr"
    dpi: int = Field(150, ge=72, le=300)
    fallback_dpis: list[int] = Field(default_factory=lambda: [100, 200], max_length=3)
    num_ctx: int = Field(8192, ge=2048, le=32768)
    timeout_seconds: int = Field(300, ge=30, le=1800)
    keep_alive: str = "5m"
    use_paperless_text: bool = True
    auto_read_after_sync: bool = False

    model_config = {"extra": "forbid"}

    @field_validator("ollama_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(:\d{1,5})?(/[^\s]*)?", v):
            raise ValueError("must be an http(s) URL, e.g. http://localhost:11434")
        return v

    @field_validator("reader_a_model", "reader_b_model")
    @classmethod
    def _model(cls, v: str) -> str:
        v = v.strip()
        if not MODEL_RX.fullmatch(v):
            raise ValueError("not a valid Ollama model name")
        return v

    @field_validator("fallback_dpis")
    @classmethod
    def _dpis(cls, v: list[int]) -> list[int]:
        for dpi in v:
            if not 72 <= dpi <= 300:
                raise ValueError("each fallback dpi must be between 72 and 300")
        return list(dict.fromkeys(v))

    @field_validator("keep_alive")
    @classmethod
    def _keep_alive(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"-1|0|\d{1,5}[smh]?", v):
            raise ValueError("use e.g. 5m, 30s, 1h, 0 (unload at once) or -1 (keep loaded)")
        return v


# ReadingSettings field -> environment-backed attribute of config.Settings
ENV = {
    "enabled": "reading_enabled",
    "ollama_url": "ollama_url",
    "reader_a_model": "reader_a_model",
    "reader_b_enabled": "reader_b_enabled",
    "reader_b_model": "reader_b_model",
    "dpi": "reading_dpi",
    "fallback_dpis": "reading_fallback_dpis",
    "num_ctx": "ollama_num_ctx",
    "timeout_seconds": "ollama_timeout_seconds",
    "keep_alive": "ollama_keep_alive",
    "use_paperless_text": "use_paperless_text",
    "auto_read_after_sync": "auto_read_after_sync",
}

# The reading.* keys live in the generic settings store like every other section.
SECTION = settings_store.Section("reading", ReadingSettings, env=ENV)
settings_store.register(SECTION)


def defaults() -> ReadingSettings:
    """What applies without anything saved in the app: environment, else built-in."""
    s = get_settings()
    return ReadingSettings(**{name: getattr(s, attr) for name, attr in ENV.items()})


def sources() -> dict[str, str]:
    """Per setting: 'app' (saved in the app), 'env' or 'default'."""
    return settings_store.resolve(SECTION, use_cache=False).sources


def load() -> ReadingSettings:
    data = defaults().model_dump()
    with Session(engine) as session:
        for key, raw in settings_store.rows(session, PREFIX).items():
            name = key[len(PREFIX):]
            if name in data:
                try:
                    data[name] = json.loads(raw)
                except ValueError:
                    pass
    try:
        return ReadingSettings(**data)
    except ValueError:
        # A stored value that no longer validates must not break reading.
        return defaults()


def save(new: ReadingSettings) -> ReadingSettings:
    """Store every field that differs from the env default; drop the rest."""
    base = defaults().model_dump()
    with Session(engine) as session:
        for name, value in new.model_dump().items():
            key = PREFIX + name
            if value == base[name]:
                settings_store.drop(session, key)
            else:
                settings_store.put(session, key, json.dumps(value))
        session.commit()
    settings_store.clear_cache()
    return load()
