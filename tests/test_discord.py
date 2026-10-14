import asyncio
from email import policy
from email.parser import BytesParser
import json
import time
from uuid import uuid4

import httpx
import pytest

from companion.collaboration.discord import BotClient, DiscordBus, DiscordError
from companion.collaboration.protocol import Envelope, MAX_BYTES, PREFIX
from companion.collaboration.workflow import WorkflowRunner
from companion.core.agent import Agent
from companion.models.gateway import ModelGateway
from companion.models.providers.openai import OpenAIProvider
from companion.models.providers.deepseek import DeepSeekProvider
from companion.models.registry import ModelRegistry
from companion.tools.system import default_tools
from test_collaboration import RecordingAgent, configuration


class DiscordService:
    def __init__(self, config, monkeypatch):
        self.roles = {}
        self.ids = {}
        self.messages = []
        self.attachments = {}
        self.requests = []
        self.application_flags = 1 << 18
        for index, (role, spec) in enumerate(config.agents.items(), start=100):
            token = f"test-token-{role}"
            monkeypatch.setenv(spec.token_env, token)
            self.roles[token] = role
            self.ids[role] = str(index)
        monkeypatch.setenv(config.channel_env, "42")
        if config.require_separate_bus:
            monkeypatch.setenv(config.bus_channel_env, "43")

    def add(self, author, content, attachments=None, channel='42'):
        message = {"id": str(len(self.messages) + 1), "author": {"id": author, "bot": True},
                   "content": content, "attachments": attachments or [], "channel_id": channel}
        self.messages.append(message)
        return message

    def handle(self, request):
        self.requests.append(request)
        if request.url.host == "cdn.discordapp.com":
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=self.attachments[request.url.path])
        token = request.headers["Authorization"].removeprefix("Bot ")
        assert request.headers["User-Agent"].startswith("DiscordBot (")
        role = self.roles[token]
        if request.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": self.ids[role], "bot": True})
        if request.url.path.endswith("/applications/@me"):
            return httpx.Response(200, json={"flags": self.application_flags})
        assert request.url.path in ("/api/v10/channels/42/messages", "/api/v10/channels/43/messages")
        channel = request.url.path.split('/')[-2]
        if request.method == "GET":
            after = int(request.url.params.get("after", "0"))
            limit = int(request.url.params.get("limit", "100"))
            messages = [message for message in self.messages if int(message["id"]) > after and message['channel_id'] == channel]
            return httpx.Response(200, json=list(reversed(messages))[:limit])
        attachments = []
        if request.headers["Content-Type"].startswith("multipart/form-data"):
            mime = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {request.headers['Content-Type']}\r\n\r\n".encode() + request.content,
            )
            parts = {part.get_param("name", header="Content-Disposition"): part for part in mime.iter_parts()}
            payload = json.loads(parts["payload_json"].get_payload(decode=True))
            data = parts["files[0]"].get_payload(decode=True)
            path = f"/attachments/42/{len(self.messages) + 1}/agent-task.json"
            self.attachments[path] = data
            attachments = [{"filename": "agent-task.json", "size": len(data), "url": f"https://cdn.discordapp.com{path}"}]
        else:
            payload = json.loads(request.content)
        assert payload["allowed_mentions"] == {"parse": []}
        assert payload["enforce_nonce"] is True
        assert len(payload["nonce"]) <= 25
        return httpx.Response(200, json=self.add(self.ids[role], payload["content"], attachments, channel=channel))


def envelope(**updates):
    data = {"kind": "task", "job_id": uuid4(), "task_id": uuid4(), "sender": "coordinator",
            "recipient": "coding", "step": 0, "expires_at": time.time() + 60, "content": "Task"}
    data.update(updates)
    return Envelope(**data)


def test_four_distinct_bots_complete_a_workflow_through_mock_discord_http(monkeypatch):
    config = configuration()
    service = DiscordService(config, monkeypatch)
    bus = DiscordBus(config, transport=httpx.MockTransport(service.handle))
    agent = RecordingAgent()

    async def check():
        try:
            await bus.initialize()
            result = await WorkflowRunner(config, bus, agent).run("Design a parser")
            assert result.content == "primary result"
            assert len({message["author"]["id"] for message in service.messages}) == 4
            assert len(service.messages) == 10
            assert len(agent.requests) == 5
        finally:
            await bus.close()

    asyncio.run(check())


