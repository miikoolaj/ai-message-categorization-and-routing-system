"""Application configuration loaded from environment variables (validated at startup)."""

import re
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Rough upper bound of the static prompt (instructions + tool schema) in tokens, and a conservative
# characters-per-token ratio for user text (Polish ~2.5-3, CJK ~1). Used only for sanity checks.
_PROMPT_OVERHEAD_TOKENS = 1600
_CHARS_PER_TOKEN_WORST_CASE = 2.0

_KEEP_ALIVE = re.compile(r"-?\d+(\.\d+)?|-?(\d+(\.\d+)?(ns|us|µs|ms|s|m|h))+")


class Settings(BaseSettings):
    """All runtime settings. Field names map to upper-case environment variables.

    Every value has a default, so the service starts without any `.env` file.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    # --- LLM (Ollama) -----------------------------------------------------------------------
    llm_backend: Literal["ollama", "fake"] = "ollama"
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    ollama_keep_alive: str = "-1"
    ollama_context_length: int = Field(default=8192, ge=512)  # mirror of the server setting
    ollama_num_parallel: int = Field(default=1, ge=1)  # mirror of the server setting
    warmup_on_startup: bool = True
    readiness_require_warm: bool = False
    model_watchdog_interval_seconds: float = Field(default=30.0, ge=0)  # 0 = disabled
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_seed: int | None = 42
    llm_max_tokens: int = Field(default=256, ge=16, le=8192)
    llm_thinking: bool | None = None
    llm_request_timeout_seconds: float = Field(default=120.0, gt=0)

    # --- Agent and routing --------------------------------------------------------------------
    routing_policy_file: str | None = None
    agent_mode: Literal["tool", "output_function"] = "tool"
    agent_timeout_seconds: float = Field(default=180.0, gt=0)
    agent_tool_retries: int = Field(default=2, ge=0, le=10)
    agent_output_retries: int = Field(default=2, ge=0, le=10)
    agent_request_limit: int = Field(default=6, ge=2, le=50)
    max_concurrent_agent_runs: int = Field(default=1, ge=1, le=64)
    agent_queue_timeout_seconds: float = Field(default=30.0, gt=0)
    max_message_chars: int = Field(default=4000, ge=100, le=20000)
    routing_fallback_enabled: bool = True
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    low_confidence_action: Literal["flag", "other"] = "flag"
    context_warning_ratio: float = Field(default=0.8, gt=0.0, le=1.0)

    # --- Storage (SQLite: tickets, outbox, idempotency, feedback) -------------------------------
    database_path: str = "/app/data/ai-router.sqlite3"
    ticket_retention_days: int = Field(default=30, ge=1)
    feedback_retention_days: int = Field(default=180, ge=1)

    # --- Outbox (durable e-mail delivery) --------------------------------------------------
    mail_sync_delivery_timeout_seconds: float = Field(default=5.0, gt=0)
    outbox_max_attempts: int = Field(default=12, ge=1)
    outbox_retry_base_seconds: float = Field(default=2.0, gt=0)
    outbox_retry_max_seconds: float = Field(default=300.0, gt=0)

    # --- SMTP --------------------------------------------------------------------------------
    smtp_host: str = "mailpit"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    smtp_use_tls: bool = False
    smtp_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- E-mail content ------------------------------------------------------------------------
    mail_from: str = "AI Router <ai-router@example.com>"
    mail_subject_prefix: str = "[AI Router]"
    mail_subject_language: Literal["auto", "pl"] = "pl"  # readers are Polish departments
    mail_message_id_domain: str = "ai-router.local"

    # --- Security: authentication, rate limiting -------------------------------------------
    auth_mode: Literal["none", "api_key", "oidc"] = "none"
    api_keys: str = ""  # "client-a:<sha256 hex of key>,client-b:<sha256 hex>"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_email_claim: str = "email"
    admin_token: SecretStr | None = None
    rate_limit_per_minute: int = Field(default=30, ge=0)  # 0 = disabled
    rate_limit_burst: int = Field(default=10, ge=1)

    # --- Feedback loop -------------------------------------------------------------------------
    feedback_enabled: bool = True
    public_base_url: str = "http://localhost:8080"
    feedback_secret: SecretStr | None = None  # default: generated once and stored in the DB

    # --- Idempotency ---------------------------------------------------------------------------
    idempotency_ttl_seconds: int = Field(default=86_400, ge=1)

    # --- Observability ------------------------------------------------------------------------
    metrics_enabled: bool = True
    otel_exporter_otlp_endpoint: str | None = None  # e.g. http://jaeger:4318 (enables tracing)
    otel_service_name: str = "ai-router"
    trace_include_content: bool = False
    static_dir: str = "/app/static"  # self-hosted Swagger UI / ReDoc assets (Docker image)

    # --- Logging --------------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_message_content: bool = False

    @field_validator(
        "llm_thinking",
        "llm_seed",
        "smtp_username",
        "smtp_password",
        "routing_policy_file",
        "oidc_issuer",
        "oidc_audience",
        "oidc_jwks_url",
        "admin_token",
        "feedback_secret",
        "otel_exporter_otlp_endpoint",
        mode="before",
    )
    @classmethod
    def _empty_string_is_none(cls, value: object) -> object:
        """Treat `VAR=` (empty value, e.g. from .env) as "not set"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ollama_base_url", "public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("ollama_keep_alive")
    @classmethod
    def _valid_keep_alive(cls, value: str) -> str:
        """A number of seconds (-1 = forever) or a Go duration such as "30m" or "1h30m"."""
        value = value.strip()
        if not _KEEP_ALIVE.fullmatch(value):
            raise ValueError(
                'OLLAMA_KEEP_ALIVE must be seconds ("-1", "300") or a duration ("30m")'
            )
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _cross_field_checks(self) -> Self:
        """Combinations that cannot work are rejected at startup (fail fast)."""
        if self.auth_mode == "api_key" and not self.api_keys.strip():
            raise ValueError("AUTH_MODE=api_key requires API_KEYS")
        if self.auth_mode == "oidc" and not (self.oidc_jwks_url and self.oidc_audience):
            raise ValueError("AUTH_MODE=oidc requires OIDC_JWKS_URL and OIDC_AUDIENCE")
        budget = self.ollama_context_length * 0.9
        needed = (
            _PROMPT_OVERHEAD_TOKENS
            + self.max_message_chars / _CHARS_PER_TOKEN_WORST_CASE
            + self.llm_max_tokens
        )
        if needed > budget:
            raise ValueError(
                f"MAX_MESSAGE_CHARS={self.max_message_chars} may not fit the model context "
                f"(~{int(needed)} tokens needed, "
                f"OLLAMA_CONTEXT_LENGTH={self.ollama_context_length}) - Ollama would silently "
                "truncate the prompt; lower the limit or enlarge the context"
            )
        return self

    def warnings(self) -> list[str]:
        """Suspicious but legal combinations, logged at startup."""
        result: list[str] = []
        if self.auth_mode == "none":
            result.append(
                "AUTH_MODE=none: anyone with network access can send e-mails to departments - "
                "do not expose the API beyond localhost"
            )
        if self.max_concurrent_agent_runs > self.ollama_num_parallel:
            result.append(
                "MAX_CONCURRENT_AGENT_RUNS > OLLAMA_NUM_PARALLEL: extra runs wait inside Ollama "
                "while their agent timeout is already running"
            )
        if self.agent_timeout_seconds < self.llm_request_timeout_seconds:
            result.append(
                "AGENT_TIMEOUT_SECONDS < LLM_REQUEST_TIMEOUT_SECONDS: the per-request LLM timeout "
                "can never trigger"
            )
        if self.rate_limit_per_minute == 0:
            result.append("RATE_LIMIT_PER_MINUTE=0: rate limiting disabled")
        return result


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton (cached after the first call)."""
    return Settings()
