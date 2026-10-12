import asyncio

from pydantic import Field
import pytest

from companion.models.base import ToolCall
from companion.tools.registry import Tool, ToolArguments, ToolRegistry
from companion.tools.system import default_tools


class CountArguments(ToolArguments):
    count: int = Field(ge=1, le=3)


@pytest.mark.parametrize("arguments", [{"count": "2"}, {"count": 0}, {"count": 4}, {"count": 2, "extra": True}])
def test_invalid_arguments_never_reach_the_handler(arguments):
    registry = ToolRegistry()
    executions = []

    async def handler(args):
        executions.append(args)

    registry.register(Tool("count", "Count", CountArguments, handler))
    result = asyncio.run(registry.execute(ToolCall("count", arguments)))
    assert result["error"]["code"] == "invalid_arguments"
    assert not executions
    parameters = registry.schemas()[0]["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["count"]


def test_tool_timeout_is_reported():
    async def wait_forever(_):
        await asyncio.Event().wait()

    registry = ToolRegistry()
    registry.register(Tool("wait", "Wait", ToolArguments, wait_forever, timeout=0.01))
    result = asyncio.run(registry.execute(ToolCall("wait", {})))
    assert result["error"]["code"] == "tool_timeout"


def test_handler_error_is_reported_without_exception_details():
    async def broken(_):
        raise RuntimeError("private implementation details")

    registry = ToolRegistry()
    registry.register(Tool("broken", "Broken", ToolArguments, broken))
    result = asyncio.run(registry.execute(ToolCall("broken", {})))
    assert result["error"]["code"] == "tool_failed"
    assert "private implementation details" not in str(result)


def test_non_json_tool_output_is_reported_as_failure():
    async def bad_output(_):
        return {"value": float("nan")}

    registry = ToolRegistry()
    registry.register(Tool("bad", "Bad", ToolArguments, bad_output))
    result = asyncio.run(registry.execute(ToolCall("bad", {})))
    assert result["error"]["code"] == "tool_failed"


def test_duplicate_tool_registration_is_rejected():
    async def handler(_):
        return {}

    tool = Tool("same", "Same", ToolArguments, handler)
    registry = ToolRegistry()
    registry.register(tool)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_tool_timeout_is_rejected(timeout):
    async def handler(_):
        return {}

    with pytest.raises(ValueError, match="timeout"):
        ToolRegistry().register(Tool("bad", "Bad", ToolArguments, handler, timeout=timeout))


def test_system_status_reads_real_machine_metrics():
    result = asyncio.run(default_tools().execute(ToolCall("get_system_status", {})))
    assert result["ok"] is True
    status = result["result"]
    assert status["os"]
    assert 0 <= status["cpu_percent"] <= 100
    assert status["memory"]["total_bytes"] > 0
    assert 0 <= status["memory"]["available_bytes"] <= status["memory"]["total_bytes"]
    assert 0 <= status["memory"]["percent"] <= 100
