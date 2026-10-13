"""A scripted stand-in for the LLM, built on Pydantic AI's FunctionModel."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

Responder = Callable[[list[ModelMessage], AgentInfo], ModelResponse | Awaitable[ModelResponse]]
Step = ModelResponse | Responder | BaseException


class ScriptedModel:
    """Returns the given steps in order - one step per request the agent sends to the "model".

    A step can be a `ModelResponse`, a callable producing one (sync or async), or an exception
    to raise (e.g. to simulate an unreachable LLM).
    """

    def __init__(self, *steps: Step) -> None:
        self._steps = list(steps)
        self.requests: list[list[ModelMessage]] = []
        self.infos: list[AgentInfo] = []
        self.model = FunctionModel(self._respond, model_name="scripted-model")

    async def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(list(messages))
        self.infos.append(info)
        if not self._steps:
            raise AssertionError("ScriptedModel: the agent sent more requests than scripted")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, ModelResponse):
            return step
        result = step(messages, info)
        return await result if inspect.isawaitable(result) else result

    @property
    def remaining_steps(self) -> int:
        return len(self._steps)

    def retry_prompts(self) -> list[RetryPromptPart]:
        """Retry prompts the agent sent back to the model (validation errors, ModelRetry)."""
        last = self.requests[-1] if self.requests else []
        return [
            part
            for message in last
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, RetryPromptPart)
        ]

    def tool_schema(self, name: str = "send_email") -> dict[str, Any]:
        for tool in self.infos[0].function_tools:
            if tool.name == name:
                return tool.parameters_json_schema
        raise AssertionError(f"tool {name!r} was not offered to the model")


def tool_call(
    department: str = "help-desk@example.com",
    subject: str = "Komputer nie działa",
    reason: str = "Awaria sprzętu jednego użytkownika",
) -> ModelResponse:
    args = {"reason": reason, "department": department, "subject": subject}
    return ModelResponse(parts=[ToolCallPart("send_email", args)])


def parallel_tool_calls(*departments: str) -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart("send_email", {"reason": "Test", "department": d, "subject": "Test"})
            for d in departments
        ]
    )


def text(content: str = "Message forwarded.") -> ModelResponse:
    return ModelResponse(parts=[TextPart(content)])


def slow(response: ModelResponse, seconds: float) -> Responder:
    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(seconds)
        return response

    return respond
