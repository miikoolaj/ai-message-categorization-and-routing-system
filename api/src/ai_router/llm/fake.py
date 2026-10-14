"""Deterministic, rule-based stand-in for the LLM (`LLM_BACKEND=fake`).

Used for end-to-end tests in CI (no 2.5 GB model download, no flaky decisions) and for demos on
machines without enough RAM. It speaks the same protocol as a real model: it answers with a
`send_email` tool call chosen by keyword matching against the routing policy.
"""

import html
import re

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_router.routing.policy import RoutingPolicy

_USER_MESSAGE = re.compile(r"<user_message>\s*(.*?)\s*</user_message>", re.DOTALL)


def _user_text(messages: list[ModelMessage]) -> str:
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    found = _USER_MESSAGE.search(part.content)
                    return html.unescape(found.group(1) if found else part.content)
    return ""


def build_fake_model(policy: RoutingPolicy) -> FunctionModel:
    rules = [
        (department.address, [keyword.lower() for keyword in department.keywords_pl])
        for department in policy.departments
        if not department.fallback
    ]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(
            isinstance(p, ToolReturnPart) for p in last.parts
        ):
            return ModelResponse(parts=[TextPart("Message forwarded (fake LLM backend).")])
        text = _user_text(messages)
        lowered = text.lower()
        department, confidence = policy.fallback.address, 0.3
        for address, keywords in rules:
            if any(keyword in lowered for keyword in keywords):
                department, confidence = address, 0.9
                break
        tools = [*info.output_tools, *info.function_tools]
        subject = " ".join(text.split()[:8]) or "Zgłoszenie"
        args = {
            "reason": "Dopasowanie słów kluczowych (fałszywy backend LLM).",
            "department": department,
            "confidence": confidence,
            "subject": subject,
        }
        return ModelResponse(parts=[ToolCallPart(tools[0].name, args)])

    return FunctionModel(respond, model_name="fake-rules")
