import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from companion.tools.approval import ApprovalManager, ExecutionContext, execution_context
from companion.tools.coding import CodingFiles, EditArguments, TestArguments as RunArguments, WriteArguments
from companion.tools.files import PathArguments, ReadArguments
from companion.tools.registry import ToolError


@pytest.fixture
def project(tmp_path):
    token = execution_context.set(ExecutionContext('human', 'coding', 'job'))
    approvals = ApprovalManager(tmp_path / '.agent-internal/audit', terminal=False, policies={'coding': 'automatic'})
    project = CodingFiles(tmp_path, approvals)
    yield project
    execution_context.reset(token)


def test_actual_create_read_edit_and_version_guard(project):
    result = asyncio.run(project.write_project_file(WriteArguments(path='src/code.py', content='value = 1\n')))
    assert result['status'] == 'written'
    read = asyncio.run(project.read_project_file(ReadArguments(path='src/code.py')))
    assert read['text'] == 'value = 1\n'
    result = asyncio.run(project.apply_text_edit(EditArguments(path='src/code.py', old_text='1', new_text='2', expected_sha256=read['sha256'])))
    assert (project.root / 'src/code.py').read_text() == 'value = 2\n'
    assert '-value = 1' in result['diff'] and '+value = 2' in result['diff']
    with pytest.raises(ToolError, match='changed'):
        asyncio.run(project.apply_text_edit(EditArguments(path='src/code.py', old_text='2', new_text='3', expected_sha256=read['sha256'])))
    assert (project.root / 'src/code.py').read_text() == 'value = 2\n'
    events = [json.loads(line) for line in project.approvals.audit_path.read_text().splitlines()]
    assert {event['approval_mode'] for event in events} == {'automatic'}
    assert not project.approvals.pending


def test_existing_file_requires_read_version_and_ambiguous_edit_fails(project):
    path = project.root / 'code.py'
    path.write_text('x = 1\ny = 1\n')
    with pytest.raises(ToolError, match='SHA-256'):
        asyncio.run(project.write_project_file(WriteArguments(path='code.py', content='replace')))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ToolError, match='exactly once'):
        asyncio.run(project.apply_text_edit(EditArguments(path='code.py', old_text='1', new_text='2', expected_sha256=digest)))
    assert path.read_text() == 'x = 1\ny = 1\n'


def test_manual_policy_can_deny_a_write_without_creating_directories(project):
    project.approvals.policies['coding'] = 'manual'
    project.approvals.terminal = True
    async def deny(request):
        assert '+not written' in request['details']['diff']
        return False
    project.approvals.confirmer = deny
    with pytest.raises(ToolError, match='declined'):
        asyncio.run(project.write_project_file(WriteArguments(path='new/file.py', content='not written\n')))
    assert not (project.root / 'new').exists()


def test_file_change_during_manual_preview_is_not_overwritten(project):
    path = project.root / 'file.py'
    path.write_text('before')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    project.approvals.policies['coding'] = 'manual'
    project.approvals.terminal = True
    async def confirm(request):
        path.write_text('changed while pending')
        return True
    project.approvals.confirmer = confirm
    with pytest.raises(ToolError, match='changed'):
        asyncio.run(project.write_project_file(WriteArguments(path='file.py', content='after', expected_sha256=digest)))
    assert path.read_text() == 'changed while pending'
    assert not list((project.root / '.agent-internal').glob('staging/*'))


@pytest.mark.parametrize('path', ['../outside.py', '.env', '.ENV.secret', '.git/config', '.GIT/config', '.ssh/id_rsa', 'NUL', 'file.py:secret', 'bad.'])
def test_coding_write_cannot_bypass_workspace_paths(project, path):
    with pytest.raises(ToolError):
        asyncio.run(project.write_project_file(WriteArguments(path=path, content='blocked')))
    assert not project.approvals.audit_path.exists()


