"""Factory of the Pydantic AI model used by the agent (Ollama or the rule-based fake)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.profiles import merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.ollama import OllamaProvider

from ai_router.config import Settings
from ai_router.llm.fake import build_fake_model
from ai_router.routing.policy import RoutingPolicy

# Ollama's OpenAI-compatible endpoint understands `max_tokens` (mapped to `num_predict`) but
# silently ignores the newer `max_completion_tokens` that Pydantic AI sends by default. Without
# this override the `LLM_MAX_TOKENS` limit would simply not exist (verified in a contract test).
OLLAMA_PROFILE_OVERRIDES = OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False)
# Pydantic AI forwards the unified `thinking` setting (sent as `reasoning_effort`) only when the
# profile declares support, and its bundled Qwen profile does not. Without this, LLM_THINKING=false
# was silently dropped: hybrid models such as qwen3.5 kept "thinking" until the token limit and
# never called the tool (found in the model comparison, 2026-10-14).
THINKING_PROFILE_OVERRIDES = OpenAIModelProfile(supports_thinking=True)


async def _noop() -> None:
    return None


@dataclass(frozen=True)
class ModelHandle:
    model: Model
    name: str
    close: Callable[[], Awaitable[None]]


def build_ollama_model(settings: Settings) -> ModelHandle:
    """Model using Ollama's OpenAI-compatible Chat Completions API (`/v1/chat/completions`).

    * The base URL is passed explicitly: `OllamaProvider` would otherwise read the
      `OLLAMA_BASE_URL` environment variable, which in this project points at the server root
      (without `/v1`) because the native API is used for health checks and warm-up.
    * The OpenAI SDK retries failed requests twice by default. One retry is enough here: a
      request that failed at temperature 0 would most likely fail the same way again, and the
      agent has its own retry/fallback logic.
    """
    client = AsyncOpenAI(
        base_url=f"{settings.ollama_base_url}/v1",
        api_key="ollama",  # Ollama does not check it, but the SDK requires a non-empty value
        max_retries=1,
        timeout=settings.llm_request_timeout_seconds,
    )
    provider = OllamaProvider(openai_client=client)
    profile = merge_profile(provider.model_profile(settings.ollama_model), OLLAMA_PROFILE_OVERRIDES)
    if settings.llm_thinking is not None:
        profile = merge_profile(profile, THINKING_PROFILE_OVERRIDES)
    model = OllamaModel(settings.ollama_model, provider=provider, profile=profile)
    return ModelHandle(model=model, name=settings.ollama_model, close=client.close)


def build_model(settings: Settings, policy: RoutingPolicy) -> ModelHandle:
    if settings.llm_backend == "fake":
        return ModelHandle(model=build_fake_model(policy), name="fake-rules", close=_noop)
    return build_ollama_model(settings)
