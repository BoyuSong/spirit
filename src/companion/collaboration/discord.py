import asyncio
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
import time

import httpx
from pydantic import ValidationError

from companion.collaboration.config import CollaborationConfig
from companion.collaboration.protocol import Envelope, MAX_BYTES, PREFIX


class DiscordError(RuntimeError):
    pass


@dataclass
class OperatorMessage:
    role: str
    text: str
    author_id: str


class BotClient:
    def __init__(self, token_env: str, *, transport=None):
        self.token_env = token_env
        self.http = httpx.AsyncClient(
            timeout=20, transport=transport,
            headers={"User-Agent": "DiscordBot (https://github.com/BoyuSong/spirit, 0.1.0)"},
        )
        self._lock = asyncio.Lock()
        self._ready_at = 0.0

    async def request(self, method: str, path: str, **kwargs):
        token = os.environ.get(self.token_env)
        if not token:
            raise DiscordError(f"Missing bot credential: {self.token_env}")
        async with self._lock:
            for attempt in range(4):
                await asyncio.sleep(max(0, self._ready_at - time.monotonic()))
                response = await self.http.request(
                    method, f"https://discord.com/api/v10{path}",
                    headers={"Authorization": f"Bot {token}"}, **kwargs,
                )
                if response.status_code == 429:
                    try:
                        delay = float(response.json()["retry_after"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise DiscordError("Invalid Discord rate-limit response") from exc
                    if not math.isfinite(delay) or delay < 0 or delay > 60 or attempt == 3:
                        raise DiscordError("Discord rate limit prevents further requests; retry later")
                    self._ready_at = time.monotonic() + delay
                    continue
                if response.is_error or response.is_redirect:
                    # Report only the numeric API code, never response bodies,
                    # request headers or credential values.
                    code = None
                    try:
                        body = response.json()
                        if isinstance(body, dict) and type(body.get("code")) is int:
                            code = body["code"]
                    except ValueError:
                        pass
                    detail = f", Discord code {code}" if code is not None else ""
                    endpoint = path.split("?", 1)[0]
                    hint = ""
                    if endpoint.startswith("/channels/") and response.status_code in (403, 404):
                        hint = "; check the configured channel ID, bot membership, and channel View Channel/Read Message History permissions"
                    raise DiscordError(
                        f"Discord request failed for {self.token_env}: {method} {endpoint} "
                        f"(HTTP {response.status_code}{detail}){hint}"
                    )
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    try:
                        delay = float(response.headers.get("X-RateLimit-Reset-After", "0"))
                    except ValueError as exc:
                        raise DiscordError("Invalid Discord rate-limit headers") from exc
                    if not math.isfinite(delay) or not 0 <= delay <= 60:
                        raise DiscordError("Discord rate-limit reset exceeds the supported wait")
                    self._ready_at = time.monotonic() + delay
                return response.json()
        raise DiscordError("Discord request retry limit reached")

    async def download(self, url: str) -> bytes:
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or parsed.host != "cdn.discordapp.com" or parsed.userinfo:
            raise DiscordError("Task attachments must come from the Discord CDN")
        data = bytearray()
        # No bot Authorization header is sent to the public attachment CDN.
        async with self.http.stream("GET", url) as response:
            if response.status_code != 200:
                raise DiscordError(f"Discord attachment download failed (HTTP {response.status_code})")
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise DiscordError("Discord task attachment exceeds 512 KiB")
        return bytes(data)

    async def close(self):
        await self.http.aclose()


class DiscordBus:
    """One registered bot identity per role, with one selected bot reading the bus."""

    def __init__(self, config: CollaborationConfig, *, transport=None, observer=None):
        self.config = config
        self.clients = {
            role: BotClient(spec.token_env, transport=transport)
            for role, spec in config.agents.items()
        }
        self.bot_ids: dict[str, str] = {}
        self.channel_id = ""
        self.cursor = "0"
        self.observer = observer or config.coordinator
        self.operator_ids = set()

    async def initialize(self, *, active_roles=None, require_operators=False):
        roles = set(active_roles or self.config.agents)
        if self.observer not in roles or not roles.issubset(self.config.agents):
            raise DiscordError("Observer and active roles must be registered")
        raw_operators = os.environ.get(self.config.operator_env, "")
        self.operator_ids = {part.strip() for part in raw_operators.split(",") if part.strip()}
        if any(not value.isdigit() or int(value) <= 0 for value in self.operator_ids):
            raise DiscordError(f"Invalid operator IDs in {self.config.operator_env}")
        if require_operators and not self.operator_ids:
            raise DiscordError(f"Configure authorized human IDs in {self.config.operator_env}")
        missing = [self.config.agents[role].token_env for role in roles if not os.environ.get(self.config.agents[role].token_env)]
        channel = os.environ.get(self.config.channel_env, "")
        if missing or not channel.isdigit() or int(channel) <= 0:
            names = missing + ([self.config.channel_env] if not channel.isdigit() or int(channel) <= 0 else [])
            raise DiscordError(f"Configure environment settings: {', '.join(names)}")
        self.channel_id = channel
        self.bot_ids = {}
        for role, client in self.clients.items():
            if role not in roles:
                continue
            identity = await client.request("GET", "/users/@me")
            if not identity.get("bot") or not str(identity.get("id", "")).isdigit():
                raise DiscordError(f"{role} must use a genuine Discord bot account")
            self.bot_ids[role] = str(identity["id"])
        if len(set(self.bot_ids.values())) != len(self.bot_ids):
            raise DiscordError("Each model role must use a different Discord bot")
        application = await self.clients[self.observer].request("GET", "/applications/@me")
        if not int(application.get("flags", 0)) & ((1 << 18) | (1 << 19)):
            raise DiscordError(f"Enable Message Content Intent for the {self.observer} bot in Discord Developer Portal")
        # Ignore old jobs at startup; this runner does not resume interrupted jobs.
        latest = await self.clients[self.observer].request(
            "GET", f"/channels/{self.channel_id}/messages", params={"limit": 1},
        )
        if latest:
            self.cursor = str(max(int(message["id"]) for message in latest))

    async def publish(self, envelope: Envelope):
        if envelope.sender not in self.bot_ids or envelope.recipient not in self.bot_ids:
            raise DiscordError("Message roles must be registered before sending")
        data = envelope.encode()
        content = PREFIX + data.decode("utf-8")
        # Stable per-author nonce suppresses duplicate publication of the same event.
        nonce = hashlib.sha256(f"{envelope.job_id}:{envelope.task_id}:{envelope.kind}".encode()).hexdigest()[:24]
        payload = {"allowed_mentions": {"parse": []}, "nonce": nonce, "enforce_nonce": True}
        client = self.clients[envelope.sender]
        path = f"/channels/{self.channel_id}/messages"
        # Discord counts UTF-16 code units, so emoji consume two units.
        if len(content.encode("utf-16-le")) // 2 <= 2000:
            payload["content"] = content
            await client.request("POST", path, json=payload)
        else:
            payload["content"] = PREFIX + f"{envelope.sender} → {envelope.recipient}: {envelope.kind} {envelope.task_id}"
            payload["attachments"] = [{"id": 0, "filename": "agent-task.json"}]
            await client.request(
                "POST", path, data={"payload_json": json.dumps(payload)},
                files={"files[0]": ("agent-task.json", data, "application/json")},
            )

    async def send_notice(self, role: str, content: str):
        payload = {"allowed_mentions": {"parse": []}, "nonce": uuid_nonce(), "enforce_nonce": True}
        path = f"/channels/{self.channel_id}/messages"
        if len(content.encode("utf-16-le")) // 2 <= 2000:
            await self.clients[role].request("POST", path, json={**payload, "content": content})
        else:
            data = content.encode("utf-8")
            if len(data) > MAX_BYTES:
                raise DiscordError("Notice exceeds the attachment budget")
            await self.clients[role].request("POST", path,
                data={"payload_json": json.dumps({**payload, "content": "详细结果见附件。",
                    "attachments": [{"id": 0, "filename": "agent-result.txt"}]})},
                files={"files[0]": ("agent-result.txt", data, "text/plain; charset=utf-8")})

    async def poll(self) -> list[Envelope]:
        return [event for event in await self.poll_events() if isinstance(event, Envelope)]

    async def poll_events(self) -> list[Envelope | OperatorMessage]:
        client = self.clients[self.observer]
        messages = await client.request(
            "GET", f"/channels/{self.channel_id}/messages", params={"after": self.cursor, "limit": 100},
        )
        envelopes = []
        for message in sorted(messages, key=lambda item: int(item["id"])):
            message_id = str(message["id"])
            if int(message_id) <= int(self.cursor):
                continue
            author = str(message.get("author", {}).get("id", ""))
            content = message.get("content", "")
            if author in self.operator_ids and not message.get("author", {}).get("bot", False) and author not in self.bot_ids.values():
                match = re.fullmatch(r"<@!?(\d+)>\s+([\s\S]{1,32000})", content.strip())
                if match:
                    role = next((name for name, bot_id in self.bot_ids.items() if bot_id == match[1]), None)
                    if role and match[2].strip():
                        envelopes.append(OperatorMessage(role, match[2].strip(), author))
                self.cursor = message_id
                continue
            if author not in self.bot_ids.values():
                self.cursor = message_id
                continue
            if not content.startswith(PREFIX):
                self.cursor = message_id
                continue
            attachments = [item for item in message.get("attachments", []) if item.get("filename") == "agent-task.json"]
            try:
                if attachments:
                    attachment = attachments[0]
                    if attachment.get("size", MAX_BYTES + 1) > MAX_BYTES:
                        self.cursor = message_id
                        continue
                    data = await client.download(attachment["url"])
                else:
                    data = content[len(PREFIX):].encode("utf-8")
                envelope = Envelope.decode(data)
            except (ValueError, ValidationError):
                self.cursor = message_id
                continue
            if (
                self.bot_ids.get(envelope.sender) == author
                and envelope.recipient in self.bot_ids
                and envelope.expires_at > time.time()
            ):
                envelopes.append(envelope)
            self.cursor = message_id
        return envelopes

    async def close(self):
        for client in self.clients.values():
            await client.close()


def uuid_nonce():
    from uuid import uuid4
    return uuid4().hex[:24]
