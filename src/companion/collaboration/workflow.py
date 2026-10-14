import asyncio
from dataclasses import dataclass
import json
import time
from typing import Protocol
from uuid import uuid4

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.protocol import Envelope
from companion.core.agent import Agent


class TaskBus(Protocol):
    async def publish(self, envelope: Envelope): ...
    async def poll(self) -> list[Envelope]: ...


class WorkflowError(RuntimeError):
    pass


@dataclass
class WorkflowResult:
    job_id: str
    content: str
    results: dict[str, str]


class WorkflowRunner:
    def __init__(self, config: CollaborationConfig, bus: TaskBus, agent: Agent):
        self.config = config
        self.bus = bus
        self.agent = agent

    async def run(self, goal: str) -> WorkflowResult:
        if not goal.strip() or len(goal) > 32000:
            raise ValueError("Task goal must contain 1–32000 characters")
        try:
            return await asyncio.wait_for(self._run(goal), timeout=self.config.task_timeout)
        except TimeoutError as exc:
            raise WorkflowError("Discord workflow timed out; the job was not resumed or retried") from exc

    async def _run(self, goal):
        job_id = uuid4()
        expires_at = time.time() + self.config.task_timeout
        results = {}
        executed = set()
        step = 0
        expected_task = uuid4()
        sender = self.config.coordinator

        async def submit():
            await self.bus.publish(Envelope(
                kind="task", job_id=job_id, task_id=expected_task, sender=sender,
                recipient=self.config.workflow[step], step=step, expires_at=expires_at,
                content=goal, context=dict(results),
            ))

        await submit()
        while True:
            messages = await self.bus.poll()
            if not messages:
                await asyncio.sleep(self.config.poll_interval)
                continue
            for message in messages:
                if (
                    message.job_id != job_id or message.task_id != expected_task
                    or message.step != step or message.expires_at <= time.time()
                ):
                    continue
                role = self.config.workflow[step]
                if message.kind == "task":
                    if message.sender != sender or message.recipient != role or message.task_id in executed:
                        continue
                    executed.add(message.task_id)
                    spec = self.config.agents[role]
                    instructions = {
                        "goal": message.content, "stage": step + 1,
                        "stage_count": len(self.config.workflow), "previous_results": message.context,
                    }
                    try:
                        output = await self.agent.run(spec.model_role, [
                            {"role": "system", "content": spec.prompt},
                            {"role": "user", "content": json.dumps(instructions, ensure_ascii=False)},
                        ])
                        content = output.response.content
                        if len(content) > 32000:
                            raise WorkflowError("Model result exceeds the task message budget")
                    except Exception as exc:
                        await self.bus.publish(Envelope(
                            kind="error", job_id=job_id, task_id=expected_task, sender=role,
                            recipient=self.config.coordinator, step=step, expires_at=expires_at,
                            content=f"{role} step failed ({type(exc).__name__})",
                        ))
                    else:
                        await self.bus.publish(Envelope(
                            kind="result", job_id=job_id, task_id=expected_task, sender=role,
                            recipient=self.config.coordinator, step=step, expires_at=expires_at, content=content,
                        ))
                elif message.sender == role and message.recipient == self.config.coordinator and expected_task in executed:
                    if message.kind == "error":
                        raise WorkflowError(message.content)
                    results[f"{step + 1}:{role}"] = message.content
                    if step == len(self.config.workflow) - 1:
                        return WorkflowResult(str(job_id), message.content, results)
                    if sum(len(value) for value in results.values()) > self.config.context_limit:
                        raise WorkflowError("Workflow context budget exceeded")
                    sender = role
                    step += 1
                    expected_task = uuid4()
                    await submit()