def test_gateway_agent_system_tool_and_all_cloud_models_share_real_http_code_paths(monkeypatch, tmp_path):
    from pathlib import Path
    from companion.collaboration.config import CollaborationConfig
    from companion.collaboration.factory import build_agents

    root = Path(__file__).resolve().parents[1]
    config = CollaborationConfig.load(root / "config" / "agents.yaml")
    assert set(config.agents) == {'coordinator', 'coding', 'web'}
    monkeypatch.setenv('DISCORD_SYSTEM_BOT_TOKEN', 'unused-token')
    service = DiscordService(config, monkeypatch)
    bus = DiscordBus(config, transport=httpx.MockTransport(service.handle))
    registry = ModelRegistry(root / "config" / "models.yaml")
    gateway = ModelGateway(registry)
    monkeypatch.setenv("CODING_API_KEY", "test-model-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    model_requests = []
    coding_requests = []
    decisions = iter([
        {"action": "delegate", "agent": role, "task": "Check", "answer": None}
        for role in ("web", "coding")
    ] + [{"action": "finish", "agent": None, "task": None, "answer": "Completed"}])
    monkeypatch.setenv("SYSTEM_WORKSPACE", str(tmp_path / "system"))
    monkeypatch.setenv("CODING_WORKSPACE", str(tmp_path / "coding"))

    def models_endpoint(request):
        body = json.loads(request.content)
        if request.url.host == "api.deepseek.com":
            assert request.headers["Authorization"] == "Bearer test-deepseek-key"
            assert request.url.path == "/chat/completions"
            assert body["model"] == "deepseek-flash"
            assert body["thinking"] == {"type": "enabled"}
            assert body["reasoning_effort"] == "high"
        else:
            assert request.url.host == "api.openai.com"
            assert request.headers["Authorization"] == "Bearer test-model-key"
            assert request.url.path == "/v1/responses"
        model_requests.append(body["model"])
        model = body["model"]
        text = f"{model} result"
        annotations = []
        if model == registry.get("primary").model:
            assert body["response_format"] == {"type": "json_object"}
            assert "JSON schema" in body["messages"][0]["content"]
            assert "tools" not in body
            text = json.dumps(next(decisions))
        elif model == registry.get("coding").model and not any(tool.get('type') == 'web_search' for tool in body.get('tools', [])):
            coding_requests.append(body)
            names = {tool['name'] for tool in body['tools']}
            assert {'write_project_file', 'apply_text_edit', 'run_python_tests', 'run_project_command'} <= names
            assert {'get_system_status', 'delete_file', 'restore_file'} <= names
            if len(coding_requests) == 1:
                output = [
                    {'type': 'function_call', 'call_id': 'call_status', 'name': 'get_system_status', 'arguments': '{}'},
                    {'type': 'function_call', 'call_id': 'write_code', 'name': 'write_project_file',
                     'arguments': json.dumps({'path': 'addition.py', 'content': 'def add(a, b):\n    return a + b\n'})},
                    {'type': 'function_call', 'call_id': 'write_test', 'name': 'write_project_file',
                     'arguments': json.dumps({'path': 'tests/test_addition.py', 'content': 'import os\nfrom addition import add\ndef test_add():\n    assert add(2, 3) == 5\n    assert "CODING_API_KEY" not in os.environ\n'})},
                ]
                return httpx.Response(200, json={'status': 'completed', 'model': model, 'output': output})
            elif len(coding_requests) == 2:
                status = next(item for item in body['input'] if item.get('call_id') == 'call_status' and item['type'] == 'function_call_output')
                assert json.loads(status['output'])['result']['memory']['total_bytes'] > 0
                assert json.loads(body['input'][-1]['output'])['result']['status'] == 'written'
                output = [{'type': 'function_call', 'call_id': 'run_test', 'name': 'run_project_command',
                           'arguments': json.dumps({'command': ['python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests']})}]
                return httpx.Response(200, json={'status': 'completed', 'model': model, 'output': output})
            tested = json.loads(body['input'][-1]['output'])['result']
            assert tested['success'] is True and '1 passed' in tested['output']
        elif model == registry.get("web").model:
            assert {tool["type"] for tool in body["tools"]} == {"web_search", "function"}
            annotations = [{"type": "url_citation", "url": "https://docs.python.org/3/"}]
        if request.url.host == "api.deepseek.com":
            return httpx.Response(200, json={"model": model, "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
        output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": annotations}]}]
        return httpx.Response(200, json={"status": "completed", "model": model, "output": output})

    for model in registry.config.models.values():
        assert model.provider in ("openai", "deepseek")
        key = (model.provider, model.model, model.base_url, model.api_key_env)
        provider = OpenAIProvider if model.provider == "openai" else DeepSeekProvider
        options = {}
        if model.provider == "deepseek":
            key += (model.thinking or "", model.reasoning_effort or "")
            options = {"thinking": model.thinking, "reasoning_effort": model.reasoning_effort}
        gateway.providers[key] = provider(
            model.model, model.base_url, model.api_key_env, transport=httpx.MockTransport(models_endpoint),
            **options,
        )

    async def check():
        try:
            await bus.initialize()
            result = await WorkflowRunner(config, bus, build_agents(config, gateway)).run("Review system monitoring code")
            assert result.content == "Completed"
            assert "https://docs.python.org/3/" in result.results["1:web"]
            assert len(set(model_requests)) == 2  # Independent coding/web roles share one OpenAI model.
            assert len(model_requests) == 7
            assert len(service.messages) == 10
            assert (tmp_path / 'coding' / 'addition.py').is_file()
        finally:
            await bus.close()

    asyncio.run(check())


def test_long_unicode_task_round_trips_as_attachment_without_sending_token_to_cdn(monkeypatch):
    config = configuration()
    service = DiscordService(config, monkeypatch)
    bus = DiscordBus(config, transport=httpx.MockTransport(service.handle))
    task = envelope(content="🙂" * 1000)

    async def check():
        try:
            await bus.initialize()
            await bus.publish(task)
            received = await bus.poll()
            assert received == [task]
            assert service.messages[0]["attachments"]
            assert await bus.poll() == []
        finally:
            await bus.close()

    asyncio.run(check())


def test_chat_unknown_authors_sender_spoofing_and_expired_messages_do_not_execute(monkeypatch):
    config = configuration()
    service = DiscordService(config, monkeypatch)
    bus = DiscordBus(config, transport=httpx.MockTransport(service.handle))

    async def check():
        try:
            await bus.initialize()
            valid = envelope()
            content = PREFIX + valid.encode().decode()
            service.add("999", content)
            service.add(service.ids["coding"], content)
            service.add(service.ids["coordinator"], "ordinary chat")
            service.add(service.ids["coordinator"], PREFIX + "not-json")
            expired = envelope(expires_at=time.time() - 1)
            service.add(service.ids["coordinator"], PREFIX + expired.encode().decode())
            await bus.publish(valid)
            assert await bus.poll() == [valid]
            assert await bus.poll() == []
        finally:
            await bus.close()

    asyncio.run(check())


def test_initialize_skips_history_and_rejects_reused_bot_accounts(monkeypatch):
    config = configuration()
    service = DiscordService(config, monkeypatch)
    service.add(service.ids["coordinator"], PREFIX + envelope().encode().decode())
    bus = DiscordBus(config, transport=httpx.MockTransport(service.handle))

    async def check():
        try:
            await bus.initialize()
            assert await bus.poll() == []
            service.ids["coding"] = service.ids["coordinator"]
            with pytest.raises(DiscordError, match="different Discord bot"):
                await bus.initialize()
        finally:
            await bus.close()

    asyncio.run(check())


def test_missing_bindings_fail_before_network_access(monkeypatch):
    config = configuration()
    for spec in config.agents.values():
        monkeypatch.delenv(spec.token_env, raising=False)
    monkeypatch.delenv(config.channel_env, raising=False)
    bus = DiscordBus(config, transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected network request")))

    async def check():
        try:
            with pytest.raises(DiscordError, match="DISCORD_CHANNEL_ID"):
                await bus.initialize()
        finally:
            await bus.close()

    asyncio.run(check())


def test_missing_message_content_intent_blocks_before_posting(monkeypatch):
    config = configuration()
    service = DiscordService(config, monkeypatch)
    service.application_flags = 0
    bus = DiscordBus(config, transport=httpx.MockTransport(service.handle))

    async def check():
        try:
            with pytest.raises(DiscordError, match="Message Content Intent"):
                await bus.initialize()
            assert not service.messages
        finally:
            await bus.close()

    asyncio.run(check())


def test_rate_limit_retries_follow_retry_after(monkeypatch):
    monkeypatch.setenv("TEST_BOT_TOKEN", "test-token")
    requests = []

    def endpoint(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"retry_after": 0})
        return httpx.Response(200, json={"ok": True})

    client = BotClient("TEST_BOT_TOKEN", transport=httpx.MockTransport(endpoint))

    async def check():
        try:
            assert await client.request("GET", "/users/@me") == {"ok": True}
            assert len(requests) == 2
        finally:
            await client.close()

    asyncio.run(check())


def test_persistent_rate_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("TEST_BOT_TOKEN", "test-token")
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(429, json={"retry_after": 0})

    client = BotClient("TEST_BOT_TOKEN", transport=httpx.MockTransport(endpoint))

    async def check():
        try:
            with pytest.raises(DiscordError, match="retry later"):
                await client.request("GET", "/users/@me")
            assert len(requests) == 4
        finally:
            await client.close()

    asyncio.run(check())


def test_uncertain_post_failure_is_not_automatically_retried(monkeypatch):
    monkeypatch.setenv("TEST_BOT_TOKEN", "test-token")
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(500, json={"error": "test-token must not be logged"})

    client = BotClient("TEST_BOT_TOKEN", transport=httpx.MockTransport(endpoint))

    async def check():
        try:
            with pytest.raises(DiscordError, match="HTTP 500") as error:
                await client.request("POST", "/channels/42/messages", json={"content": "test"})
            assert "test-token" not in str(error.value)
            assert len(requests) == 1
        finally:
            await client.close()

    asyncio.run(check())


def test_attachment_origin_and_size_are_bounded():
    client = BotClient("UNUSED_TOKEN", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * (MAX_BYTES + 1))))

    async def check():
        try:
            with pytest.raises(DiscordError, match="Discord CDN"):
                await client.download("https://example.com/task.json")
            with pytest.raises(DiscordError, match="512 KiB"):
                await client.download("https://cdn.discordapp.com/task.json")
        finally:
            await client.close()

    asyncio.run(check())


def test_startup_channel_404_identifies_endpoint_and_hides_response_body(monkeypatch):
    config = configuration()
    service = DiscordService(config, monkeypatch)
    requests = []
    def endpoint(request):
        requests.append(request)
        if request.url.path == '/api/v10/channels/42/messages':
            return httpx.Response(404, json={'code': 10003, 'message': 'sensitive response body'})
        return service.handle(request)
    bus = DiscordBus(config, transport=httpx.MockTransport(endpoint))
    async def check():
        try:
            with pytest.raises(DiscordError) as error:
                await bus.initialize()
            message = str(error.value)
            assert 'DISCORD_COORDINATOR_BOT_TOKEN' in message
            assert 'GET /channels/42/messages' in message
            assert 'HTTP 404, Discord code 10003' in message
            assert 'configured channel ID' in message
            assert 'sensitive response body' not in message
            assert all(f'test-token-{role}' not in message for role in config.agents)
            assert len(bus.bot_ids) == 4  # Identity and intent preflight succeeded.
            assert all(request.method == 'GET' for request in requests)
        finally:
            await bus.close()
    asyncio.run(check())


@pytest.mark.parametrize('path,response,expected_hint', [
    ('/applications/@me', httpx.Response(404, json={'code': 10002, 'message': 'sensitive body'}), False),
    ('/channels/42/messages', httpx.Response(403, json={'code': 50001, 'message': 'sensitive body'}), True),
    ('/channels/42/messages?private=sensitive-query', httpx.Response(404, text='sensitive body'), True),
])
def test_error_diagnostics_are_endpoint_specific_and_do_not_echo_payloads(monkeypatch, path, response, expected_hint):
    monkeypatch.setenv('TEST_BOT_TOKEN', 'private-test-token')
    client = BotClient('TEST_BOT_TOKEN', transport=httpx.MockTransport(lambda _: response))
    async def check():
        try:
            with pytest.raises(DiscordError) as error:
                await client.request('GET', path)
            message = str(error.value)
            assert f'GET {path.split("?", 1)[0]}' in message
            assert ('configured channel ID' in message) is expected_hint
            assert 'private-test-token' not in message
            assert 'sensitive body' not in message
            assert 'sensitive-query' not in message
            if response.headers.get('Content-Type', '').startswith('application/json'):
                assert f'Discord code {response.json()["code"]}' in message
        finally:
            await client.close()
    asyncio.run(check())
