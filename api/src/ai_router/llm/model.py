"""Factory of the Pydantic AI model object talking to Ollama."""

from openai import AsyncOpenAI
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.profiles import merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.ollama import OllamaProvider

from ai_router.config import Settings

# Ollama's OpenAI-compatible endpoint understands `max_tokens` (mapped to `num_predict`) but
# silently ignores the newer `max_completion_tokens` that Pydantic AI sends by default. Without
# this override the `LLM_MAX_TOKENS` limit would simply not exist.
OLLAMA_PROFILE_OVERRIDES = OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False)


def build_model(settings: Settings) -> OllamaModel:
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
    return OllamaModel(settings.ollama_model, provider=provider, profile=profile)
