import hashlib
import codecs
import os
from pathlib import Path
import tempfile
from uuid import uuid4

import httpx
from pydantic import Field

from companion.tools.approval import ApprovalManager
from companion.tools.registry import ToolArguments, ToolError
from companion.tools.web import public_url


class PathArguments(ToolArguments):
    path: str = Field(default=".", min_length=1, max_length=1024)


class DownloadArguments(ToolArguments):
    url: str = Field(min_length=1, max_length=4096)
    path: str = Field(min_length=1, max_length=1024)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class SystemFiles:
    def __init__(self, root: Path, approvals: ApprovalManager, domains: list[str], *, transport=None):
        self.root = root.resolve()
        if self.root == self.root.parent:
            raise ValueError("Use a dedicated system workspace, not a filesystem root")
        self.root.mkdir(parents=True, exist_ok=True)
        self.approvals = approvals
        self.domains = domains
        self.transport = transport

    def path(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute():
            raise ToolError("path_not_allowed", "Use a path relative to the system workspace")
        candidate = self.root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolError("path_not_allowed", "Path is outside the system workspace")
        for part in candidate.relative_to(self.root).parts:
            if part in (".git", ".aws", ".agents", ".codex", ".agent-internal") or part.startswith(".env"):
                raise ToolError("path_not_allowed", "Credential and control paths are protected")
        for part in (candidate, *candidate.parents):
            if part == self.root:
                break
            if part.is_symlink():
                raise ToolError("path_not_allowed", "Symlink paths are not supported")
        return resolved

    async def list_directory(self, args: PathArguments):
        directory = self.path(args.path)
        entries = [item for item in sorted(directory.iterdir(), key=lambda p: p.name)
                   if item.name not in (".agent-internal", ".git", ".aws", ".agents", ".codex") and not item.name.startswith(".env")]
        return {"entries": [{"name": item.name, "directory": item.is_dir()} for item in entries[:200]], "truncated": len(entries) > 200}

    async def read_text_file(self, args: PathArguments):
        path = self.path(args.path)
        if not path.is_file():
            raise ToolError("not_a_file", "Target must be a regular file")
        with path.open("rb") as file:
            data = file.read(32769)
        try:
            text = codecs.getincrementaldecoder("utf-8")().decode(data[:32768], final=len(data) <= 32768)
        except UnicodeDecodeError as exc:
            raise ToolError("not_text", "File must be UTF-8 text") from exc
        return {"path": str(path.relative_to(self.root)), "text": text, "truncated": len(data) > 32768}

    async def delete_file(self, args: PathArguments):
        path = self.path(args.path)
        if not path.is_file():
            raise ToolError("not_a_file", "Only individual regular files can be deleted")
        before = path.stat()
        fingerprint = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        request = await self.approvals.require("delete_file", {
            "path": str(path), "size_bytes": before.st_size, "mode": "move_to_app_trash",
        })
        try:
            current = self.path(args.path).stat()
            if fingerprint != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
                raise ToolError("target_changed", "File changed after the approval preview; request a new approval")
            trash = self.root / ".agent-internal" / "trash" / str(uuid4())
            trash.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, trash)
            self.approvals.audit(request, "completed")
            return {"status": "deleted", "original_path": args.path, "restore_id": trash.name, "recoverable": True}
        except BaseException:
            self.approvals.audit(request, "failed")
            raise

    async def download_file(self, args: DownloadArguments):
        path = self.path(args.path)
        if path.exists() or not path.parent.is_dir():
            raise ToolError("invalid_destination", "Destination must be a new file in an existing directory")
        url = public_url(args.url, self.domains)
        async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
            for _ in range(4):
                head = await client.head(url)
                if not head.is_redirect:
                    break
                url = public_url(str(url.join(head.headers["Location"])), self.domains)
            else:
                raise ToolError("redirect_limit", "Too many download redirects")
            if head.status_code not in (200, 405):
                raise ToolError("download_unavailable", f"Download preview returned HTTP {head.status_code}")
            request = await self.approvals.require("download_file", {
                "url": str(url), "path": str(path), "max_bytes": 10 * 1024 * 1024,
                "reported_size": head.headers.get("Content-Length"), "sha256": args.sha256,
            })
            temporary = None
            try:
                self.path(args.path)
                staging = self.root / ".agent-internal" / "staging"
                staging.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(dir=staging)
                temporary = Path(name)
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(descriptor, "wb") as file:
                    async with client.stream("GET", url) as response:
                        if response.status_code != 200:
                            raise ToolError("download_unavailable", "Approved URL no longer returns a direct download")
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 10 * 1024 * 1024:
                                raise ToolError("download_too_large", "Download exceeds 10 MiB")
                            file.write(chunk)
                            digest.update(chunk)
                if args.sha256 and digest.hexdigest() != args.sha256.lower():
                    raise ToolError("checksum_mismatch", "Download does not match the supplied SHA-256")
                target = self.path(args.path)
                os.link(temporary, target)  # Atomic publication without overwriting an existing file.
                self.approvals.audit(request, "completed")
                return {"status": "downloaded", "path": args.path, "bytes": size, "sha256": digest.hexdigest()}
            except BaseException:
                self.approvals.audit(request, "failed")
                raise
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
