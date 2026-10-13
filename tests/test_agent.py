import asyncio
from copy import deepcopy
import json

import pytest

from companion.core.agent import Agent, AgentError
from companion.models.base import ModelResponse, ToolCall
from companion.tools.registry import Tool, ToolArguments, ToolRegistry


class ScriptedGateway:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def generate(self, role, messages, tools=None):
        self.requests.append((role, deepcopy(messages), deepcopy(tools)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def registry_with_counter():
    registry = ToolRegistry()
    executions = []

    async def handler(_):
        executions.append(True)
        return {"memory_percent": 42}

    registry.register(Tool("status", "Read status", ToolArguments, handler))
    return registry, executions


def test_tool_result_is_returned_to_model_and_history_is_preserved():
    registry, executions = registry_with_counter()
    gateway = ScriptedGateway([
        ModelResponse("", "test", [ToolCall("status", {})], thinking="Need a measurement"),
        ModelResponse("Memory usage is 42%", "test"),
    ])
    messages = [{"role": "user", "content": "Memory usage?"}]
    result = asyncio.run(Agent(gateway, registry).run("primary", messages))
    assert result.response.content == "Memory usage is 42%"
    assert len(executions) == 1
    assert messages == [{"role": "user", "content": "Memory usage?"}]
    second_request = gateway.requests[1][1]
    assert second_request[1]["thinking"] == "Need a measurement"
    assert second_request[1]["tool_calls"][0]["function"]["name"] == "status"
    assert second_request[2]["tool_name"] == "status"
    assert json.loads(second_request[2]["content"]) == {
        "ok": True, "result": {"memory_percent": 42},
    }
    assert result.messages[-1] == {"role": "assistant", "content": "Memory usage is 42%"}


def test_multiple_calls_and_tool_errors_are_returned_in_order():
    registry, executions = registry_with_counter()
    gateway = ScriptedGateway([
        ModelResponse("", "test", [
            ToolCall("status", {}),
            ToolCall("missing", {}),
            ToolCall("status", {"unexpected": True}),
        ]),
        ModelResponse("One measurement succeeded; two tools failed", "test"),
    ])
    result = asyncio.run(Agent(gateway, registry).run("coding", []))
    outputs = [json.loads(message["content"]) for message in result.messages if message["role"] == "tool"]
    assert outputs[0]["ok"] is True
    assert outputs[1]["error"]["code"] == "unknown_tool"
    assert outputs[2]["error"]["code"] == "invalid_arguments"
    assert len(executions) == 1
    assert all(request[0] == "coding" for request in gateway.requests)


def test_plain_chat_needs_no_tool_execution():
    registry, executions = registry_with_counter()
    gateway = ScriptedGateway([ModelResponse("Hello", "test")])
    result = asyncio.run(Agent(gateway, registry).run("primary", []))
    assert result.response.content == "Hello"
    assert not executions


def test_repeated_tool_calls_stop_at_round_limit():
    registry, executions = registry_with_counter()
    call = ModelResponse("", "test", [ToolCall("status", {})])
    gateway = ScriptedGateway([call, call, call])
    with pytest.raises(AgentError, match="round limit"):
        asyncio.run(Agent(gateway, registry, max_tool_rounds=2).run("primary", []))
    assert len(executions) == 2
    assert len(gateway.requests) == 3


def test_oversized_tool_batch_is_rejected_before_execution():
    registry, executions = registry_with_counter()
    gateway = ScriptedGateway([ModelResponse("", "test", [ToolCall("status", {})] * 3)])
    with pytest.raises(AgentError, match="call limit"):
        asyncio.run(Agent(gateway, registry, max_tool_calls=2).run("primary", []))
    assert not executions


def test_tool_budget_accumulates_across_rounds():
    registry, executions = registry_with_counter()
    call = ModelResponse("", "test", [ToolCall("status", {})])
    gateway = ScriptedGateway([call, call])
    with pytest.raises(AgentError, match="call limit"):
        asyncio.run(Agent(gateway, registry, max_tool_calls=1).run("primary", []))
    assert len(executions) == 1


def test_failure_after_tool_execution_does_not_change_caller_history():
    registry, _ = registry_with_counter()
    gateway = ScriptedGateway([
        ModelResponse("", "test", [ToolCall("status", {})]),
        ConnectionError("backend unavailable"),
    ])
    messages = [{"role": "user", "content": "Status?"}]
    original = deepcopy(messages)
    with pytest.raises(ConnectionError):
        asyncio.run(Agent(gateway, registry).run("primary", messages))
    assert messages == original


def test_model_timeout():
    class SlowGateway:
        async def generate(self, *args, **kwargs):
            await asyncio.Event().wait()

    with pytest.raises(AgentError, match="timed out"):
        asyncio.run(Agent(SlowGateway(), ToolRegistry(), model_timeout=0.01).run("primary", []))


def test_empty_final_answer_is_an_error():
    gateway = ScriptedGateway([ModelResponse("  ", "test", thinking="Still thinking")])
    with pytest.raises(AgentError, match="empty answer"):
        asyncio.run(Agent(gateway, ToolRegistry()).run("primary", []))


@pytest.mark.parametrize("options", [
    {"max_tool_rounds": -1}, {"max_tool_rounds": 1.5},
    {"max_tool_calls": -1}, {"max_tool_calls": True},
    {"model_timeout": 0}, {"model_timeout": float("inf")},
])
def test_invalid_limits_are_rejected(options):
    with pytest.raises(ValueError):
        Agent(ScriptedGateway([]), ToolRegistry(), **options)
