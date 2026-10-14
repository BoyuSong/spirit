import asyncio
from contextlib import asynccontextmanager
import importlib.util
import shutil
from types import SimpleNamespace

import httpx
import pytest

from companion.tools.browser import BrowseArguments, ExtractArguments, WebBrowser
from companion.tools.registry import ToolError
from companion.tools.web import public_url


class Route:
    def __init__(self, url, method='GET'):
        self.request = SimpleNamespace(url=url, method=method)
        self.aborted = self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


@pytest.mark.parametrize('url,method,allowed', [
    ('https://example.org/page', 'GET', True),
    ('https://example.org/script.js', 'HEAD', True),
    ('https://example.org/submit', 'POST', False),
    ('https://other.org/track', 'GET', False),
    ('http://example.org/page', 'GET', False),
    ('https://127.0.0.1/secret', 'GET', False),
    ('file:///etc/passwd', 'GET', False),
])
def test_every_browser_request_obeys_domains_scheme_and_read_only_method(url, method, allowed):
    route = Route(url, method)
    asyncio.run(WebBrowser(['example.org']).guard_request(route))
    assert route.continued == allowed and route.aborted != allowed


def test_invalid_start_url_is_rejected_before_starting_browser():
    def unexpected_factory():
        raise AssertionError('Browser must not start')
    browser = WebBrowser(['example.org'], playwright_factory=unexpected_factory)
    with pytest.raises(ToolError, match='configured web domain'):
        asyncio.run(browser.browse(BrowseArguments(url='https://other.org')))


def test_browser_errors_do_not_reveal_page_or_local_sensitive_details():
    @asynccontextmanager
    async def broken():
        raise RuntimeError('private-test-key in /private/profile')
        yield
    browser = WebBrowser(['example.org'], playwright_factory=broken)
    with pytest.raises(ToolError) as error:
        asyncio.run(browser.browse(BrowseArguments(url='https://example.org')))
    assert error.value.code == 'browser_failed'
    assert 'private-test-key' not in str(error.value) and '/private/profile' not in str(error.value)


@pytest.fixture
def rendered_site(monkeypatch):
    if importlib.util.find_spec('playwright') is None:
        pytest.skip('Install the browser extra to test rendered pages')
    executable = shutil.which('chromium') or shutil.which('google-chrome')
    if not executable:
        pytest.skip('A local Chromium executable is needed for these offline fixtures')
    monkeypatch.delenv('WEB_BROWSER_EXECUTABLE_PATH', raising=False)
    html = '''<title>Product details</title><h1 id="product">Example product</h1>
        <p id="price">Loading</p><p id="session"></p><a href="/next">Next page</a>
        <a href="https://other.org/source">Other source</a>
        <a href="javascript:alert(1)">Invalid link</a>
        <script>
        localStorage.setItem('visits', String(Number(localStorage.getItem('visits') || 0) + 1));
        document.getElementById('session').innerText = 'Session ' + localStorage.getItem('visits');
        setTimeout(() => { document.getElementById('price').innerText = 'Price 128'; }, 50);
        </script>'''
    class Site(WebBrowser):
        requests = []
        async def guard_request(self, route):
            self.requests.append(route.request.url)
            try:
                public_url(route.request.url, self.domains)
                if route.request.method not in {'GET', 'HEAD'}:
                    raise ToolError('blocked', 'read only')
            except (ToolError, httpx.InvalidURL, ValueError):
                await super().guard_request(route)
                return
            # Fulfill every allowed request locally: no real website traffic.
            await route.fulfill(status=200, content_type='text/html', body=html)
    return Site(['example.org'])


def test_real_chromium_reads_javascript_text_links_and_selector_hints(rendered_site):
    result = asyncio.run(rendered_site.browse(BrowseArguments(url='https://example.org/product', wait_ms=150)))
    assert result['rendered'] and result['title'] == 'Product details'
    assert 'Price 128' in result['text'] and 'Loading' not in result['text']
    assert result['links'][:2] == [
        {'text': 'Next page', 'url': 'https://example.org/next', 'readable': True},
        {'text': 'Other source', 'url': 'https://other.org/source', 'readable': False},
    ]
    assert all(not item['url'].startswith('javascript:') for item in result['links'])
    assert any(item['selector'] == '#price' for item in result['elements'])


def test_real_browser_extracts_target_fields_and_reports_missing_values(rendered_site):
    result = asyncio.run(rendered_site.extract(ExtractArguments(url='https://example.org/product',
        selectors={'name': '#product', 'price': '#price', 'missing': '#absent', 'link': 'a[href="/next"]', 'missing_attribute': '#price'},
        attributes={'link': 'href', 'missing_attribute': 'datetime'}, wait_ms=150)))
    assert result['fields']['name']['items'][0]['text'] == 'Example product'
    assert result['fields']['price']['items'][0]['text'] == 'Price 128'
    assert not result['fields']['missing']['found'] and result['fields']['missing']['items'] == []
    assert result['fields']['link']['items'][0]['text'] == '/next'
    assert not result['fields']['missing_attribute']['found']
    assert result['fields']['missing_attribute']['match_count'] == 1
    assert result['url'] == 'https://example.org/product'


def test_extraction_rejects_unknown_attribute_fields_before_browser_start():
    def unexpected_factory():
        raise AssertionError('Browser must not start')
    browser = WebBrowser(['example.org'], playwright_factory=unexpected_factory)
    with pytest.raises(ToolError, match='selected fields'):
        asyncio.run(browser.extract(ExtractArguments(url='https://example.org', selectors={'title': 'h1'}, attributes={'unknown': 'href'})))


def test_real_browser_paginates_selected_text_and_does_not_reuse_session(rendered_site):
    first = asyncio.run(rendered_site.browse(BrowseArguments(url='https://example.org/product', selector='#price', limit=5, wait_ms=150)))
    second = asyncio.run(rendered_site.browse(BrowseArguments(url='https://example.org/product', selector='#price', offset=first['next_offset'], wait_ms=150)))
    assert first['text'] + second['text'] == 'Price 128'
    assert second['next_offset'] is None
    for _ in range(2):
        result = asyncio.run(rendered_site.extract(ExtractArguments(url='https://example.org/product', selectors={'session': '#session'}, wait_ms=0)))
        assert result['fields']['session']['items'][0]['text'] == 'Session 1'


def test_real_browser_reports_invalid_and_unmatched_selectors(rendered_site):
    with pytest.raises(ToolError) as unmatched:
        asyncio.run(rendered_site.browse(BrowseArguments(url='https://example.org/product', selector='#absent', wait_ms=0)))
    assert unmatched.value.code == 'selector_not_found'
    with pytest.raises(ToolError) as invalid:
        asyncio.run(rendered_site.extract(ExtractArguments(url='https://example.org/product', selectors={'field': '['}, wait_ms=0)))
    assert invalid.value.code == 'browser_failed'
