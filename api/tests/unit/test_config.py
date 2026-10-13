import pytest
from pydantic import ValidationError

from ai_router.config import Settings


def test_defaults_work_without_any_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OLLAMA_MODEL", "OLLAMA_BASE_URL", "SMTP_HOST", "SMTP_PORT"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings()
    assert settings.ollama_model == "qwen3:4b-instruct-2507-q4_K_M"
    assert settings.smtp_host == "mailpit"
    assert settings.routing_fallback_enabled is True
    assert settings.llm_thinking is None


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example:11434/")
    monkeypatch.setenv("LLM_THINKING", "false")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    settings = Settings()
    assert settings.ollama_base_url == "http://example:11434"
    assert settings.llm_thinking is False
    assert settings.log_level == "DEBUG"


def test_empty_values_mean_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_THINKING", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    settings = Settings()
    assert settings.llm_thinking is None
    assert settings.smtp_password is None


def test_invalid_values_fail_fast() -> None:
    with pytest.raises(ValidationError):
        Settings(agent_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(max_message_chars=10)
