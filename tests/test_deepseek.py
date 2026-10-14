import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.workflow import WorkflowError, WorkflowRunner
from companion.core.agent import Agent
from companion.discord_workflow import check_models
from companion.models.base import ModelRequest, ToolCall
from companion.models.gateway import ModelGateway
from companion.models.providers.deepseek import DeepSeekProvider, chat_messages
from companion.models.providers.openai import responses_input
from companion.models.registry import ModelConfig, ModelRegistry
from companion.tools.registry import Tool, ToolArguments, ToolRegistry
from test_collaboration import MemoryBus


def completion(content='answer', **message_updates):
    return {'model': 'deepseek-chat', 'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content, **message_updates}}]}


def test_deepseek_json_mode_uses_separate_endpoint_and_key(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'deepseek-test-key')
    monkeypatch.setenv('CODING_API_KEY', 'openai-test-key')
    requests = []
    def endpoint(request):
        requests.append(request)
        assert request.headers['Authorization'] == 'Bearer deepseek-test-key'
        assert request.url == 'https://api.deepseek.com/v1/chat/completions'
        return httpx.Response(200, json=completion('{"action":"finish"}'))
    provider = DeepSeekProvider('deepseek-chat', transport=httpx.MockTransport(endpoint))
    messages = [{'role': 'system', 'content': 'Manager'}, {'role': 'user', 'content': 'task'}]
    original = deepcopy(messages)
    response = asyncio.run(provider.generate(ModelRequest(messages, response_format={'type': 'json_schema', 'schema': {'type': 'object'}, 'strict': True})))
    body = json.loads(requests[0].content)
    assert body['model'] == 'deepseek-chat'
    assert body['response_format'] == {'type': 'json_object'}
    assert body['stream'] is False
    assert 'JSON schema' in body['messages'][0]['content']
    assert 'input' not in body and 'text' not in body
    assert messages == original
    assert response.content == '{"action":"finish"}'


def test_deepseek_agent_tool_loop_preserves_call_ids_and_scoped_reasoning(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test')
    requests = []
    def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['tools'][0]['function']['name'] == 'status'
        if len(requests) == 1:
            data = completion(None, reasoning_content='private reasoning', tool_calls=[{'id': 'call_ds', 'type': 'function', 'function': {'name': 'status', 'arguments': '{}'}}])
            data['choices'][0]['finish_reason'] = 'tool_calls'
            return httpx.Response(200, json=data)
        assert body['messages'][-2]['reasoning_content'] == 'private reasoning'
        assert body['messages'][-2]['tool_calls'][0]['function']['arguments'] == '{}'
        assert body['messages'][-1]['tool_call_id'] == 'call_ds'
        assert json.loads(body['messages'][-1]['content'])['result'] == {'memory': 42}
        return httpx.Response(200, json=completion('Memory 42'))
    provider = DeepSeekProvider('deepseek-chat', transport=httpx.MockTransport(endpoint))
    class Gateway:
        async def generate(self, role, messages, tools=None):
            return await provider.generate(ModelRequest(messages, tools=tools or []))
    registry = ToolRegistry()
    async def status(_):
        return {'memory': 42}
    registry.register(Tool('status', 'status', ToolArguments, status))
    result = asyncio.run(Agent(Gateway(), registry).run('primary', [{'role': 'user', 'content': 'status'}]))
    assert result.response.content == 'Memory 42'
    assert len(requests) == 2
    assert 'private reasoning' not in result.response.content
    # Native metadata must not escape into another provider/model's API input.
    for translated in (responses_input(result.messages, model='gpt-6.1-sol'), chat_messages(result.messages, model='other-deepseek-model')):
        assert 'private reasoning' not in json.dumps(translated)


def test_missing_deepseek_key_fails_before_network(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    provider = DeepSeekProvider('deepseek-chat', transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected request')))
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        asyncio.run(provider.generate(ModelRequest([])))


def test_native_search_cannot_be_sent_to_deepseek(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test')
    provider = DeepSeekProvider('deepseek-chat', transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected request')))
    with pytest.raises(ValueError, match='native web_search'):
        asyncio.run(provider.generate(ModelRequest([], tools=[{'type': 'web_search'}])))


@pytest.mark.parametrize('data', [
    {'choices': []}, [], {'error': {'message': 'do not echo'}},
    {'choices': [{'finish_reason': 'length', 'message': {'role': 'assistant', 'content': 'partial'}}]},
    completion('answer', tool_calls=[{'id': 'bad', 'type': 'function', 'function': {'name': 'tool', 'arguments': '[]'}}]),
    completion('answer', tool_calls=[{'id': 'bad', 'type': 'function', 'function': {'name': 'tool', 'arguments': '{}'}}]),
])
def test_incomplete_invalid_and_error_responses_are_rejected(monkeypatch, data):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test')
    provider = DeepSeekProvider('deepseek-chat', transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    with pytest.raises(ValueError, match='invalid or incomplete'):
        asyncio.run(provider.generate(ModelRequest([])))


def test_http_error_does_not_echo_body_or_keys(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'private-key')
    provider = DeepSeekProvider('deepseek-chat', transport=httpx.MockTransport(lambda _: httpx.Response(401, json={'error': 'private-key sensitive body'})))
    with pytest.raises(RuntimeError) as error:
        asyncio.run(provider.generate(ModelRequest([])))
    assert 'HTTP 401' in str(error.value)
    assert 'private-key' not in str(error.value) and 'sensitive body' not in str(error.value)


@pytest.mark.parametrize('url', ['http://api.deepseek.com', 'https://user:key@api.deepseek.com', 'https://api.deepseek.com?key=secret', 'https://api.deepseek.com#fragment'])
def test_deepseek_endpoint_keeps_credentials_on_https(url):
    with pytest.raises(ValueError):
        DeepSeekProvider('deepseek-chat', base_url=url)


def test_chat_history_rejects_unmatched_and_unanswered_tool_calls():
    with pytest.raises(ValueError, match='matching'):
        chat_messages([{'role': 'tool', 'tool_call_id': 'missing', 'content': '{}'}], model='deepseek-chat')
    with pytest.raises(ValueError, match='unanswered'):
        chat_messages([{'role': 'assistant', 'tool_calls': [ToolCall('tool', {}).as_dict()]}], model='deepseek-chat')


def test_deepseek_defaults_and_model_gateway_cache(monkeypatch, tmp_path):
    config = ModelConfig(provider='deepseek', model='deepseek-chat')
    assert config.base_url == 'https://api.deepseek.com/v1'
    assert config.api_key_env == 'DEEPSEEK_API_KEY'
    path = tmp_path / 'models.yaml'
    path.write_text('models:\n  primary:\n    provider: deepseek\n    model: deepseek-chat\n  other:\n    provider: deepseek\n    model: deepseek-chat\n')
    requests = []
    class Provider:
        def __init__(self, model, base_url, api_key_env):
            assert api_key_env == 'DEEPSEEK_API_KEY'
        async def generate(self, request):
            requests.append(request)
            return SimpleNamespace(content='ok')
    monkeypatch.setattr('companion.models.gateway.DeepSeekProvider', Provider)
    gateway = ModelGateway(ModelRegistry(path))
    async def check():
        await gateway.generate('primary', [], response_format={'type': 'json_object'})
        await gateway.generate('other', [])
    asyncio.run(check())
    assert len(gateway.providers) == 1
    assert requests[0].response_format == {'type': 'json_object'}


def test_direct_openai_coding_does_not_require_deepseek_key(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    config = CollaborationConfig.load(root / 'config/agents.yaml')
    registry = ModelRegistry(root / 'config/models.yaml')
    monkeypatch.setenv('CODING_API_KEY', 'test')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    check_models(config, registry, ['coding'])
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        check_models(config, registry)


def test_deepseek_invalid_manager_decision_is_locally_rejected(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test')
    root = Path(__file__).resolve().parents[1]
    config = CollaborationConfig.load(root / 'config/agents.yaml')
    registry = ModelRegistry(root / 'config/models.yaml')
    model = registry.get('primary')
    gateway = ModelGateway(registry)
    gateway.providers[(model.provider, model.model, model.base_url, model.api_key_env)] = DeepSeekProvider(
        model.model, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion('{"action":"delegate","agent":"unknown","task":"run","answer":null}'))))
    bus = MemoryBus()
    with pytest.raises(WorkflowError, match='unknown'):
        asyncio.run(WorkflowRunner(config, bus, Agent(gateway, ToolRegistry())).run('task'))
    assert all(message.recipient == 'coordinator' for message in bus.published)
