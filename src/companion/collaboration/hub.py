"""Single Discord reader multiplexes jobs and handles human approvals independently."""
import asyncio
import re

from companion.collaboration.discord import DiscordError, OperatorMessage
from companion.collaboration.protocol import Envelope
from companion.collaboration.workflow import WorkflowError, WorkflowRunner


class JobMailbox:
    def __init__(self, hub):
        self.hub = hub
        self.job_id = None
        self.queue = asyncio.Queue(maxsize=256)

    async def begin(self, job_id):
        self.job_id = job_id
        self.hub.mailboxes[job_id] = self.queue

    async def end(self, job_id):
        self.hub.mailboxes.pop(job_id, None)

    async def publish(self, envelope):
        await self.hub.bus.publish(envelope)

    async def poll(self):
        result = [await self.queue.get()]
        while not self.queue.empty():
            result.append(self.queue.get_nowait())
        return result


class DiscordHub:
    def __init__(self, config, bus, agents, approvals, *, max_active_jobs=3):
        self.config = config
        self.bus = bus
        self.agents = agents
        self.approvals = approvals
        self.max_active_jobs = max_active_jobs
        self.mailboxes = {}
        self.tasks = set()
        approvals.notifier = self.notify_approval

    async def notify_approval(self, request):
        bot_id = self.bus.bot_ids[request["role"]]
        details = request["details"]
        preview = "\n".join(f"{name}: {value}" for name, value in details.items()
                            if name != "diff" and not isinstance(value, (dict, list)))
        if details.get("command"):
            preview += "\n命令：" + " ".join(details["command"])
        if details.get("diff"):
            preview += "\n修改预览：\n" + details["diff"]
        await self.bus.send_notice(request["role"],
            "需要发起用户批准（有效期 " + str(self.approvals.timeout) + " 秒）：\n"
            + f"操作：{request['action']}\n发起用户：{request['owner']}\n请求 ID：{request['request_id']}\n"
            + preview
            + f"\n批准：<@{bot_id}> approve {request['request_id']}"
            + f"\n拒绝：<@{bot_id}> deny {request['request_id']}")

    async def handle_event(self, event):
        if isinstance(event, Envelope):
            queue = self.mailboxes.get(event.job_id)
            if queue is not None:
                queue.put_nowait(event)
            return
        if not isinstance(event, OperatorMessage):
            return
        # Only DiscordBus can turn an authenticated, allowlisted human message
        # into OperatorMessage. Model and bot text never reaches this branch.
        approval = re.fullmatch(r"(approve|deny)\s+([0-9a-fA-F-]{36})", event.text)
        if approval:
            accepted = self.approvals.resolve(approval[2].lower(), event.author_id, approval[1] == "approve")
            await self.bus.send_notice(event.role, "已记录你的决定。" if accepted else "批准请求不存在、已失效，或不是由你发起。")
            return
        if event.text.split()[0].lower() in ("approve", "deny"):
            await self.bus.send_notice(event.role, "格式：@Bot approve 请求ID，或 @Bot deny 请求ID。")
            return
        if len(self.tasks) >= self.max_active_jobs:
            await self.bus.send_notice(event.role, "当前任务已达并发上限，请稍后重试。")
            return
        task = asyncio.create_task(self.run_job(event))
        self.tasks.add(task)
        task.add_done_callback(self.task_done)

    def task_done(self, task):
        self.tasks.discard(task)
        # Retrieve exceptions even if posting an error notice also failed.
        if not task.cancelled():
            error = task.exception()
            if error:
                detail = str(error) if isinstance(error, (DiscordError, WorkflowError)) else type(error).__name__
                print(f"Discord job stopped: {detail}")

    async def run_job(self, event):
        try:
            await self.bus.send_notice(event.role, "已收到，正在处理你的任务。")
            target = None if event.role == self.config.coordinator else event.role
            result = await WorkflowRunner(self.config, JobMailbox(self), self.agents).run(
                event.text, target=target, owner=event.author_id,
            )
            await self.bus.send_notice(event.role, f"任务 {result.job_id}\n{result.content}")
        except Exception as exc:
            detail = str(exc) if isinstance(exc, (WorkflowError, DiscordError)) else type(exc).__name__
            await self.bus.send_notice(event.role, f"任务停止：{detail}。未自动重试。")

    async def tick(self):
        for event in await self.bus.poll_events():
            await self.handle_event(event)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.mailboxes.clear()

    async def serve(self):
        try:
            while True:
                await self.tick()
                await asyncio.sleep(self.config.poll_interval)
        finally:
            await self.close()
