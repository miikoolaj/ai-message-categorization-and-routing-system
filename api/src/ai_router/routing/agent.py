"""The routing agent: an LLM that must forward the message by calling the `send_email` tool.

NOTE: do not add `from __future__ import annotations` to this module. Pydantic AI builds the
tool's JSON Schema from the real annotation objects when the tool is registered.
"""

import logging
from typing import Annotated

from pydantic import BeforeValidator, Field, WithJsonSchema
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from ai_router.config import Settings
from ai_router.mail.composer import compose_routed_email, sanitize_header
from ai_router.routing.departments import Department
from ai_router.routing.deps import RoutingDeps, SentEmail
from ai_router.routing.prompts import build_instructions

logger = logging.getLogger(__name__)

_ADDRESS_BY_LOCAL_PART = {
    department.value.split("@")[0]: department.value for department in Department
}


def normalize_department(value: object) -> object:
    """Lenient pre-validation of the department chosen by the model.

    Accepts `" HELP-DESK@EXAMPLE.COM "`, `"help-desk"` or `"HELP_DESK"`. Anything that still does
    not match an allowed address is left as is, fails validation, and Pydantic AI asks the model
    to correct itself (the error message lists the allowed values).
    """
    if not isinstance(value, str):
        return value
    candidate = value.strip().strip("\"'<>").lower()
    if "@" not in candidate:
        candidate = _ADDRESS_BY_LOCAL_PART.get(candidate.replace("_", "-"), candidate)
    return candidate


# Plain `enum` schema on purpose. Pydantic would describe an Enum with `$ref`/`$defs` (and
# `anyOf`/`const` with member docstrings); Ollama's tool-schema parser keeps neither `$ref` nor
# `const`, so the allowed values would silently disappear before reaching the model.
DEPARTMENT_JSON_SCHEMA = {"type": "string", "enum": [department.value for department in Department]}

DepartmentArg = Annotated[
    Department,
    BeforeValidator(normalize_department),
    WithJsonSchema(DEPARTMENT_JSON_SCHEMA),
]
ReasonArg = Annotated[str, Field(min_length=2, max_length=500)]
SubjectArg = Annotated[str, Field(min_length=2, max_length=200)]


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


def build_agent(settings: Settings, model: Model) -> Agent[RoutingDeps, str]:
    """Create the (stateless, reusable) routing agent."""
    agent = Agent(
        model,
        deps_type=RoutingDeps,
        output_type=str,
        instructions=build_instructions(),
        retries={"tools": settings.agent_tool_retries, "output": settings.agent_output_retries},
        model_settings=build_model_settings(settings),
        name="message-router",
    )

    # `sequential=True`: even if the model asks for several calls at once, they run one by one.
    @agent.tool(docstring_format="google", require_parameter_descriptions=True, sequential=True)
    async def send_email(
        ctx: RunContext[RoutingDeps],
        reason: ReasonArg,
        department: DepartmentArg,
        subject: SubjectArg,
    ) -> str:
        """Forward the user's message to the responsible department by e-mail. Call exactly once.

        Args:
            reason: One short sentence explaining why this department is responsible, written in
                the language of the user's message.
            department: E-mail address of the responsible department: human-resources (HR,
                people), help-desk (one user's IT problem), it (infrastructure, outages,
                security, system access), kadry (leave, payroll, contracts), other (fallback).
            subject: Short e-mail subject (max ~10 words) summarising the request, written in the
                language of the user's message.
        """
        deps = ctx.deps
        if deps.sent is not None or deps.send_in_progress:
            logger.warning("agent.tool.duplicate_call", extra={"department": department.value})
            return (
                "The message has already been forwarded. Do not call send_email again - "
                "reply with one short confirmation sentence."
            )
        deps.send_in_progress = True  # set before the first `await` -> atomic within asyncio
        deps.tool_attempts += 1
        logger.info(
            "agent.tool.called",
            extra={
                "tool": "send_email",
                "department": department.value,
                "attempt": deps.tool_attempts,
            },
        )
        try:
            clean_subject = sanitize_header(subject)
            clean_reason = sanitize_header(reason, max_length=300)
            if len(clean_subject) < 2 or len(clean_reason) < 2:
                raise ModelRetry(
                    "`subject` and `reason` must be short, non-empty single-line texts."
                )
            # Reply-To and the original text come from the request (deps), never from the LLM.
            message = compose_routed_email(
                sender_email=deps.sender_email,
                department=department,
                subject=clean_subject,
                reason=clean_reason,
                original_message=deps.original_message,
                request_id=deps.request_id,
                routed_by="agent",
                model_name=deps.model_name,
                mail_settings=deps.mail_settings,
            )
            await deps.mailer.send(message)  # EmailDeliveryError propagates and aborts the run
        finally:
            deps.send_in_progress = False

        deps.sent = SentEmail(
            department=department,
            to=department.value,
            reply_to=deps.sender_email,
            subject=str(message["Subject"]),
            reason=clean_reason,
            message_id=str(message["Message-ID"]),
            routed_by="agent",
        )
        return (
            f"E-mail sent to {department.value} (Message-ID {deps.sent.message_id}). "
            "Task complete - reply with one short confirmation sentence."
        )

    @agent.output_validator
    async def ensure_email_sent(ctx: RunContext[RoutingDeps], output: str) -> str:
        """The run may only finish after the e-mail was actually sent by the tool."""
        if ctx.partial_output:
            return output
        if ctx.deps.sent is None:
            logger.info("agent.retry", extra={"kind": "no_tool_call"})
            raise ModelRetry(
                "You have not forwarded the message yet. You MUST call the `send_email` tool "
                "exactly once. If no department clearly fits, use other@example.com."
            )
        return output

    return agent
