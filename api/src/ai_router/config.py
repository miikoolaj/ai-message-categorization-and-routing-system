"""Application configuration loaded from environment variables (validated at startup)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings. Field names map to upper-case environment variables.

    Every value has a default, so the service starts without any `.env` file.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    # --- LLM (Ollama) ---------------------------------------------------------------
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    ollama_keep_alive: str = "24h"
    warmup_on_startup: bool = True
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_seed: int | None = 42
    llm_max_tokens: int = Field(default=256, ge=16, le=8192)
    llm_thinking: bool | None = None
    llm_request_timeout_seconds: float = Field(default=120.0, gt=0)

    # --- Agent and routing ------------------------------------------------------------
    agent_timeout_seconds: float = Field(default=180.0, gt=0)
    agent_tool_retries: int = Field(default=2, ge=0, le=10)
    agent_output_retries: int = Field(default=2, ge=0, le=10)
    agent_request_limit: int = Field(default=6, ge=2, le=50)
    max_concurrent_agent_runs: int = Field(default=2, ge=1, le=64)
    agent_queue_timeout_seconds: float = Field(default=30.0, gt=0)
    max_message_chars: int = Field(default=4000, ge=100, le=20000)
    routing_fallback_enabled: bool = True

    # --- SMTP ---------------------------------------------------------------------------
    smtp_host: str = "mailpit"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    smtp_use_tls: bool = False
    smtp_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- E-mail content -------------------------------------------------------------------
    mail_from: str = "AI Router <ai-router@example.com>"
    mail_subject_prefix: str = "[AI Router]"
    mail_message_id_domain: str = "ai-router.local"

    # --- Idempotency ----------------------------------------------------------------------
    idempotency_ttl_seconds: int = Field(default=86_400, ge=1)
    idempotency_max_entries: int = Field(default=10_000, ge=1)

    # --- Logging --------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_message_content: bool = False

    @field_validator("llm_thinking", "llm_seed", "smtp_username", "smtp_password", mode="before")
    @classmethod
    def _empty_string_is_none(cls, value: object) -> object:
        """Treat `VAR=` (empty value, e.g. from .env) as "not set"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ollama_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton (cached after the first call)."""
    return Settings()
