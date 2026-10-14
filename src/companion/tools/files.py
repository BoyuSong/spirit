import hashlib
import codecs
import os
from pathlib import Path
import tempfile
from uuid import UUID
import json
import time

import httpx
from pydantic import Field

from companion.tools.approval import ApprovalManager, execution_context
from companion.tools.registry import ToolArguments, ToolError
from companion.tools.web import public_url


class PathArguments(ToolArguments):
    path: str = Field(default=".", min_length=1, max_length=1024)


class ReadArguments(PathArguments):
    offset: int = Field(default=0, ge=0, le=1024 * 1024)


class RestoreArguments(ToolArguments):
    restore_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")


PROTECTED = {".git", ".aws", ".ssh", ".gnupg", ".agents", ".codex", ".agent-internal", ".venv", "venv", "node_modules", "__pycache__"}


def protected(part):
    return part.casefold() in PROTECTED or part.casefold().startswith(".env")


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
        for part in (*candidate.relative_to(self.root).parts, *resolved.relative_to(self.root).parts):
            if protected(part):
                raise ToolError("path_not_allowed", "Credential and control paths are protected")
            if ":" in part or part.rstrip(". ") != part or part.split(".")[0].upper() in {
                "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
            }:
                raise ToolError("path_not_allowed", "Device names and alternate data streams are not supported")
        for part in (candidate, *candidate.parents):
            if part == self.root:
                break
            if part.is_symlink() or part.is_junction():
                raise ToolError("path_not_allowed", "Symlink paths are not supported")
        return resolved

    async def workspace_info(self, _):
        return {"workspace": str(self.root), "paths": "relative", "approval_mode": self.approvals.policies.get(execution_context.get().role, "manual")}

    async def make_directory(self, args: PathArguments):
        path = self.path(args.path)
        if path.is_dir():
            return {"status": "unchanged", "path": args.path}
        if path.exists():
            raise ToolError("invalid_destination", "Directory destination is already a file")
        request = await self.approvals.require("make_directory", {"path": str(path), "parents": True})
        try:
            self.path(args.path).mkdir(parents=True, exist_ok=True)
            self.approvals.audit(request, "completed")
            return {"status": "created", "path": args.path}
        except BaseException:
            self.approvals.audit(request, "failed")
            raise

    async def list_directory(self, args: PathArguments):
        directory = self.path(args.path)
        entries = [item for item in sorted(directory.iterdir(), key=lambda p: p.name)
                   if not protected(item.name)]
        return {"entries": [{"name": item.name, "directory": item.is_dir()} for item in entries[:200]], "truncated": len(entries) > 200}

    async def read_text_file(self, args: ReadArguments):
        path = self.path(args.path)
        if not path.is_file():
            raise ToolError("not_a_file", "Target must be a regular file")
        with path.open("rb") as file:
            offset = getattr(args, "offset", 0)
            file.seek(offset)
            data = file.read(32769)
        try:
            decoder = codecs.getincrementaldecoder("utf-8-sig" if offset == 0 else "utf-8")()
            text = decoder.decode(data[:32768], final=len(data) <= 32768)
        except UnicodeDecodeError as exc:
            raise ToolError("not_text", "File must be UTF-8 text") from exc
        consumed = min(len(data), 32768) - len(decoder.getstate()[0])
        return {"path": str(path.relative_to(self.root)), "text": text, "truncated": len(data) > 32768,
                "offset": offset, "next_offset": offset + consumed if len(data) > 32768 else None}

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
            trash = self.root / ".agent-internal" / "trash" / request["request_id"]
            trash.parent.mkdir(parents=True, exist_ok=True)
            metadata = trash.with_suffix(".json")
            metadata.write_text(json.dumps({"path": str(path.relative_to(self.root)), "size_bytes": before.st_size,
                                           "deleted_at": time.time()}), encoding="utf-8")
            os.replace(path, trash)
            self.approvals.audit(request, "completed")
            return {"status": "deleted", "original_path": args.path, "restore_id": trash.name, "recoverable": True}
        except BaseException:
            self.approvals.audit(request, "failed")
            raise

    async def list_recoverable_files(self, _):
        directory = self.root / ".agent-internal" / "trash"
        entries = []
        if not directory.is_dir():
            return {"entries": entries, "truncated": False}
        for metadata_path in sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True):
            try:
                restore_id = str(UUID(metadata_path.stem))
                trash = directory / restore_id
                if metadata_path.is_symlink() or trash.is_symlink() or not trash.is_file():
                    continue
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.path(metadata["path"])
                entries.append({"restore_id": restore_id, "path": metadata["path"], "size_bytes": trash.stat().st_size,
                                "deleted_at": metadata.get("deleted_at")})
                if len(entries) > 200:
                    break
            except (ValueError, OSError, ToolError, KeyError, TypeError):
                continue
        return {"entries": entries[:200], "truncated": len(entries) > 200}

    async def restore_file(self, args: RestoreArguments):
        try:
            restore_id = str(UUID(args.restore_id))
        except ValueError as exc:
            raise ToolError("invalid_restore_id", "Use a restore ID returned by delete_file") from exc
        trash = self.root / ".agent-internal" / "trash" / restore_id
        metadata_path = trash.with_suffix(".json")
        if not trash.is_file() or trash.is_symlink() or not metadata_path.is_file() or metadata_path.is_symlink():
            raise ToolError("restore_unavailable", "Recoverable file or its metadata is missing")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        target = self.path(metadata["path"])
        if target.exists() or not target.parent.is_dir():
            raise ToolError("invalid_destination", "Original destination must be absent and its directory must exist")
        before = trash.stat()
        request = await self.approvals.require("restore_file", {
            "restore_id": restore_id, "path": str(target), "size_bytes": before.st_size,
        })
        try:
            current = trash.stat()
            if trash.is_symlink() or (before.st_ino, before.st_size, before.st_mtime_ns) != (current.st_ino, current.st_size, current.st_mtime_ns):
                raise ToolError("target_changed", "Recoverable file changed after the request")
            target = self.path(metadata["path"])
            os.link(trash, target)
            trash.unlink()
            metadata_path.unlink()
            self.approvals.audit(request, "completed")
            return {"status": "restored", "path": metadata["path"]}
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
