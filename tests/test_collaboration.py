import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from companion.collaboration.config import AgentSpec, CollaborationConfig
from companion.collaboration.protocol import Envelope, MAX_BYTES
from companion.collaboration.workflow import WorkflowError, WorkflowRunner
from companion.models.base import ModelResponse


def configuration(**updates):
    data = {
        "agents": {
            role: {"model_role": "primary" if role == "coordinator" else role,
                   "token_env": f"DISCORD_{role.upper()}_BOT_TOKEN", "prompt": f"You are {role}"}
            for role in ("coordinator", "research", "coding", "review")
        },
        "workflow": ["coordinator", "research", "coding", "review", "coordinator"],
    }
    data.update(updates)
    return CollaborationConfig.model_validate(data)


class MemoryBus:
    def __init__(self, duplicates=False, inject=False):
        self.messages = []
        self.published = []
        self.duplicates = duplicates
        self.inject = inject

    async def publish(self, envelope):
        # A genuine serialization boundary: workers cannot share mutable Python data.
        envelope = Envelope.decode(envelope.encode())
        self.published.append(envelope)
        if self.inject and envelope.kind == "task":
            self.messages.extend([
                envelope.model_copy(update={"job_id": uuid4()}),
                envelope.model_copy(update={"expires_at": time.time() - 1}),
                envelope.model_copy(update={"recipient": "not-registered"}),
                envelope.model_copy(update={"kind": "result", "sender": envelope.recipient,
                                            "recipient": "coordinator", "content": "premature result"}),
            ])
        self.messages.append(envelope)
        if self.duplicates:
            self.messages.append(envelope)

    async def poll(self):
        messages, self.messages = self.messages, []
        return messages


class RecordingAgent:
    def __init__(self):
        self.requests = []

    async def run(self, role, messages):
        self.requests.append((role, messages))
        return SimpleNamespace(response=ModelResponse(f"{role} result", "test-model"))


def test_full_workflow_uses_bus_for_every_handoff_and_passes_all_previous_results():
    bus = MemoryBus(duplicates=True, inject=True)
    agent = RecordingAgent()
    result = asyncio.run(WorkflowRunner(configuration(), bus, agent).run("Build a parser"))
    assert [role for role, _ in agent.requests] == ["primary", "research", "coding", "review", "primary"]
    assert len(bus.published) == 10
    assert [event.kind for event in bus.published] == ["task", "result"] * 5
    assert len({event.task_id for event in bus.published}) == 5
    assert len({event.job_id for event in bus.published}) == 1
    assert [event.sender for event in bus.published if event.kind == "result"] == [
        "coordinator", "research", "coding", "review", "coordinator",
    ]
    final_input = json.loads(agent.requests[-1][1][-1]["content"])
    assert len(final_input["previous_results"]) == 4
    assert final_input["previous_results"]["3:coding"] == "coding result"
    assert result.content == "primary result"
    assert len(result.results) == 5


def test_new_functional_role_needs_configuration_only():
    config = configuration()
    config.agents["analysis"] = AgentSpec(model_role="analysis_model", token_env="DISCORD_ANALYSIS_BOT_TOKEN", prompt="Analyze")
    config.workflow = ["coordinator", "analysis", "coordinator"]
    bus = MemoryBus()
    agent = RecordingAgent()
    result = asyncio.run(WorkflowRunner(config, bus, agent).run("Analyze design"))
    assert [role for role, _ in agent.requests] == ["primary", "analysis_model", "primary"]
    assert result.results["2:analysis"] == "analysis_model result"


def test_worker_failure_is_sent_by_its_bot_then_stops_without_retry():
    class BrokenAgent:
        async def run(self, *args):
            raise ConnectionError("backend unavailable")

    bus = MemoryBus()
    with pytest.raises(WorkflowError, match="coordinator step failed"):
        asyncio.run(WorkflowRunner(configuration(), bus, BrokenAgent()).run("Task"))
    assert [message.kind for message in bus.published] == ["task", "error"]
    assert bus.published[-1].sender == "coordinator"


def test_context_budget_stops_before_another_model_call():
    bus = MemoryBus()
    agent = RecordingAgent()
    with pytest.raises(WorkflowError, match="context budget"):
        asyncio.run(WorkflowRunner(configuration(context_limit=3), bus, agent).run("Task"))
    assert len(agent.requests) == 1


def test_timeout_cancels_wait_and_does_not_resubmit():
    class SilentBus(MemoryBus):
        async def poll(self):
            return []

    bus = SilentBus()
    agent = RecordingAgent()
    with pytest.raises(WorkflowError, match="timed out"):
        asyncio.run(WorkflowRunner(configuration(task_timeout=0.01), bus, agent).run("Task"))
    assert len(bus.published) == 1
    assert not agent.requests


def test_timeout_cancels_in_progress_model_call():
    cancelled = []

    class SlowAgent:
        async def run(self, *args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    with pytest.raises(WorkflowError, match="timed out"):
        asyncio.run(WorkflowRunner(configuration(task_timeout=0.01), MemoryBus(), SlowAgent()).run("Task"))
    assert cancelled == [True]


@pytest.mark.parametrize("updates", [
    {"workflow": ["unknown", "coordinator"]},
    {"workflow": ["coding"]}, {"workflow": ["coordinator"] * 17},
    {"poll_interval": 0}, {"task_timeout": float("nan")}, {"context_limit": 64001},
])
def test_invalid_workflow_config(updates):
    with pytest.raises(ValueError):
        configuration(**updates)


def test_duplicate_token_bindings_are_rejected():
    data = configuration().model_dump()
    data["agents"]["coding"]["token_env"] = data["agents"]["coordinator"]["token_env"]
    with pytest.raises(ValueError, match="own bot token"):
        CollaborationConfig.model_validate(data)


def test_wire_protocol_rejects_unknown_fields_versions_and_large_messages():
    valid = Envelope(kind="task", job_id=uuid4(), task_id=uuid4(), sender="coordinator",
                     recipient="coding", step=0, expires_at=time.time() + 60, content="Task")
    for updates in ({"version": 2}, {"unexpected": "field"}, {"step": 16}, {"content": ""}):
        data = json.loads(valid.encode())
        data.update(updates)
        with pytest.raises(ValueError):
            Envelope.decode(json.dumps(data).encode())
    with pytest.raises(ValueError, match="512 KiB"):
        Envelope.decode(b"x" * (MAX_BYTES + 1))
