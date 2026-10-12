import asyncio
from copy import deepcopy
import json

import httpx
from ollama import AsyncClient, ResponseError
import pytest
import yaml

from companion.core.agent import Agent
from companion.models.base import ModelResponse
from companion.models.gateway import ModelGateway
from companion.models.providers.ollama import OllamaProvider
from companion.models.registry import ModelRegistry
from companion.tools.registry import Tool, ToolArguments, ToolRegistry


def registry(tmp_path, provider="ollama"):
    path = tmp_path / "models.yaml"
    config = {"provider": provider, "model": "test-model", "host": "http://ollama.test"}
    path.write_text(yaml.safe_dump({"models": {"primary": config, "coding": config}}))
    return ModelRegistry(path)


def test_gateway_shares_identical_backends_and_forwards_tools(tmp_path, monkeypatch):
    requests = []

    class Provider:
        def __init__(self, model, host):
            pass

        async def generate(self, request):
            requests.append(request)
            return ModelResponse("answer", "test-model")

    monkeypatch.setattr("companion.models.gateway.OllamaProvider", Provider)
    gateway = ModelGateway(registry(tmp_path))
    tools = [{"type": "function", "function": {"name": "status"}}]

    async def check():
        await gateway.generate("primary", [], tools=tools)
        await gateway.generate("coding", [])

    asyncio.run(check())
    assert len(gateway.providers) == 1
    assert requests[0].tools == tools
    assert requests[1].tools == []


@pytest.mark.parametrize("role,provider,error", [
    ("missing", "ollama", "Unknown model role"),
    ("primary", "unsupported", "Unsupported provider"),
])
def test_gateway_rejects_invalid_configuration(tmp_path, role, provider, error):
    gateway = ModelGateway(registry(tmp_path, provider=provider))
    with pytest.raises(ValueError, match=error):
        asyncio.run(gateway.generate(role, []))
    assert not gateway.providers


def test_agent_through_real_ollama_sdk_tool_round_trip(tmp_path):
    """Exercise SDK serialization and decoding without downloading a model."""
    requests = []

    def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/api/chat"
        assert body["tools"][0]["function"]["name"] == "status"
        assert body["stream"] is False
        if len(requests) == 1:
            message = {
                "role": "assistant", "content": "", "thinking": "Measure first",
                "tool_calls": [{"function": {"name": "status", "arguments": {}}}],
            }
        else:
            assert body["messages"][-2]["thinking"] == "Measure first"
            assert body["messages"][-1]["tool_name"] == "status"
            assert json.loads(body["messages"][-1]["content"])["result"] == {"memory_percent": 42}
            message = {"role": "assistant", "content": "Memory usage is 42%"}
        return httpx.Response(200, json={"model": "test-model", "message": message, "done": True})

    async def check():
        provider = OllamaProvider("test-model", "http://ollama.test")
        await provider.client._client.aclose()
        provider.client = AsyncClient(host="http://ollama.test", transport=httpx.MockTransport(endpoint))
        gateway = ModelGateway(registry(tmp_path))
        gateway.providers[("ollama", "test-model", "http://ollama.test")] = provider
        tools = ToolRegistry()

        async def status(_):
            return {"memory_percent": 42}

        tools.register(Tool("status", "Read status", ToolArguments, status))
        try:
            messages = [{"role": "user", "content": "Memory usage?"}]
            original = deepcopy(messages)
            result = await Agent(gateway, tools).run("primary", messages)
            assert result.response.content == "Memory usage is 42%"
            assert messages == original
            assert len(requests) == 2
        finally:
            await provider.client._client.aclose()

    asyncio.run(check())


def test_sdk_http_error_propagates_through_gateway(tmp_path):
    async def check():
        provider = OllamaProvider("test-model", "http://ollama.test")
        await provider.client._client.aclose()
        provider.client = AsyncClient(
            host="http://ollama.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"error": "unavailable"})),
        )
        gateway = ModelGateway(registry(tmp_path))
        gateway.providers[("ollama", "test-model", "http://ollama.test")] = provider
        try:
            with pytest.raises(ResponseError) as error:
                await gateway.generate("primary", [])
            assert error.value.status_code == 503
        finally:
            await provider.client._client.aclose()

    asyncio.run(check())
