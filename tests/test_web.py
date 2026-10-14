import asyncio

import httpx
import pytest

from companion.tools.registry import ToolError
from companion.tools.web import URLArguments, WebReader, public_url


@pytest.mark.parametrize('url', ['http://example.org', 'https://other.org', 'https://user:password@example.org', 'https://example.org:8443', 'https://127.0.0.1', 'https://localhost'])
def test_reader_only_accepts_configured_public_https_urls(url):
    with pytest.raises(ToolError):
        public_url(url, ['example.org', '127.0.0.1', 'localhost'])


def test_web_reader_extracts_text_and_does_not_send_credentials():
    def endpoint(request):
        assert 'Authorization' not in request.headers
        return httpx.Response(200, headers={'Content-Type': 'text/html'}, text='<h1>Title</h1><script>ignore</script><style>ignore</style><p>Real text</p>')
    reader = WebReader(['example.org'], transport=httpx.MockTransport(endpoint))
    result = asyncio.run(reader.read(URLArguments(url='https://example.org/page')))
    assert result['text'] == 'Title Real text'
    assert result['url'] == 'https://example.org/page'


def test_reader_checks_redirect_destination():
    reader = WebReader(['example.org'], transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={'Location': 'https://other.org'})))
    with pytest.raises(ToolError):
        asyncio.run(reader.read(URLArguments(url='https://example.org')))


@pytest.mark.parametrize('response,code', [(httpx.Response(200, content=b'x' * (1024 * 1024 + 1)), 'page_too_large'), (httpx.Response(200, headers={'Content-Type': 'application/octet-stream'}, content=b'file'), 'unsupported_media')])
def test_reader_limits_content_size_and_media(response, code):
    reader = WebReader(['example.org'], transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(ToolError) as error:
        asyncio.run(reader.read(URLArguments(url='https://example.org')))
    assert error.value.code == code
