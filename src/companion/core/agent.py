import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from typing import Any

from companion.models.base import ModelResponse
from companion.models.gateway import ModelGateway
from companion.tools.registry import ToolRegistry


class AgentError(Exception):
    pass


@dataclass
class AgentResult:
    response: ModelResponse
    messages: list[dict[str, Any]]


class Agent:
    def __init__(
        self,
        gateway: ModelGateway,
        tools: ToolRegistry,
        *,
        max_tool_rounds: int = 3,
        max_tool_calls: int = 8,
        model_timeout: float = 120.0,
    ):
        if type(max_tool_rounds) is not int or max_tool_rounds < 0:
            raise ValueError("max_tool_rounds must be a nonnegative integer")
        if type(max_tool_calls) is not int or max_tool_calls < 0:
            raise ValueError("max_tool_calls must be a nonnegative integer")
        if not math.isfinite(model_timeout) or model_timeout <= 0:
            raise ValueError("model_timeout must be finite and positive")
        self.gateway = gateway
        self.tools = tools
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_calls = max_tool_calls
        self.model_timeout = model_timeout

    async def run(self, role: str, messages: list[dict[str, Any]]) -> AgentResult:
        # Commit history only after a complete answer; failed turns leave the caller unchanged.
        history = deepcopy(messages)
        calls = 0
        for round_index in range(self.max_tool_rounds + 1):
            try:
                response = await asyncio.wait_for(
                    self.gateway.generate(role, history, tools=self.tools.schemas()),
                    timeout=self.model_timeout,
                )
            except TimeoutError as exc:
                raise AgentError("Model response timed out") from exc
            if not response.tool_calls:
                if not response.content.strip():
                    raise AgentError("Model returned an empty answer")
                history.append(response.as_message())
                return AgentResult(response=response, messages=history)
            if round_index == self.max_tool_rounds:
                raise AgentError("Tool round limit reached")
            if calls + len(response.tool_calls) > self.max_tool_calls:
                raise AgentError("Tool call limit reached")
            history.append(response.as_message())
            for call in response.tool_calls:
                result = await self.tools.execute(call)
                history.append({
                    "role": "tool",
                    "tool_name": call.name,
                    "content": json.dumps(result, ensure_ascii=False, allow_nan=False),
                })
                calls += 1
        raise AssertionError("Unreachable agent state")
