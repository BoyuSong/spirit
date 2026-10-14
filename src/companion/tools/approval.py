import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import json
from pathlib import Path
import time
import sys
from uuid import uuid4

from companion.tools.registry import ToolError


@dataclass
class ExecutionContext:
    owner: str = "terminal"
    role: str = "system"
    job_id: str = ""


execution_context = ContextVar("execution_context", default=ExecutionContext())


class ApprovalManager:
    """Apply configured policies; manual decisions come from the initiating human."""
    def __init__(self, audit_path: Path, *, terminal=True, notifier=None, timeout=300, confirmer=None, policies=None):
        self.audit_path = audit_path
        self.terminal = terminal
        self.notifier = notifier
        self.timeout = timeout
        self.pending = {}
        self.confirmer = confirmer
        self.denied = set()
        self.policies = dict(policies or {})

    async def confirm_terminal(self, request):
        print(json.dumps(request, ensure_ascii=False, indent=2))
        if not sys.stdin.isatty():
            return False
        print("Approve this operation? [y/N] ", end="", flush=True)
        if sys.platform == "win32":
            import msvcrt
            answer = ""
            while True:
                if msvcrt.kbhit():
                    char = msvcrt.getwche()
                    if char in ("\r", "\n"):
                        return answer.strip().lower() == "y"
                    answer += char
                await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def read_line():
            if not future.done():
                future.set_result(sys.stdin.readline().strip().lower() == "y")
        loop.add_reader(sys.stdin.fileno(), read_line)
        try:
            return await future
        finally:
            loop.remove_reader(sys.stdin.fileno())

    def audit(self, request, status):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({**request, "status": status, "at": time.time()}, ensure_ascii=False) + "\n")

    async def require(self, action: str, details: dict) -> dict:
        context = execution_context.get()
        denial_key = (context.job_id, action, json.dumps(details, sort_keys=True))
        if denial_key in self.denied:
            raise ToolError("approval_denied", "This action was already declined in this job")
        request = {"request_id": str(uuid4()), "owner": context.owner, "role": context.role,
                   "action": action, "details": details, "job_id": context.job_id,
                   "approval_mode": self.policies.get(context.role, "manual")}
        self.audit(request, "requested")
        approved = False
        try:
            if request["approval_mode"] == "automatic":
                approved = True
            elif self.terminal:
                approved = await asyncio.wait_for((self.confirmer or self.confirm_terminal)(request), timeout=self.timeout)
            else:
                future = asyncio.get_running_loop().create_future()
                self.pending[request["request_id"]] = (request, future)
                try:
                    await self.notifier(request)
                    approved = await asyncio.wait_for(future, timeout=self.timeout)
                finally:
                    self.pending.pop(request["request_id"], None)
        except BaseException:
            self.audit(request, "cancelled")
            raise
        self.audit(request, "approved" if approved else "denied")
        if not approved:
            self.denied.add(denial_key)
            raise ToolError("approval_denied", "User declined this operation")
        return request

    def resolve(self, request_id: str, author_id: str, approved: bool) -> bool:
        pending = self.pending.get(request_id)
        if pending is None or pending[0]["owner"] != author_id or pending[1].done():
            return False
        pending[1].set_result(approved)
        return True
