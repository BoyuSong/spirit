import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from companion.tools.approval import ApprovalManager, ExecutionContext, execution_context
from companion.tools.files import DownloadArguments, PathArguments, SystemFiles
from companion.tools.registry import ToolError


def files(tmp_path, answer=True, endpoint=None):
    async def confirm(request):
        return answer
    approval = ApprovalManager(tmp_path / '.agent-internal/audit.jsonl', confirmer=confirm)
    return SystemFiles(tmp_path, approval, ['example.org'], transport=httpx.MockTransport(endpoint) if endpoint else None)


def test_read_list_and_utf8_truncation(tmp_path):
    (tmp_path / 'text.txt').write_text('你' * 12000)
    (tmp_path / '.env.secret').write_text('private')
    system = files(tmp_path)
    listing = asyncio.run(system.list_directory(PathArguments()))
    assert [item['name'] for item in listing['entries']] == ['text.txt']
    result = asyncio.run(system.read_text_file(PathArguments(path='text.txt')))
    assert result['truncated']
    assert set(result['text']) == {'你'}


@pytest.mark.parametrize('path', ['../outside', '/etc/passwd', '.env.secret', '.git/config', '.agent-internal/audit.jsonl', '.aws/credentials'])
def test_paths_outside_workspace_and_control_paths_are_blocked(tmp_path, path):
    with pytest.raises(ToolError, match='protected|outside|relative'):
        files(tmp_path).path(path)


def test_symlink_is_blocked_even_inside_workspace(tmp_path):
    (tmp_path / 'target').write_text('hi')
    (tmp_path / 'link').symlink_to(tmp_path / 'target')
    with pytest.raises(ToolError, match='Symlink'):
        files(tmp_path).path('link')


def test_denied_delete_preserves_file_and_no_repeat_prompt_in_job(tmp_path):
    (tmp_path / 'keep').write_text('keep')
    system = files(tmp_path, False)
    for _ in range(2):
        with pytest.raises(ToolError, match='declined'):
            asyncio.run(system.delete_file(PathArguments(path='keep')))
    assert (tmp_path / 'keep').read_text() == 'keep'
    events = [json.loads(line) for line in system.approvals.audit_path.read_text().splitlines()]
    assert [event['status'] for event in events] == ['requested', 'denied']


def test_approved_delete_is_recoverable_and_audited(tmp_path):
    (tmp_path / 'remove').write_text('restore me')
    system = files(tmp_path)
    result = asyncio.run(system.delete_file(PathArguments(path='remove')))
    assert not (tmp_path / 'remove').exists()
    assert (tmp_path / '.agent-internal/trash' / result['restore_id']).read_text() == 'restore me'
    events = [json.loads(line) for line in system.approvals.audit_path.read_text().splitlines()]
    assert [event['status'] for event in events] == ['requested', 'approved', 'completed']
    assert events[0]['details']['path'] == str(tmp_path / 'remove')


def test_file_change_during_approval_requires_new_preview(tmp_path):
    target = tmp_path / 'file'
    target.write_text('old')
    system = files(tmp_path)
    async def change(request):
        target.write_text('changed')
        return True
    system.approvals.confirmer = change
    with pytest.raises(ToolError, match='changed'):
        asyncio.run(system.delete_file(PathArguments(path='file')))
    assert target.read_text() == 'changed'


def test_denied_download_sends_no_get_and_creates_no_target(tmp_path):
    methods = []
    def endpoint(request):
        methods.append(request.method)
        return httpx.Response(200, headers={'Content-Length': '4'})
    system = files(tmp_path, False, endpoint)
    with pytest.raises(ToolError, match='declined'):
        asyncio.run(system.download_file(DownloadArguments(url='https://example.org/file', path='new')))
    assert methods == ['HEAD']
    assert not (tmp_path / 'new').exists()


def test_approved_download_checks_hash_and_previews_final_url(tmp_path):
    data = b'downloaded'
    def endpoint(request):
        assert 'Authorization' not in request.headers
        if request.url.path == '/start':
            return httpx.Response(302, headers={'Location': '/file'})
        return httpx.Response(200, content=data)
    system = files(tmp_path, True, endpoint)
    result = asyncio.run(system.download_file(DownloadArguments(url='https://example.org/start', path='new', sha256=hashlib.sha256(data).hexdigest())))
    assert (tmp_path / 'new').read_bytes() == data
    assert result['bytes'] == len(data)
    requested = json.loads(system.approvals.audit_path.read_text().splitlines()[0])
    assert requested['details']['url'] == 'https://example.org/file'
    assert requested['details']['path'] == str(tmp_path / 'new')
    assert not list((tmp_path / '.agent-internal/staging').iterdir())


def test_hash_mismatch_leaves_no_download_or_staging_file(tmp_path):
    system = files(tmp_path, True, lambda _: httpx.Response(200, content=b'wrong'))
    with pytest.raises(ToolError, match='SHA-256'):
        asyncio.run(system.download_file(DownloadArguments(url='https://example.org/file', path='new', sha256='0' * 64)))
    assert not (tmp_path / 'new').exists()
    assert not list((tmp_path / '.agent-internal/staging').iterdir())


def test_download_cannot_overwrite_destination_created_during_approval(tmp_path):
    system = files(tmp_path, True, lambda _: httpx.Response(200, content=b'new'))
    async def confirm(request):
        (tmp_path / 'target').write_bytes(b'keep')
        return True
    system.approvals.confirmer = confirm
    with pytest.raises(FileExistsError):
        asyncio.run(system.download_file(DownloadArguments(url='https://example.org/file', path='target')))
    assert (tmp_path / 'target').read_bytes() == b'keep'


