"""Resolve project command arguments without constructing a shell command."""
import os
from pathlib import Path
import shutil
import sys

from companion.tools.registry import ToolError


def npm_launcher(name='npm'):
    """Find the JS entry point used by npm/npx, including Windows .cmd installs."""
    wrapper = shutil.which(name)
    node = shutil.which('node')
    entry = f'{name}-cli.js'
    candidates = []
    if wrapper:
        resolved = Path(wrapper).resolve()
        if resolved.name == entry:
            candidates.append(resolved)
        candidates.extend([Path(wrapper).parent / f'node_modules/npm/bin/{entry}',
                           Path(wrapper).parent.parent / f'lib/node_modules/npm/bin/{entry}'])
    if node:
        candidates.append(Path(node).parent / f'node_modules/npm/bin/{entry}')
    return next((str(path) for path in candidates if path.is_file()), None)


def resolve_command(command, cwd, checked_path, root):
    name, *arguments = command
    if not name.strip() or any('\x00' in part for part in command):
        raise ToolError('invalid_command', 'Command must name an executable and contain no NUL bytes')
    if name in {'python', 'python3'}:
        return [sys.executable, *arguments]
    if name.lower() in {'npm', 'npm.cmd', 'npx', 'npx.cmd'}:
        tool = name.lower().split('.')[0]
        node, launcher = shutil.which('node'), npm_launcher(tool)
        if not node or not launcher:
            raise ToolError('command_missing', f'Install Node.js with {tool} and restart the Bot; its JS launcher was not found')
        return [node, launcher, *arguments]
    # Relative executable paths (e.g. ./build/app) are scoped like file tools.
    # Absolute paths may name installed compilers outside the project.
    candidate = Path(name)
    if not candidate.is_absolute() and ('/' in name or '\\' in name):
        executable = checked_path(str((cwd / candidate).relative_to(root)))
        if not executable.is_file():
            raise ToolError('command_missing', f'Project executable not found: {name}')
        executable = str(executable)
    else:
        executable = shutil.which(name)
        if executable is None:
            raise ToolError('command_missing', f'Executable not found: {name}; install the required runtime/compiler and restart the Bot')
    if os.name == 'nt' and Path(executable).suffix.lower() in {'.bat', '.cmd'}:
        raise ToolError('batch_not_supported', 'Use the native executable or its script interpreter instead of a .bat/.cmd wrapper')
    return [executable, *arguments]
