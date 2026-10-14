import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion.collaboration.config import AgentSpec, CollaborationConfig
from companion.collaboration.factory import build_agents
from companion.collaboration.workflow import WorkflowError, WorkflowRunner
from companion.models.base import ModelResponse, ToolCall
from test_collaboration import MemoryBus, configuration


def flat_config(**updates):
    return configuration(workflow=None, **updates)


class ChoosingAgent:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = []

    async def run(self, role, messages, **options):
        self.calls.append((role, messages, options))
        text = json.dumps(next(self.decisions)) if role == 'primary' else f'{role} answer'
        return SimpleNamespace(response=ModelResponse(text, 'fake'))


def delegate(role):
    return dict(action='delegate', agent=role, task='Specific task', answer=None)


def finish():
    return dict(action='finish', agent=None, task=None, answer='Delivered')


def test_manager_selects_only_needed_roles_and_results_travel_over_bus():
    agent = ChoosingAgent([delegate('coding'), finish()])
    bus = MemoryBus(duplicates=True, inject=True)
    result = asyncio.run(WorkflowRunner(flat_config(), bus, agent).run('Code'))
    assert [call[0] for call in agent.calls] == ['primary', 'coding', 'primary']
    assert list(result.results) == ['1:coding']
    assert result.content == 'Delivered'
    assert len(bus.published) == 6
    assert agent.calls[0][2]['response_format']['strict']
    assert 'coding answer' in agent.calls[-1][1][-1]['content']


def test_manager_can_answer_without_calling_workers():
    agent = ChoosingAgent([finish()])
    result = asyncio.run(WorkflowRunner(flat_config(), MemoryBus(), agent).run('Hi'))
    assert result.results == {}
    assert len(agent.calls) == 1


@pytest.mark.parametrize('target', ['coding', 'research', 'review'])
def test_direct_dispatch_bypasses_manager(target):
    agent = ChoosingAgent([])
    bus = MemoryBus(duplicates=True)
    result = asyncio.run(WorkflowRunner(flat_config(), bus, agent).run('Task', target=target))
    assert [call[0] for call in agent.calls] == [target]
    assert not agent.calls[0][2]
    assert result.content == f'{target} answer'
    assert all(message.sender == target and message.recipient == target for message in bus.published)


def test_new_functional_bot_appears_in_catalog_and_can_be_selected():
    config = flat_config()
    config.agents['calendar'] = AgentSpec(model_role='calendar', token_env='CALENDAR_TOKEN', prompt='Calendar', description='Schedule')
    agent = ChoosingAgent([delegate('calendar'), finish()])
    asyncio.run(WorkflowRunner(config, MemoryBus(), agent).run('Plan'))
    assert [call[0] for call in agent.calls] == ['primary', 'calendar', 'primary']
    assert 'Schedule' in agent.calls[0][1][-1]['content']


@pytest.mark.parametrize('decision', [delegate('missing'), delegate('coordinator'), {'action': 'finish', 'agent': 'coding', 'task': None, 'answer': 'bad'}, {'action': 'shell', 'agent': None, 'task': None, 'answer': None}])
def test_manager_invalid_decisions_never_start_workers(decision):
    agent = ChoosingAgent([decision])
    with pytest.raises(WorkflowError):
        asyncio.run(WorkflowRunner(flat_config(), MemoryBus(), agent).run('Task'))
    assert len(agent.calls) == 1


def test_manager_budget_stops_further_dispatch():
    agent = ChoosingAgent([delegate('coding'), delegate('research')])
    with pytest.raises(WorkflowError, match='budget'):
        asyncio.run(WorkflowRunner(flat_config(max_dispatches=1), MemoryBus(), agent).run('Task'))
    assert [call[0] for call in agent.calls] == ['primary', 'coding', 'primary']


def test_default_roles_have_separate_tools_and_cannot_call_approval(tmp_path, monkeypatch):
    config = CollaborationConfig.load(Path(__file__).resolve().parents[1] / 'config/agents.yaml')
    monkeypatch.setenv('SYSTEM_WORKSPACE', str(tmp_path))
    agents = build_agents(config, object())
    assert set(agents) == {'coordinator', 'coding', 'web', 'system'}
    assert config.workflow is None
    assert not agents['coding'].tools.schemas()
    assert not agents['coordinator'].tools.schemas()
    assert agents['web'].builtin_tools == [{'type': 'web_search'}]
    assert not agents['system'].builtin_tools
    for role in ('coordinator', 'coding', 'web'):
        result = asyncio.run(agents[role].tools.execute(ToolCall('delete_file', {'path': 'file'})))
        assert not result['ok']
    result = asyncio.run(agents['system'].tools.execute(ToolCall('approve', {'request_id': 'any'})))
    assert not result['ok']
