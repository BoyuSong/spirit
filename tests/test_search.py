import asyncio
from types import SimpleNamespace

import pytest

from companion.models.base import ModelAPIError, ModelResponse
from companion.tools.registry import ToolError
from companion.tools.search import SearchArguments, WebSearch


class SearchGateway:
    def __init__(self, response, provider='openai'):
        self.response = response
        self.registry = SimpleNamespace(get=lambda role: SimpleNamespace(provider=provider))
        self.requests = []

    async def generate(self, role, messages, **options):
        self.requests.append((role, messages, options))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def result(sources, text='Search facts'):
    return ModelResponse(text, 'search-model', provider_items=[
        {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search', 'sources': sources}},
        {'type': 'message', 'content': [{'annotations': [{'type': 'url_citation', 'url': 'https://docs.python.org/3/', 'title': 'Citation'}]}]},
    ])


def test_search_forces_native_lookup_and_returns_bounded_deduplicated_public_sources():
    gateway = SearchGateway(result([
        {'url': 'https://docs.python.org/3/', 'title': 'Docs'},
        {'url': 'https://docs.python.org/3/'},
        {'url': 'https://other.org/page', 'title': 'Other'},
        {'url': 'https://user:secret@other.org/private'},
        {'url': 'https://127.0.0.1/'},
        {'url': 'file:///secret'},
    ], text='x' * 14000))
    searched = asyncio.run(WebSearch(gateway, 'search', ['docs.python.org']).search(SearchArguments(query='Python docs', limit=2)))
    assert searched['search_performed'] and len(searched['summary']) == 12000 and searched['summary_truncated']
    assert len(searched['sources']) == 2
    assert searched['sources'][0]['readable'] and not searched['sources'][1]['readable']
    assert gateway.requests[0][0] == 'search'
    assert gateway.requests[0][2] == {'tools': [{'type': 'web_search'}], 'tool_choice': {'type': 'web_search'}}
    assert len(gateway.requests[0][1]) == 2  # No other agent history or project files forwarded.


def test_search_rejects_answer_without_actual_completed_search():
    gateway = SearchGateway(ModelResponse('Unverified answer', 'fake'))
    with pytest.raises(ToolError) as error:
        asyncio.run(WebSearch(gateway, 'search', []).search(SearchArguments(query='question')))
    assert error.value.code == 'search_not_performed'


@pytest.mark.parametrize('response,provider,code', [
    (ModelAPIError('OpenAI request failed (HTTP 429)'), 'openai', 'search_unavailable'),
    (ValueError('private-test-value'), 'openai', 'search_unavailable'),
    (result([]), 'deepseek', 'search_provider_unsupported'),
])
def test_search_failures_are_explicit_and_do_not_reveal_raw_provider_errors(response, provider, code):
    gateway = SearchGateway(response, provider=provider)
    with pytest.raises(ToolError) as error:
        asyncio.run(WebSearch(gateway, 'search', []).search(SearchArguments(query='question')))
    assert error.value.code == code and 'private-test-value' not in str(error.value)


def test_blank_search_does_not_call_backend():
    gateway = SearchGateway(result([]))
    with pytest.raises(ToolError, match='blank'):
        asyncio.run(WebSearch(gateway, 'search', []).search(SearchArguments(query='  ')))
    assert not gateway.requests


def test_deepseek_website_preflight_also_checks_independent_search_credentials(monkeypatch):
    from pathlib import Path
    from companion.collaboration.config import CollaborationConfig
    from companion.discord_workflow import check_models
    from companion.models.registry import ModelRegistry
    root = Path(__file__).resolve().parents[1]
    config = CollaborationConfig.load(root / 'config/agents.yaml')
    registry = ModelRegistry(root / 'config/models.yaml')
    assert registry.get('web').provider == 'deepseek'
    assert not config.agents['web'].builtin_tools
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-value')
    monkeypatch.delenv('CODING_API_KEY', raising=False)
    with pytest.raises(ValueError, match='CODING_API_KEY'):
        check_models(config, registry, ['web'])
    monkeypatch.setenv('CODING_API_KEY', 'test-value')
    check_models(config, registry, ['web'])
