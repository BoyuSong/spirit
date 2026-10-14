import asyncio
import importlib
import os

import pytest

from companion.environment import load_environment


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    # Record these before dotenv writes so every test restores the original state.
    for name in ("CODING_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)


def test_loads_only_project_root_even_when_working_directory_has_another_env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    cwd = tmp_path / "elsewhere"
    project.mkdir()
    cwd.mkdir()
    (project / ".env").write_text("CODING_API_KEY=project-test-value\n", encoding="utf-8")
    (cwd / ".env").write_text("CODING_API_KEY=wrong-directory-test-value\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    assert load_environment(project)
    assert os.environ["CODING_API_KEY"] == "project-test-value"


@pytest.mark.parametrize("injected", ["cloud-test-placeholder", ""])
def test_existing_environment_always_wins_including_empty_values(tmp_path, monkeypatch, injected):
    monkeypatch.setenv("CODING_API_KEY", injected)
    (tmp_path / ".env").write_text("CODING_API_KEY=file-test-value\nDEEPSEEK_API_KEY=other-test-value\n", encoding="utf-8")
    load_environment(tmp_path)
    assert os.environ["CODING_API_KEY"] == injected
    assert os.environ["DEEPSEEK_API_KEY"] == "other-test-value"


def test_missing_project_file_does_not_search_parent_or_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / ".env").write_text("CODING_API_KEY=wrong-directory-test-value\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert not load_environment(project)
    assert "CODING_API_KEY" not in os.environ


def test_windows_utf8_bom_and_literal_key_values(tmp_path):
    (tmp_path / ".env").write_text('CODING_API_KEY="literal-${DEEPSEEK_API_KEY}"\n', encoding="utf-8-sig")
    load_environment(tmp_path)
    assert os.environ["CODING_API_KEY"] == "literal-${DEEPSEEK_API_KEY}"


@pytest.mark.parametrize("module_name", ["companion.__main__", "companion.discord_workflow", "companion.discord_listener"])
def test_every_entry_loads_dotenv_before_credentials_or_tool_initialization(tmp_path, monkeypatch, module_name):
    (tmp_path / ".env").write_text("CODING_API_KEY=entry-test-value\n", encoding="utf-8")
    entry = importlib.import_module(module_name)
    monkeypatch.setattr(entry, "load_environment", lambda root: load_environment(tmp_path))

    class StopBeforeNetwork(Exception):
        pass

    def preflight(*args, **kwargs):
        assert os.environ.get("CODING_API_KEY") == "entry-test-value"
        raise StopBeforeNetwork

    # Stop before construction of any live model request or Discord client.
    if module_name == "companion.__main__":
        monkeypatch.setattr(entry, "build_agents", preflight)
        operation = entry.main(goal="test")
    else:
        monkeypatch.setattr(entry, "check_models", preflight)
        operation = entry.run("test") if module_name.endswith("discord_workflow") else entry.run()
    with pytest.raises(StopBeforeNetwork):
        asyncio.run(operation)
