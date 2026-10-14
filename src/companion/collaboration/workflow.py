import asyncio
from dataclasses import dataclass
import json
import time
from typing import Protocol
from uuid import uuid4

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.protocol import Envelope
from companion.core.agent import Agent, AgentError
from companion.models.base import ModelAPIError
from companion.collaboration.discord import DiscordError
from companion.tools.approval import ExecutionContext, execution_context
from pydantic import BaseModel, ConfigDict, model_validator


class ManagerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: str
    agent: str | None
    task: str | None
    answer: str | None

    @model_validator(mode="after")
    def validate_decision(self):
        if self.action == "delegate":
            if not self.agent or not self.task or not self.task.strip() or len(self.task) > 32000 or self.answer is not None:
                raise ValueError("Invalid delegation")
        elif self.action == "finish":
            if not self.answer or not self.answer.strip() or len(self.answer) > 32000 or self.agent is not None or self.task is not None:
                raise ValueError("Invalid final answer")
        else:
            raise ValueError("Unknown manager action")
        return self


class TaskBus(Protocol):
    async def publish(self, envelope: Envelope): ...
    async def poll(self) -> list[Envelope]: ...


class WorkflowError(RuntimeError):
    pass


def failure_message(role, error):
    message = f"{role} step failed ({type(error).__name__})"
    if isinstance(error, (AgentError, ModelAPIError, WorkflowError, DiscordError)):
        message += f": {error}"
    return message


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

    async def run(self, goal: str, *, target: str | None = None, owner: str = "terminal") -> WorkflowResult:
        if not goal.strip() or len(goal) > 32000:
            raise ValueError("Task goal must contain 1–32000 characters")
        try:
            operation = self._run(goal, owner) if self.config.workflow and target is None else self._flat(goal, target, owner)
            return await asyncio.wait_for(operation, timeout=self.config.task_timeout)
        except TimeoutError as exc:
            raise WorkflowError("Discord workflow timed out; the job was not resumed or retried") from exc

    async def _run(self, goal, owner):
        job_id = uuid4()
        if hasattr(self.bus, "begin"):
            await self.bus.begin(job_id)
        try:
            return await self._legacy(job_id, goal, owner)
        finally:
            if hasattr(self.bus, "end"):
                await self.bus.end(job_id)

    async def _legacy(self, job_id, goal, owner):
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
                    agent = self.agent[role] if isinstance(self.agent, dict) else self.agent
                    context_token = execution_context.set(ExecutionContext(owner, role, str(job_id)))
                    try:
                        output = await agent.run(spec.model_role, [
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
                            content=failure_message(role, exc),
                        ))
                    else:
                        await self.bus.publish(Envelope(
                            kind="result", job_id=job_id, task_id=expected_task, sender=role,
                            recipient=self.config.coordinator, step=step, expires_at=expires_at, content=content,
                        ))
                    finally:
                        execution_context.reset(context_token)
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

    async def _flat(self, goal, target, owner):
        if target is not None and target not in self.config.agents:
            raise ValueError(f"Unknown agent: {target}")
        job_id = uuid4()
        expires_at = time.time() + self.config.task_timeout
        results = {}
        executed = set()
        step = 0
        if hasattr(self.bus, "begin"):
            await self.bus.begin(job_id)

        async def exchange(role, task, *, structured=False):
            nonlocal step
            task_id = uuid4()
            sender = target or self.config.coordinator
            await self.bus.publish(Envelope(
                kind="task", job_id=job_id, task_id=task_id, sender=sender,
                recipient=role, step=step, expires_at=expires_at, content=task, context=dict(results),
            ))
            while True:
                messages = await self.bus.poll()
                if not messages:
                    await asyncio.sleep(self.config.poll_interval)
                    continue
                for message in messages:
                    if (message.job_id != job_id or message.task_id != task_id or message.step != step
                        or message.expires_at <= time.time()):
                        continue
                    if message.kind == "task":
                        if message.sender != sender or message.recipient != role or task_id in executed:
                            continue
                        executed.add(task_id)
                        spec = self.config.agents[role]
                        agent = self.agent[role] if isinstance(self.agent, dict) else self.agent
                        system = spec.prompt
                        options = {}
                        if structured:
                            catalog = {name: item.description or item.prompt for name, item in self.config.agents.items()
                                       if name != self.config.coordinator}
                            system += "\n你是主管。按需分派一个功能 Bot，或直接回答。不必调用所有 Bot。仅返回 action/agent/task/answer 四字段 JSON；delegate 时 answer=null；finish 时 agent/task=null。"
                            task = json.dumps({"goal": goal, "agents": catalog, "results": results,
                                               "remaining_dispatches": self.config.max_dispatches - len(results)}, ensure_ascii=False)
                            schema = ManagerDecision.model_json_schema()
                            schema["properties"]["action"] = {"type": "string", "enum": ["delegate", "finish"]}
                            options["response_format"] = {"type": "json_schema", "name": "manager_decision", "schema": schema, "strict": True}
                        context_token = execution_context.set(ExecutionContext(owner, role, str(job_id)))
                        try:
                            output = await agent.run(spec.model_role, [
                                {"role": "system", "content": system},
                                {"role": "user", "content": json.dumps({"task": task, "previous_results": results}, ensure_ascii=False)},
                            ], **options)
                            content = output.response.content
                            if not content.strip() or len(content) > 32000:
                                raise WorkflowError("Invalid or oversized agent output")
                        except Exception as exc:
                            await self.bus.publish(Envelope(
                                kind="error", job_id=job_id, task_id=task_id, sender=role,
                                recipient=sender, step=step, expires_at=expires_at,
                                content=failure_message(role, exc),
                            ))
                        else:
                            await self.bus.publish(Envelope(
                                kind="result", job_id=job_id, task_id=task_id, sender=role,
                                recipient=sender, step=step, expires_at=expires_at, content=content,
                            ))
                        finally:
                            execution_context.reset(context_token)
                    elif message.sender == role and message.recipient == sender and task_id in executed:
                        if message.kind == "error":
                            raise WorkflowError(message.content)
                        step += 1
                        return message.content

        try:
            if target is not None:
                answer = await exchange(target, goal)
                return WorkflowResult(str(job_id), answer, {target: answer})
            for _ in range(self.config.max_dispatches + 1):
                content = await exchange(self.config.coordinator, goal, structured=True)
                try:
                    decision = ManagerDecision.model_validate_json(content)
                except ValueError as exc:
                    raise WorkflowError("Manager returned an invalid decision") from exc
                if decision.action == "finish":
                    return WorkflowResult(str(job_id), decision.answer, results)
                if decision.agent == self.config.coordinator or decision.agent not in self.config.agents:
                    raise WorkflowError("Manager selected an unknown or recursive target")
                if len(results) >= self.config.max_dispatches:
                    raise WorkflowError("Manager dispatch budget exceeded")
                answer = await exchange(decision.agent, decision.task)
                results[f"{len(results) + 1}:{decision.agent}"] = answer
                if sum(map(len, results.values())) > self.config.context_limit:
                    raise WorkflowError("Workflow context budget exceeded")
            raise WorkflowError("Manager dispatch budget exceeded")
        finally:
            if hasattr(self.bus, "end"):
                await self.bus.end(job_id)
