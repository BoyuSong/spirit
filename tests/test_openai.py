import asyncio
from copy import deepcopy
import json

import httpx
import pytest
import yaml

from companion.core.agent import Agent
from companion.models.base import ModelRequest, ModelResponse
from companion.models.gateway import ModelGateway
from companion.models.providers.openai import OpenAIProvider, responses_input
from companion.models.registry import ModelRegistry
from companion.tools.registry import Tool, ToolArguments, ToolRegistry


def completed(output):
    return {"status": "completed", "model": "test-model", "output": output}


def text_output(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def function_call(call_id="call_1", name="status", arguments="{}"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def test_remote_chat_request_and_key_rotation(monkeypatch):
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(200, json=completed([text_output("Remote answer")]))

    provider = OpenAIProvider("test-model", transport=httpx.MockTransport(endpoint))

    async def check():
        monkeypatch.setenv("CODING_API_KEY", "test-key-one")
        response = await provider.generate(ModelRequest([{"role": "user", "content": "Help with coding"}]))
        assert response.content == "Remote answer"
        monkeypatch.setenv("CODING_API_KEY", "test-key-two")
        await provider.generate(ModelRequest([]))

    asyncio.run(check())
    assert requests[0].url == "https://api.openai.com/v1/responses"
    assert requests[0].headers["Authorization"] == "Bearer test-key-one"
    assert requests[1].headers["Authorization"] == "Bearer test-key-two"
    body = json.loads(requests[0].content)
    assert body["model"] == "test-model"
    assert body["input"] == [{"role": "user", "content": "Help with coding"}]
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert "temperature" not in body
    assert "tools" not in body


def test_missing_key_fails_before_network_request(monkeypatch):
    monkeypatch.delenv("CODING_API_KEY", raising=False)

    def endpoint(_):
        pytest.fail("Missing credentials must not make a network request")

    provider = OpenAIProvider("test-model", transport=httpx.MockTransport(endpoint))
    with pytest.raises(ValueError, match="CODING_API_KEY"):
        asyncio.run(provider.generate(ModelRequest([])))


def test_openai_agent_tool_round_trip_preserves_reasoning_and_call_ids(monkeypatch):
    monkeypatch.setenv("CODING_API_KEY", "test-key")
    requests = []
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "test-encrypted"}

    def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["tools"][0]["name"] == "status"
            assert body["tools"][0]["strict"] is False
            return httpx.Response(200, json=completed([reasoning, function_call("call_1"), function_call("call_2")]))
        assert body["input"][1] == reasoning
        outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
        assert [item["call_id"] for item in outputs] == ["call_1", "call_2"]
        assert all(json.loads(item["output"])["result"] == {"memory_percent": 42} for item in outputs)
        return httpx.Response(200, json=completed([text_output("Memory usage is 42%")]))

    provider = OpenAIProvider("test-model", transport=httpx.MockTransport(endpoint))
    registry = ToolRegistry()

    async def status(_):
        return {"memory_percent": 42}

    registry.register(Tool("status", "Read status", ToolArguments, status))

    class Gateway:
        async def generate(self, role, messages, tools=None):
            assert role == "coding"
            return await provider.generate(ModelRequest(messages=messages, tools=tools or []))

    history = [{"role": "user", "content": "Memory?"}]
    original = deepcopy(history)
    result = asyncio.run(Agent(Gateway(), registry).run("coding", history))
    assert result.response.content == "Memory usage is 42%"
    assert history == original
    assert len(requests) == 2
    assert [message["tool_call_id"] for message in result.messages if message["role"] == "tool"] == ["call_1", "call_2"]


def test_old_ollama_history_converts_without_private_thinking():
    history = [
        {"role": "system", "content": "Assistant"},
        {"role": "user", "content": "Status?"},
        {"role": "assistant", "content": "", "thinking": "Ollama thought", "tool_calls": [
            {"function": {"name": "status", "arguments": {}}},
            {"function": {"name": "status", "arguments": {}}},
        ]},
        {"role": "tool", "tool_name": "status", "content": "first"},
        {"role": "tool", "tool_name": "status", "content": "second"},
        {"role": "assistant", "content": "Done"},
    ]
    original = deepcopy(history)
    items = responses_input(history)
    calls = [item for item in items if item.get("type") == "function_call"]
    outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert len({call["call_id"] for call in calls}) == 2
    assert [item["call_id"] for item in calls] == [item["call_id"] for item in outputs]
    assert [item["output"] for item in outputs] == ["first", "second"]
    assert "Ollama thought" not in json.dumps(items)
    assert history == original


@pytest.mark.parametrize("messages", [
    [{"role": "tool", "tool_name": "status", "content": "orphan"}],
    [{"role": "assistant", "tool_calls": [{"function": {"name": "status", "arguments": {}}}]}],
])
def test_incomplete_tool_history_is_rejected(messages):
    with pytest.raises(ValueError, match="history"):
        responses_input(messages)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_http_errors_do_not_print_body_or_secret(monkeypatch, status):
    monkeypatch.setenv("CODING_API_KEY", "test-sensitive-key")
    provider = OpenAIProvider("test-model", transport=httpx.MockTransport(
        lambda _: httpx.Response(status, json={"error": "test-sensitive-key"}),
    ))
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        asyncio.run(provider.generate(ModelRequest([])))
    assert "test-sensitive-key" not in str(error.value)


@pytest.mark.parametrize("data", [
    [], {}, {"output": "bad"}, {"output": [function_call(arguments="not-json")]},
    {"output": [function_call(arguments="[]")]},
    {"output": [function_call(call_id=42)]},
    {"output": [function_call(), function_call()]},
    {"status": "incomplete", "output": [function_call()]},
    {"error": {"message": "failure"}, "output": []},
])
def test_bad_responses_cannot_start_tools(monkeypatch, data):
    monkeypatch.setenv("CODING_API_KEY", "test-key")
    provider = OpenAIProvider("test-model", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    with pytest.raises(ValueError, match="invalid or incomplete"):
        asyncio.run(provider.generate(ModelRequest([])))


def test_refusal_is_a_nonempty_answer(monkeypatch):
    monkeypatch.setenv("CODING_API_KEY", "test-key")
    data = completed([{"type": "message", "content": [{"type": "refusal", "refusal": "Cannot help"}]}])
    provider = OpenAIProvider("test-model", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    assert asyncio.run(provider.generate(ModelRequest([]))).content == "Cannot help"


def test_gateway_routes_coding_remote_and_keeps_chat_local(tmp_path, monkeypatch):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump({"models": {
        "primary": {"provider": "ollama", "model": "qwen3:8b"},
        "coding": {"provider": "openai", "model": "gpt-6.1-sol", "api_key_env": "CODING_API_KEY"},
        "other": {"provider": "openai", "model": "gpt-6.1-sol", "api_key_env": "OTHER_KEY"},
    }}))
    calls = []

    class Local:
        def __init__(self, model, host):
            self.model = model

        async def generate(self, request):
            calls.append(("local", self.model))
            return ModelResponse("local", self.model)

    class Remote:
        def __init__(self, model, base_url, api_key_env):
            self.model = model

        async def generate(self, request):
            calls.append(("remote", self.model))
            return ModelResponse("remote", self.model)

    monkeypatch.setattr("companion.models.gateway.OllamaProvider", Local)
    monkeypatch.setattr("companion.models.gateway.OpenAIProvider", Remote)
    gateway = ModelGateway(ModelRegistry(path))

    async def check():
        await gateway.generate("primary", [])
        await gateway.generate("coding", [])
        await gateway.generate("coding", [])
        await gateway.generate("other", [])

    asyncio.run(check())
    assert calls == [("local", "qwen3:8b"), ("remote", "gpt-6.1-sol"), ("remote", "gpt-6.1-sol"), ("remote", "gpt-6.1-sol")]
    assert len(gateway.providers) == 3
