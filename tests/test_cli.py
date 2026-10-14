import asyncio

from companion import __main__ as cli
from companion.core.agent import Agent
from companion.models.base import ModelResponse, ToolCall
from companion.tools.registry import Tool, ToolArguments, ToolRegistry


def test_cli_routes_tool_turns_and_keeps_successful_history(monkeypatch, capsys):
    requests = []
    responses = iter([
        ModelResponse("", "test", [ToolCall("status", {})]),
        ModelResponse("Memory is 42%", "test"),
        ConnectionError("backend unavailable"),
        ModelResponse("Recovered", "test"),
    ])

    class Gateway:
        def __init__(self, _):
            pass

        async def generate(self, role, messages, tools=None):
            requests.append((role, list(messages)))
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

    tools = ToolRegistry()

    async def status(_):
        return {"memory_percent": 42}

    tools.register(Tool("status", "Read status", ToolArguments, status))
    inputs = iter(["/system", "Memory?", "/coding", "failed turn", "/system", "retry", "/exit"])
    monkeypatch.setattr(cli, "ModelGateway", Gateway)
    monkeypatch.setattr(cli, "build_agents", lambda config, gateway: {role: Agent(gateway, tools) for role in config.agents})
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    asyncio.run(cli.main())
    output = capsys.readouterr().out
    assert "Memory is 42%" in output
    assert "Agent error: backend unavailable" in output
    assert "Recovered" in output
    assert [request[0] for request in requests] == ["system", "system", "coding", "system"]
    retry_history = requests[-1][1]
    assert any(message["role"] == "tool" for message in retry_history)
    assert not any(message.get("content") == "failed turn" for message in retry_history)


def test_cli_exits_cleanly_on_eof(monkeypatch):
    def eof(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    asyncio.run(cli.main())
