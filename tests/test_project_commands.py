from pathlib import Path
from types import SimpleNamespace

import pytest

from companion.tools import project_commands
from companion.tools.registry import ToolError


@pytest.mark.parametrize('name', ['npm', 'npx'])
def test_windows_node_wrappers_resolve_to_js_without_shell(tmp_path, monkeypatch, name):
    install = tmp_path / 'Program Files/nodejs'
    install.mkdir(parents=True)
    node = install / 'node.exe'
    node.touch()
    wrapper = install / f'{name}.cmd'
    wrapper.touch()
    entry = install / f'node_modules/npm/bin/{name}-cli.js'
    entry.parent.mkdir(parents=True)
    entry.touch()
    monkeypatch.setattr(project_commands.shutil, 'which', lambda tool: str(node) if tool == 'node' else str(wrapper) if tool == name else None)
    command = project_commands.resolve_command([f'{name}.cmd', 'test'], tmp_path, lambda p: tmp_path / p, tmp_path)
    assert command == [str(node), str(entry), 'test']


def test_missing_npm_entry_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(project_commands.shutil, 'which', lambda name: None)
    with pytest.raises(ToolError, match='Install Node.js'):
        project_commands.resolve_command(['npm', 'test'], tmp_path, lambda p: tmp_path / p, tmp_path)


def test_other_windows_batch_wrappers_require_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(project_commands, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(project_commands.shutil, 'which', lambda name: str(tmp_path / 'gradle.cmd'))
    with pytest.raises(ToolError, match='native executable'):
        project_commands.resolve_command(['gradle', 'test'], tmp_path, lambda p: tmp_path / p, tmp_path)
