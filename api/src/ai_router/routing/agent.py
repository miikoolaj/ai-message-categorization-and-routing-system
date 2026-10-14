"""The routing agent: an LLM that must forward the message by calling the `send_email` tool.

Two modes (`AGENT_MODE`):

* `tool` (default) - `send_email` is a regular function tool; an output validator makes sure the
  run cannot finish without it. Two model rounds (tool call + short confirmation).
* `output_function` - `send_email` is the agent's output tool: calling it ends the run, so one
  model round is enough. Still function calling, measured against `tool` in the evaluation.

NOTE: do not add `from __future__ import annotations` to this module. Pydantic AI builds the
tool's JSON Schema from the real annotation objects (some are created inside `build_agent`).
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BeforeValidator, Field, WithJsonSchema
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from ai_router.config import Settings
from ai_router.logging_config import log_event
from ai_router.mail.composer import compose_routed_email, sanitize_header
from ai_router.observability import metrics
from ai_router.routing.deps import RoutingDeps, SentEmail
from ai_router.routing.policy import RoutingPolicy
from ai_router.routing.prompts import (
    CONFIDENCE_RUBRIC,
    LANGUAGE_PHRASES,
    SubjectLanguage,
    build_instructions,
    prompt_version,
)

logger = logging.getLogger(__name__)

AgentMode = Literal["tool", "output_function"]
TOOL_NAME = "send_email"
TOOL_DESCRIPTION = (
    "Forward the user's message to the responsible department by e-mail. Call exactly once."
)


def parameter_descriptions(subject_language: SubjectLanguage) -> dict[str, str]:
    """Descriptions of the tool's parameters - the text the model reads while filling them in.

    The language requirement is repeated here: as a rule in the instructions alone it was ignored
    in 74% of `reason` texts for Polish messages (evaluation 2026-10-13).
    """
    language = LANGUAGE_PHRASES[subject_language]
    return {
        "reason": f"One short sentence {language} explaining why this department is responsible.",
        "department": "E-mail address of the responsible department.",
        "confidence": f"Certainty that this department is right: {CONFIDENCE_RUBRIC}.",
        "subject": f"Short e-mail subject {language} (max ~10 words) summarising the request.",
    }


def tool_docstring(subject_language: SubjectLanguage) -> str:
    """Google-style docstring of `send_email`; Pydantic AI turns it into the tool schema."""
    args = "\n".join(
        f"    {name}: {text}" for name, text in parameter_descriptions(subject_language).items()
    )
    return f"{TOOL_DESCRIPTION}\n\nArgs:\n{args}\n"


@dataclass(frozen=True)
class RoutingAgent:
    agent: Agent[RoutingDeps, str]
    mode: AgentMode
    instructions: str
    prompt_version: str


def normalize_confidence(value: object) -> object:
    """Accept `"0.8"`, `0.8` and percentages like `80`; anything else is left for validation."""
    if isinstance(value, str):
        try:
            value = float(value.strip().rstrip("%"))
        except ValueError:
            return value
    if isinstance(value, int | float) and 1 < value <= 100:
        return value / 100
    return value


ReasonArg = Annotated[str, Field(min_length=2, max_length=500)]
SubjectArg = Annotated[str, Field(min_length=2, max_length=200)]
ConfidenceArg = Annotated[float, BeforeValidator(normalize_confidence), Field(ge=0.0, le=1.0)]


def build_model_settings(settings: Settings) -> ModelSettings:
    model_settings = ModelSettings(
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        parallel_tool_calls=False,
        timeout=settings.llm_request_timeout_seconds,
    )
    if settings.llm_seed is not None:
        model_settings["seed"] = settings.llm_seed
    if settings.llm_thinking is not None:
        # Only sent when explicitly configured - models without "thinking" may reject the field.
        model_settings["thinking"] = settings.llm_thinking
    return model_settings


def tool_definition(
    policy: RoutingPolicy, subject_language: SubjectLanguage = "pl"
) -> dict[str, Any]:
    """What the model is told about the tool (part of the prompt version)."""
    return {
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "parameters": parameter_descriptions(subject_language),
        "departments": policy.json_schema(),
    }


async def forward_message(
    deps: RoutingDeps, *, reason: str, department: str, confidence: float, subject: str
) -> str:
    """Body of the `send_email` tool (shared by both agent modes)."""
    if deps.sent is not None or deps.submission is not None:
        deps.tool_duplicates += 1
        metrics.TOOL_DUPLICATES.inc()
        log_event(logger, logging.WARNING, "agent.tool.duplicate_call", department=department)
        return (
            "The message has already been forwarded. Do not call send_email again - "
            "reply with one short confirmation sentence."
        )
    deps.tool_attempts += 1
    clean_subject = sanitize_header(subject)
    clean_reason = sanitize_header(reason, max_length=300)
    if len(clean_subject) < 2 or len(clean_reason) < 2:
        raise ModelRetry("`subject` and `reason` must be short, non-empty single-line texts.")

    runtime = deps.runtime
    chosen = runtime.policy.require(department)
    needs_review = confidence < runtime.confidence_threshold
    redirect = needs_review and runtime.low_confidence_action == "other"
    target = runtime.policy.fallback if redirect else chosen
    mail_reason = clean_reason
    if target.address != chosen.address:
        mail_reason = (
            f"Model wskazał {chosen.address} z niską pewnością ({confidence:.2f}): {clean_reason}"
        )
    if needs_review:
        metrics.LOW_CONFIDENCE.inc()
    log_event(
        logger,
        logging.INFO,
        "agent.tool.called",
        tool=TOOL_NAME,
        department=chosen.address,
        confidence=confidence,
        needs_review=needs_review,
        attempt=deps.tool_attempts,
    )

    # Reply-To, the original text and the ticket come from the request, never from the LLM.
    message = compose_routed_email(
        sender_email=deps.sender_email,
        sender_verification=deps.sender_verification,
        department=target.address,
        subject=clean_subject,
        reason=mail_reason,
        original_message=deps.original_message,
        request_id=deps.request_id,
        ticket_code=deps.ticket_code,
        routed_by="agent",
        model_name=runtime.model_name,
        prompt_version=runtime.prompt_version,
        mail_settings=runtime.mail_settings,
        confidence=confidence,
        needs_review=needs_review,
        feedback_url=deps.feedback_url,
    )
    record = SentEmail(
        ticket_id=deps.ticket_id,
        department=target.address,
        chosen_department=chosen.address,
        reply_to=deps.sender_email,
        subject=str(message["Subject"]),
        reason=mail_reason,
        message_id=str(message["Message-ID"]),
        routed_by="agent",
        confidence=confidence,
        needs_review=needs_review,
        delivery="queued",
    )
    submission = asyncio.ensure_future(deps.gateway.submit(message, record))
    deps.submission = submission
    try:
        # shield: an agent timeout cancels the *waiting*, never the hand-over itself.
        delivery = await asyncio.shield(submission)
    except asyncio.CancelledError:
        raise
    except Exception:
        deps.submission = None  # nothing was accepted (e.g. database unavailable)
        raise
    deps.sent = replace(record, delivery=delivery)
    return (
        f"E-mail accepted for delivery to {target.address} (ticket {deps.ticket_code}). "
        "Task complete - reply with one short confirmation sentence."
    )


def build_agent(settings: Settings, model: Model, policy: RoutingPolicy) -> RoutingAgent:
    """Create the (stateless, reusable) routing agent for the given policy."""
    instructions = build_instructions(policy, settings.mail_subject_language)
    department_arg = Annotated[
        str,
        BeforeValidator(policy.normalize),
        AfterValidator(policy.validate_address),
        # Flat enum on purpose: Ollama's tool-schema parser drops `$ref` and `const`.
        WithJsonSchema(policy.json_schema()),
    ]

    async def send_email(
        ctx: RunContext[RoutingDeps],
        reason: ReasonArg,
        department: department_arg,
        confidence: ConfidenceArg,
        subject: SubjectArg,
    ) -> str:
        return await forward_message(
            ctx.deps, reason=reason, department=department, confidence=confidence, subject=subject
        )

    # The docstring (tool description + parameter descriptions) depends on MAIL_SUBJECT_LANGUAGE.
    send_email.__doc__ = tool_docstring(settings.mail_subject_language)

    common: dict[str, Any] = {
        "deps_type": RoutingDeps,
        "instructions": instructions,
        "retries": {"tools": settings.agent_tool_retries, "output": settings.agent_output_retries},
        "model_settings": build_model_settings(settings),
        "name": "message-router",
    }
    agent: Agent[RoutingDeps, str]
    if settings.agent_mode == "output_function":
        agent = Agent(
            model,
            output_type=ToolOutput(
                send_email,
                name=TOOL_NAME,
                description=TOOL_DESCRIPTION,
                max_retries=settings.agent_output_retries,
            ),
            **common,
        )
    else:
        agent = Agent(model, output_type=str, **common)
        # `sequential=True`: even if the model asks for several calls at once, they run one by one.
        agent.tool(
            name=TOOL_NAME,
            docstring_format="google",
            require_parameter_descriptions=True,
            sequential=True,
        )(send_email)

        @agent.output_validator
        async def ensure_email_sent(ctx: RunContext[RoutingDeps], output: str) -> str:
            """The run may only finish after the e-mail was accepted by the gateway."""
            if ctx.partial_output:
                return output
            if ctx.deps.sent is None:
                log_event(logger, logging.INFO, "agent.retry", kind="no_tool_call")
                raise ModelRetry(
                    "You have not forwarded the message yet. You MUST call the `send_email` tool "
                    f"exactly once. If no department clearly fits, use {policy.fallback.address}."
                )
            return output

    version = prompt_version(
        instructions=instructions,
        tool=tool_definition(policy, settings.mail_subject_language),
        agent_mode=settings.agent_mode,
    )
    return RoutingAgent(
        agent=agent, mode=settings.agent_mode, instructions=instructions, prompt_version=version
    )
