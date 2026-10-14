import pytest
from pydantic import ValidationError

from ai_router.config import Settings


def test_defaults_work_without_any_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OLLAMA_MODEL", "OLLAMA_BASE_URL", "SMTP_HOST", "SMTP_PORT", "AUTH_MODE"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    assert settings.ollama_model == "qwen3:4b-instruct-2507-q4_K_M"
    assert settings.auth_mode == "none"
    assert settings.agent_mode == "tool"
    assert settings.max_concurrent_agent_runs == settings.ollama_num_parallel == 1


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example:11434/")
    monkeypatch.setenv("LLM_THINKING", "false")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("AGENT_MODE", "output_function")
    settings = Settings()
    assert settings.ollama_base_url == "http://example:11434"
    assert settings.llm_thinking is False
    assert settings.log_level == "DEBUG"
    assert settings.agent_mode == "output_function"


def test_empty_values_mean_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LLM_THINKING",
        "SMTP_PASSWORD",
        "ROUTING_POLICY_FILE",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.setenv(var, "")
    settings = Settings()
    assert settings.llm_thinking is None
    assert settings.smtp_password is None
    assert settings.routing_policy_file is None
    assert settings.otel_exporter_otlp_endpoint is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_timeout_seconds": 0},
        {"max_message_chars": 10},
        {"auth_mode": "api_key", "api_keys": ""},
        {"auth_mode": "oidc", "oidc_jwks_url": None},
        {"max_message_chars": 16000, "ollama_context_length": 4096},
        {"ollama_keep_alive": "forever"},
    ],
    ids=[
        "timeout",
        "min-chars",
        "api-key-without-keys",
        "oidc-without-jwks",
        "context-too-small",
        "keep-alive-format",
    ],
)
def test_invalid_values_fail_fast(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)  # type: ignore[arg-type]


def test_suspicious_combinations_produce_warnings() -> None:
    warnings = Settings(
        auth_mode="none",
        max_concurrent_agent_runs=4,
        agent_timeout_seconds=60,
        rate_limit_per_minute=0,
    ).warnings()
    assert any("AUTH_MODE=none" in w for w in warnings)
    assert any("OLLAMA_NUM_PARALLEL" in w for w in warnings)
    assert any("LLM_REQUEST_TIMEOUT" in w for w in warnings)
    assert any("rate limiting disabled" in w for w in warnings)


@pytest.mark.parametrize("value", ["-1", "0", "300", "30m", "1h30m", "24h"])
def test_keep_alive_accepts_seconds_and_durations(value: str) -> None:
    assert Settings(ollama_keep_alive=value).ollama_keep_alive == value
