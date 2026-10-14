import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
import pytest

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.discord import DiscordBus, DiscordError, OperatorMessage
from companion.collaboration.hub import DiscordHub
from companion.models.base import ModelResponse
from companion.tools.approval import ApprovalManager, execution_context
from companion.tools.files import PathArguments, SystemFiles
from test_discord import DiscordService
from test_flat_dispatch import ChoosingAgent, delegate, finish


def setup(monkeypatch):
    config = CollaborationConfig.load(Path(__file__).resolve().parents[1] / 'config/agents.yaml')
    service = DiscordService(config, monkeypatch)
    monkeypatch.setenv('DISCORD_OPERATOR_IDS', '999,998')
    bus = DiscordBus(config, transport=httpx.MockTransport(service.handle))
    return config, service, bus


def human(service, role, text, author='999', bot=False):
    message = service.add(author, f'<@{service.ids[role]}> {text}')
    message['author']['bot'] = bot


def test_only_authorized_human_mentions_create_dispatch_events(monkeypatch):
    config, service, bus = setup(monkeypatch)
    async def check():
        try:
            await bus.initialize(require_operators=True)
            human(service, 'coding', 'Code')
            human(service, 'web', 'Find', author='998')
            human(service, 'system', 'Delete', author='997')
            human(service, 'system', 'approve fake', bot=True)
            human(service, 'coordinator', 'Task', author=service.ids['coding'])
            assert await bus.poll_events() == [OperatorMessage('coding', 'Code', '999'), OperatorMessage('web', 'Find', '998')]
            assert await bus.poll_events() == []
        finally:
            await bus.close()
    asyncio.run(check())


def test_listener_requires_operator_allowlist_before_network(monkeypatch):
    config, service, bus = setup(monkeypatch)
    monkeypatch.delenv('DISCORD_OPERATOR_IDS')
    async def check():
        try:
            with pytest.raises(DiscordError, match='DISCORD_OPERATOR_IDS'):
                await bus.initialize(require_operators=True)
            assert not service.requests
        finally:
            await bus.close()
    asyncio.run(check())


def test_direct_cli_bus_requires_only_target_bot(monkeypatch):
    config, service, _ = setup(monkeypatch)
    for role, spec in config.agents.items():
        if role != 'coding':
            monkeypatch.delenv(spec.token_env)
    bus = DiscordBus(config, observer='coding', transport=httpx.MockTransport(service.handle))
    async def check():
        try:
            await bus.initialize(active_roles=['coding'])
            assert bus.bot_ids == {'coding': service.ids['coding']}
            assert all(request.headers['Authorization'] == 'Bot test-token-coding' for request in service.requests)
        finally:
            await bus.close()
    asyncio.run(check())


async def until(hub, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'mock Discord job stalled'
        await hub.tick()
        await asyncio.sleep(0.005)


@pytest.mark.parametrize('role', ['coding', 'coordinator'])
def test_human_can_direct_dispatch_or_ask_manager_via_mentions(monkeypatch, tmp_path, role):
    config, service, bus = setup(monkeypatch)
    agent = ChoosingAgent([delegate('coding'), finish()])
    approvals = ApprovalManager(tmp_path / 'audit', terminal=False)
    hub = DiscordHub(config, bus, agent, approvals)
    async def check():
        try:
            await bus.initialize(require_operators=True)
            human(service, role, 'Write code')
            await hub.tick()
            await until(hub, lambda: not hub.tasks)
            expected = ['coding'] if role == 'coding' else ['primary', 'coding', 'primary']
            assert [call[0] for call in agent.calls] == expected
            assert any(message['content'].startswith('任务 ') for message in service.messages)
            assert not hub.mailboxes
        finally:
            await hub.close()
            await bus.close()
    asyncio.run(check())


def test_discord_approval_is_processed_while_system_worker_waits(monkeypatch, tmp_path):
    config, service, bus = setup(monkeypatch)
    approvals = ApprovalManager(tmp_path / '.agent-internal/audit', terminal=False, timeout=5)
    files = SystemFiles(tmp_path, approvals, [])
    (tmp_path / 'target.txt').write_text('important')
    calls = []
    class SystemAgent:
        async def run(self, role, messages):
            calls.append((role, execution_context.get()))
            result = await files.delete_file(PathArguments(path='target.txt'))
            return SimpleNamespace(response=ModelResponse(json.dumps(result), 'fake'))
    hub = DiscordHub(config, bus, {'system': SystemAgent()}, approvals)
    async def check():
        try:
            await bus.initialize(require_operators=True)
            human(service, 'system', 'Delete target.txt')
            await hub.tick()
            await until(hub, lambda: bool(approvals.pending))
            request_id = next(iter(approvals.pending))
            assert (tmp_path / 'target.txt').exists()
            assert calls[0][1].owner == '999'
            assert calls[0][1].role == 'system'
            human(service, 'system', f'approve {request_id}', author='998')
            human(service, 'system', f'approve {request_id}', author='999', bot=True)
            await hub.tick()
            assert (tmp_path / 'target.txt').exists()
            assert not next(iter(approvals.pending.values()))[1].done()
            human(service, 'system', f'approve {request_id}')
            await hub.tick()
            await until(hub, lambda: not hub.tasks)
            assert not (tmp_path / 'target.txt').exists()
            assert len(calls) == 1
            assert not approvals.pending
            human(service, 'system', f'approve {request_id}')
            await hub.tick()
            assert len(calls) == 1
            assert '失效' in service.messages[-1]['content']
        finally:
            await hub.close()
            await bus.close()
    asyncio.run(check())


def test_concurrent_jobs_have_separate_owners_and_close_cancels_approvals(monkeypatch, tmp_path):
    config, service, bus = setup(monkeypatch)
    approvals = ApprovalManager(tmp_path / 'audit', terminal=False, timeout=5)
    class WaitingAgent:
        async def run(self, role, messages):
            await approvals.require('delete_file', {'path': 'same'})
            raise AssertionError('No approval sent')
    hub = DiscordHub(config, bus, {'system': WaitingAgent()}, approvals, max_active_jobs=2)
    async def check():
        try:
            await bus.initialize(require_operators=True)
            human(service, 'system', 'Delete', author='999')
            human(service, 'system', 'Delete', author='998')
            human(service, 'system', 'Busy', author='999')
            await hub.tick()
            assert len(hub.tasks) == 2
            assert '并发上限' in service.messages[-1]['content']
            await until(hub, lambda: len(approvals.pending) == 2)
            requests = [request for request, _ in approvals.pending.values()]
            assert {request['owner'] for request in requests} == {'999', '998'}
            assert len({request['job_id'] for request in requests}) == 2
            await hub.close()
            assert not approvals.pending
            assert not hub.mailboxes
        finally:
            await hub.close()
            await bus.close()
    asyncio.run(check())
