"""Workspace-scoped edits and bounded project command execution."""
import asyncio
import difflib
import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import sys
import tempfile

from pydantic import Field

from companion.tools.approval import execution_context
from companion.tools.files import PathArguments, SystemFiles
from companion.tools.registry import ToolError
from companion.tools.project_commands import resolve_command


class WriteArguments(PathArguments):
    content: str = Field(max_length=32000)
    expected_sha256: str | None = Field(default=None, pattern=r'^[0-9a-fA-F]{64}$')


class EditArguments(PathArguments):
    old_text: str = Field(min_length=1, max_length=32000)
    new_text: str = Field(max_length=32000)
    expected_sha256: str = Field(pattern=r'^[0-9a-fA-F]{64}$')


class TestArguments(PathArguments):
    timeout_seconds: int = Field(default=30, ge=1, le=60)


class CommandArguments(TestArguments):
    command: list[str] = Field(min_length=1, max_length=64)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class CodingFiles(SystemFiles):
    def __init__(self, root, approvals):
        super().__init__(root, approvals, [])
        self.mutation_lock = asyncio.Lock()

    async def workspace_info(self, _):
        return {'workspace': str(self.root), 'paths': 'relative',
                'approval_mode': self.approvals.policies.get(execution_context.get().role, 'manual'),
                'test_runner': 'pytest', 'pytest_available': importlib.util.find_spec('pytest') is not None,
                'project_commands': True, 'command_format': 'executable and literal arguments, not a shell string'}

    def contents(self, path):
        if not path.is_file():
            raise ToolError('not_a_file', 'Target must be a regular file')
        with path.open('rb') as file:
            data = file.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ToolError('file_too_large', 'Project text tools support files up to 1 MiB')
        try:
            return data, data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ToolError('not_text', 'Project files must be UTF-8 text') from exc

    async def read_project_file(self, args):
        path = self.path(args.path)
        data, _ = self.contents(path)
        result = await self.read_text_file(args)
        result.update({'sha256': sha(data), 'size_bytes': len(data)})
        return result

    async def write_project_file(self, args: WriteArguments):
        path = self.path(args.path)
        before, old = self.contents(path) if path.exists() else (None, '')
        if before is not None and args.expected_sha256 is None:
            raise ToolError('version_required', 'Read the existing file and provide its SHA-256 before replacing it')
        if args.expected_sha256 is not None and (before is None or sha(before) != args.expected_sha256.lower()):
            raise ToolError('version_conflict', 'File does not match the supplied SHA-256; read it again')
        return await self.publish(args.path, args.content, before, old)

    async def apply_text_edit(self, args: EditArguments):
        path = self.path(args.path)
        before, old = self.contents(path)
        if sha(before) != args.expected_sha256.lower():
            raise ToolError('version_conflict', 'File changed; read it again before editing')
        if old.count(args.old_text) != 1:
            raise ToolError('ambiguous_edit', 'old_text must match exactly once in the current file')
        return await self.publish(args.path, old.replace(args.old_text, args.new_text, 1), before, old)

    async def publish(self, relative, text, before, old):
        async with self.mutation_lock:
            return await self.publish_locked(relative, text, before, old)

    async def publish_locked(self, relative, text, before, old):
        encoding = 'utf-8-sig' if before and before.startswith(b'\xef\xbb\xbf') else 'utf-8'
        data = text.encode(encoding)
        if len(data) > 1024 * 1024:
            raise ToolError('file_too_large', 'Edited files must stay within 1 MiB')
        if before == data:
            path = self.path(relative)
            if not path.exists() or self.contents(path)[0] != before:
                raise ToolError('version_conflict', 'File changed while waiting for workspace access')
            return {'status': 'unchanged', 'path': relative, 'sha256': sha(data)}
        path = self.path(relative)
        parts = difflib.unified_diff(old.splitlines(True), text.splitlines(True), fromfile=relative, tofile=relative)
        diff = ''.join(part if part.endswith('\n') else part + '\n\\ No newline at end of file\n' for part in parts)
        # Large previews must fail rather than ask approval for a hidden change.
        if len(diff) > 64000:
            raise ToolError('diff_too_large', 'Use smaller targeted edits so the change can be reviewed')
        request = await self.approvals.require('write_project_file', {
            'workspace': str(self.root), 'path': str(path), 'new_file': before is None,
            'before_sha256': sha(before) if before is not None else None,
            'after_sha256': sha(data), 'diff': diff, 'create_parent_directories': True,
        })
        temporary = None
        try:
            path = self.path(relative)
            current = self.contents(path)[0] if path.exists() else None
            if current != before:
                raise ToolError('version_conflict', 'File changed while the operation was pending')
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = self.root / '.agent-internal' / 'staging'
            staging.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=staging)
            temporary = Path(name)
            with os.fdopen(descriptor, 'wb') as file:
                file.write(data)
            path = self.path(relative)
            if before is None:
                os.link(temporary, path)  # New files never overwrite a raced destination.
            else:
                os.chmod(temporary, path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            self.approvals.audit(request, 'completed')
            return {'status': 'written', 'path': relative, 'sha256': sha(data),
                    'diff': diff[:16000], 'diff_truncated': len(diff) > 16000}
        except BaseException:
            self.approvals.audit(request, 'failed')
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def run_python_tests(self, args: TestArguments):
        async with self.mutation_lock:
            target = self.path(args.path)
            if not target.is_dir() and not (target.is_file() and target.suffix == '.py'):
                raise ToolError('invalid_test_target', 'Select a directory or a Python test file in the project workspace')
            if importlib.util.find_spec('pytest') is None:
                raise ToolError('pytest_missing', 'Install the project dev dependencies to enable pytest')
            command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--', str(target)]
            return await self.execute_locked(args, command, self.root, 'run_python_tests')

    async def run_project_command(self, args: CommandArguments):
        async with self.mutation_lock:
            cwd = self.path(args.path)
            if not cwd.is_dir():
                raise ToolError('invalid_command_directory', 'Select a working directory inside CODING_WORKSPACE')
            if sum(len(part) for part in args.command) > 16000:
                raise ToolError('command_too_large', 'Command arguments must fit within 16000 characters')
            command = resolve_command(args.command, cwd, self.path, self.root)
            return await self.execute_locked(args, command, cwd, 'run_project_command')

    async def execute_locked(self, args, command, cwd, operation):
        request = await self.approvals.require(operation, {
            'workspace': str(self.root), 'cwd': str(cwd),
            'command': command, 'timeout_seconds': args.timeout_seconds,
            'executes_project_code': True, 'output_limit_bytes': 65536,
        })
        # Do not forward API keys, Bot tokens, proxy bindings or the caller's
        # Python startup settings into generated test code.
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL',
                   'COMSPEC', 'PATHEXT', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
                   'JAVA_HOME', 'JDK_HOME', 'GOROOT', 'GOPATH', 'GOCACHE', 'GOMODCACHE',
                   'CARGO_HOME', 'RUSTUP_HOME', 'DOTNET_ROOT', 'DOTNET_ROOT_X64',
                   'DOTNET_CLI_HOME', 'NUGET_PACKAGES', 'NPM_CONFIG_PREFIX'}
        environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
        environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                           PYTHONPATH=os.pathsep.join([str(self.root / 'src'), str(self.root)]), CI='true')
        process = None
        output = bytearray()
        timed_out = truncated = False
        try:
            self.path(args.path)
            self.path(str(cwd.relative_to(self.root)))
            process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=environment,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name != 'nt')
            async def collect():
                nonlocal truncated
                while chunk := await process.stdout.read(4096):
                    exceeds_limit = len(output) + len(chunk) > 65536
                    output.extend(chunk[:65536 - len(output)])
                    if exceeds_limit:
                        truncated = True
                        return
                await process.wait()
            try:
                await asyncio.wait_for(collect(), timeout=args.timeout_seconds)
            except TimeoutError:
                timed_out = True
            await self.stop(process)
            self.approvals.audit(request, 'completed')
            decoded = output.decode('utf-8', errors='replace')
            result = {'command': command, 'cwd': str(cwd), 'exit_code': process.returncode,
                      'success': process.returncode == 0 and not timed_out and not truncated,
                      'timed_out': timed_out, 'output_truncated': truncated, 'output': decoded}
            if operation == 'run_python_tests':
                result['passed'] = result['success']
            return result
        except BaseException:
            if process is not None:
                await self.stop(process)
            self.approvals.audit(request, 'failed')
            raise

    async def stop(self, process):
        try:
            if os.name == 'nt':
                if process.returncode is None:
                    killer = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
                    if killer.is_file():
                        cleanup = await asyncio.create_subprocess_exec(str(killer), '/PID', str(process.pid), '/T', '/F',
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                        try:
                            await asyncio.wait_for(cleanup.wait(), timeout=5)
                        except TimeoutError:
                            try:
                                cleanup.kill()
                            except ProcessLookupError:
                                pass
                            await cleanup.wait()
                    if process.returncode is None:
                        process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