def test_download_redirect_to_unconfigured_site_never_prompts(tmp_path):
    system = files(tmp_path, True, lambda _: httpx.Response(302, headers={'Location': 'https://other.org/file'}))
    with pytest.raises(ToolError, match='configured'):
        asyncio.run(system.download_file(DownloadArguments(url='https://example.org/file', path='new')))
    assert not system.approvals.audit_path.exists()


def test_approval_is_owner_bound_one_use_and_cancelled_on_timeout(tmp_path):
    async def check():
        notified = asyncio.Event()
        async def notify(request):
            notified.set()
        approvals = ApprovalManager(tmp_path / 'audit', terminal=False, notifier=notify, timeout=0.1)
        token = execution_context.set(ExecutionContext('human', 'system', 'job'))
        try:
            task = asyncio.create_task(approvals.require('delete_file', {'path': 'file'}))
            await notified.wait()
            request_id = next(iter(approvals.pending))
            assert not approvals.resolve(request_id, 'bot', True)
            assert not approvals.resolve(request_id, 'other-human', True)
            assert approvals.resolve(request_id, 'human', True)
            assert not approvals.resolve(request_id, 'human', True)
            await task
            assert not approvals.pending
            with pytest.raises(TimeoutError):
                await approvals.require('download_file', {'path': 'new'})
            assert not approvals.pending
            assert not approvals.resolve(request_id, 'human', True)
        finally:
            execution_context.reset(token)
    asyncio.run(check())


def test_noninteractive_terminal_never_approves(tmp_path, monkeypatch):
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    approvals = ApprovalManager(tmp_path / 'audit')
    with pytest.raises(ToolError, match='declined'):
        asyncio.run(approvals.require('delete_file', {'path': 'file'}))


def test_automatic_system_policy_deletes_and_restores_without_prompt(tmp_path):
    from companion.tools.files import RestoreArguments
    system = files(tmp_path)
    system.approvals.terminal = False
    system.approvals.policies['system'] = 'automatic'
    (tmp_path / 'file.txt').write_text('recoverable')
    result = asyncio.run(system.delete_file(PathArguments(path='file.txt')))
    assert not (tmp_path / 'file.txt').exists()
    restored = asyncio.run(system.restore_file(RestoreArguments(restore_id=result['restore_id'])))
    assert restored == {'status': 'restored', 'path': 'file.txt'}
    assert (tmp_path / 'file.txt').read_text() == 'recoverable'
    assert not system.approvals.pending
    with pytest.raises(ToolError, match='missing'):
        asyncio.run(system.restore_file(RestoreArguments(restore_id=result['restore_id'])))


def test_restore_never_overwrites_recreated_file(tmp_path):
    from companion.tools.files import RestoreArguments
    system = files(tmp_path)
    (tmp_path / 'file.txt').write_text('old')
    result = asyncio.run(system.delete_file(PathArguments(path='file.txt')))
    (tmp_path / 'file.txt').write_text('new')
    with pytest.raises(ToolError, match='absent'):
        asyncio.run(system.restore_file(RestoreArguments(restore_id=result['restore_id'])))
    assert (tmp_path / 'file.txt').read_text() == 'new'
    assert (tmp_path / '.agent-internal/trash' / result['restore_id']).read_text() == 'old'


def test_automatic_download_has_no_pending_approval(tmp_path):
    def endpoint(request):
        return httpx.Response(200, content=b'downloaded')
    system = files(tmp_path, endpoint=endpoint)
    system.approvals.terminal = False
    system.approvals.policies['system'] = 'automatic'
    result = asyncio.run(system.download_file(DownloadArguments(url='https://example.org/file', path='new')))
    assert result['status'] == 'downloaded'
    assert not system.approvals.pending
    assert (tmp_path / 'new').read_bytes() == b'downloaded'


def test_read_pagination_preserves_multibyte_text_and_bom(tmp_path):
    from companion.tools.files import ReadArguments
    original = '你' * 16000
    (tmp_path / 'text.txt').write_text(original, encoding='utf-8-sig')
    system = files(tmp_path)
    first = asyncio.run(system.read_text_file(ReadArguments(path='text.txt')))
    second = asyncio.run(system.read_text_file(ReadArguments(path='text.txt', offset=first['next_offset'])))
    assert first['text'] + second['text'] == original
    assert second['next_offset'] is None


def test_directory_creation_and_recovery_listing_follow_system_policy(tmp_path):
    from companion.tools.registry import ToolArguments
    system = files(tmp_path)
    system.approvals.terminal = False
    system.approvals.policies['system'] = 'automatic'
    result = asyncio.run(system.make_directory(PathArguments(path='downloads/nested')))
    assert result['status'] == 'created'
    assert (tmp_path / 'downloads/nested').is_dir()
    (tmp_path / 'recover.txt').write_text('contents not in listing')
    deleted = asyncio.run(system.delete_file(PathArguments(path='recover.txt')))
    listing = asyncio.run(system.list_recoverable_files(ToolArguments()))
    assert len(listing['entries']) == 1
    assert listing['entries'][0]['restore_id'] == deleted['restore_id']
    assert listing['entries'][0]['path'] == 'recover.txt'
    assert 'contents not in listing' not in json.dumps(listing)