def test_utf8_bom_preserved_by_targeted_edit(project):
    path = project.root / 'file.py'
    path.write_text('name = "你好"\n', encoding='utf-8-sig')
    read = asyncio.run(project.read_project_file(ReadArguments(path='file.py')))
    asyncio.run(project.apply_text_edit(EditArguments(path='file.py', old_text='你好', new_text='世界', expected_sha256=read['sha256'])))
    assert path.read_bytes().startswith(b'\xef\xbb\xbf')
    assert '世界' in path.read_text(encoding='utf-8-sig')


def test_pytest_returns_real_pass_failure_and_no_test_outcomes(project, monkeypatch):
    monkeypatch.setenv('CODING_API_KEY', 'private-test-value')
    monkeypatch.setenv('DISCORD_CODING_BOT_TOKEN', 'private-test-value')
    (project.root / 'test_sample.py').write_text('import os\ndef test_ok():\n    assert "CODING_API_KEY" not in os.environ\n    assert "DISCORD_CODING_BOT_TOKEN" not in os.environ\n')
    passed = asyncio.run(project.run_python_tests(RunArguments(path='test_sample.py')))
    assert passed['passed'] and passed['exit_code'] == 0
    assert '1 passed' in passed['output'] and 'private-test-value' not in passed['output']
    (project.root / 'test_sample.py').write_text('def test_bad():\n    assert False\n')
    failed = asyncio.run(project.run_python_tests(RunArguments(path='test_sample.py')))
    assert not failed['passed'] and failed['exit_code'] == 1
    assert '1 failed' in failed['output']
    (project.root / 'empty.py').write_text('x = 1\n')
    empty = asyncio.run(project.run_python_tests(RunArguments(path='empty.py')))
    assert not empty['passed'] and empty['exit_code'] == 5


def test_pytest_timeout_stops_process_and_captures_partial_output(project):
    (project.root / 'test_slow.py').write_text('import time\ndef test_slow():\n    time.sleep(10)\n')
    result = asyncio.run(project.run_python_tests(RunArguments(path='test_slow.py', timeout_seconds=1)))
    assert result['timed_out'] and not result['passed']
    assert result['exit_code'] is not None


def test_test_execution_obeys_optional_manual_policy(project):
    (project.root / 'test_touch.py').write_text('from pathlib import Path\ndef test_touch():\n    Path("ran").touch()\n')
    project.approvals.policies['coding'] = 'manual'
    project.approvals.terminal = True
    async def deny(request):
        assert request['details']['executes_project_code']
        return False
    project.approvals.confirmer = deny
    with pytest.raises(ToolError):
        asyncio.run(project.run_python_tests(RunArguments(path='test_touch.py')))
    assert not (project.root / 'ran').exists()


def test_pytest_output_limit_is_enforced(project):
    (project.root / 'test_loud.py').write_text('def test_loud():\n    print("x" * 100000)\n    assert False\n')
    result = asyncio.run(project.run_python_tests(RunArguments(path='test_loud.py')))
    assert result['output_truncated'] and not result['passed']
    assert len(result['output'].encode()) <= 65536


def test_coding_writes_wait_until_running_tests_finish(project):
    import time
    path = project.root / 'values.py'
    path.write_text('value = 1\n')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (project.root / 'test_values.py').write_text('from pathlib import Path\nimport time\ndef test_original():\n    Path("started").touch()\n    time.sleep(0.25)\n    assert Path("values.py").read_text() == "value = 1\\n"\n')
    async def check():
        test = asyncio.create_task(project.run_python_tests(RunArguments(path='test_values.py')))
        deadline = time.monotonic() + 4
        while not (project.root / 'started').exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        edit = asyncio.create_task(project.write_project_file(WriteArguments(path='values.py', content='value = 2\n', expected_sha256=digest)))
        tested, written = await asyncio.gather(test, edit)
        assert tested['passed']
        assert written['status'] == 'written'
    asyncio.run(check())
    assert path.read_text() == 'value = 2\n'
