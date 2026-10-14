"""Isolated, bounded browser reads for public configured websites."""
from contextlib import asynccontextmanager
import importlib.util
import os
import re
import shutil

import httpx
from pydantic import Field

from companion.tools.registry import ToolError
from companion.tools.web import PageArguments, URLArguments, public_url


class BrowseArguments(PageArguments):
    selector: str = Field(default='body', min_length=1, max_length=256)
    wait_ms: int = Field(default=1000, ge=0, le=5000)


class ExtractArguments(URLArguments):
    selectors: dict[str, str] = Field(min_length=1, max_length=8)
    attributes: dict[str, str] = Field(default_factory=dict, max_length=8)
    wait_ms: int = Field(default=1000, ge=0, le=5000)


TEXT_SNAPSHOT = """(element) => ({
    text: (element.innerText || '').slice(0, 1048577),
    links: Array.from(element.querySelectorAll('a[href]')).slice(0, 100)
        .map(a => ({text: (a.innerText || '').slice(0, 120), url: a.href})),
    elements: Array.from(element.querySelectorAll('h1,h2,h3,article,main,table,[id]')).slice(0, 25)
        .map(e => ({tag: e.tagName.toLowerCase(), id: e.id.slice(0, 128),
            selector: e.id && e.id.length <= 128 ? '#' + CSS.escape(e.id) : e.tagName.toLowerCase(),
            text: (e.innerText || '').slice(0, 120)}))
})"""

FIELD_SNAPSHOT = """(elements, attribute) => ({
    count: elements.length,
    items: elements.slice(0, 3).map(e => {
        const text = attribute ? (e.getAttribute(attribute) || '') : (e.innerText || e.textContent || '');
        return {text: text.slice(0, 1000), truncated: text.length > 1000};
    })
})"""


class WebBrowser:
    def __init__(self, domains, *, playwright_factory=None):
        self.domains = domains
        self.playwright_factory = playwright_factory

    def capabilities(self):
        return {'playwright_installed': importlib.util.find_spec('playwright') is not None,
                'javascript': True, 'read_domains': self.domains,
                'operations': ['rendered_text', 'links', 'element_hints', 'CSS_field_extraction'],
                'sessions': 'Fresh browser context per call; no saved login/cookies',
                'setup': 'Install the browser extra and run python -m playwright install chromium; a system chromium/google-chrome on PATH or WEB_BROWSER_EXECUTABLE_PATH can also be used'}

    async def guard_request(self, route):
        try:
            public_url(route.request.url, self.domains)
            if route.request.method not in {'GET', 'HEAD'}:
                raise ToolError('browser_read_only', 'Only read requests are supported')
        except (ToolError, httpx.InvalidURL, ValueError):
            await route.abort()
        else:
            await route.continue_()

    @asynccontextmanager
    async def page(self, url, wait_ms):
        public_url(url, self.domains)
        if self.playwright_factory is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise ToolError('browser_missing', 'Install the browser extra and Chromium to enable rendered webpage reads') from None
            factory = async_playwright
        else:
            factory = self.playwright_factory
        try:
            async with factory() as driver:
                browser = await driver.chromium.launch(headless=True,
                    executable_path=os.environ.get('WEB_BROWSER_EXECUTABLE_PATH') or shutil.which('chromium') or shutil.which('google-chrome'))
                try:
                    context = await browser.new_context(accept_downloads=False, service_workers='block')
                    await context.route('**/*', self.guard_request)
                    # WebSocket traffic cannot bypass the ordinary request guard.
                    await context.route_web_socket('**/*', lambda socket: socket.close())
                    page = await context.new_page()
                    page.set_default_timeout(10000)
                    response = await page.goto(url, wait_until='domcontentloaded', timeout=20000)
                    if response is None or response.status >= 400:
                        status = response.status if response is not None else 'unavailable'
                        raise ToolError('web_unavailable', f'Browser navigation returned {status}')
                    await page.wait_for_timeout(wait_ms)
                    public_url(page.url, self.domains)
                    yield page
                finally:
                    await browser.close()
        except ToolError:
            raise
        except Exception as exc:
            # Browser errors can include URL credentials, page bodies or local paths.
            raise ToolError('browser_failed', 'Browser operation failed; check Chromium installation, allowed page/resource domains and selectors (' + type(exc).__name__ + ')') from None

    async def browse(self, args: BrowseArguments):
        async with self.page(args.url, args.wait_ms) as page:
            locator = page.locator(args.selector).first
            if await locator.count() == 0:
                raise ToolError('selector_not_found', 'The requested selector did not match the rendered page')
            snapshot = await locator.evaluate(TEXT_SNAPSHOT)
            text = snapshot['text']
            if len(text) > 1024 * 1024:
                raise ToolError('page_too_large', 'Rendered webpage text exceeds 1 MiB')
            end = min(len(text), args.offset + args.limit)
            links = []
            seen = set()
            for item in snapshot['links']:
                try:
                    url = httpx.URL(item['url'])
                    public_url(str(url), [url.host])
                except (ToolError, httpx.InvalidURL, ValueError):
                    continue
                if len(str(url)) <= 1024 and str(url) not in seen:
                    links.append({'text': item['text'], 'url': str(url), 'readable': url.host in self.domains})
                    seen.add(str(url))
                if len(links) == 20:
                    break
            return {'url': page.url, 'title': (await page.title())[:512], 'selector': args.selector,
                    'text': text[args.offset:end], 'offset': args.offset, 'total_chars': len(text),
                    'truncated': end < len(text), 'next_offset': end if end < len(text) else None,
                    'links': links, 'elements': snapshot['elements'], 'rendered': True,
                    'pagination_note': 'A new browser session rereads the page; changing content may affect offsets'}

    async def extract(self, args: ExtractArguments):
        if any(not name.strip() or len(name) > 64 or not selector.strip() or len(selector) > 256
               for name, selector in args.selectors.items()):
            raise ToolError('invalid_selectors', 'Provide named fields (1–64 characters) and selectors (1–256 characters)')
        if any(name not in args.selectors or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9:_-]{0,63}', attribute)
               for name, attribute in args.attributes.items()):
            raise ToolError('invalid_attributes', 'Attributes must refer to selected fields and use valid attribute names')
        async with self.page(args.url, args.wait_ms) as page:
            fields = {}
            for name, selector in args.selectors.items():
                attribute = args.attributes.get(name)
                result = await page.locator(selector).evaluate_all(FIELD_SNAPSHOT, attribute)
                fields[name] = {'selector': selector, 'attribute': attribute,
                                'found': any(item['text'].strip() for item in result['items']),
                                'match_count': result['count'], 'items': result['items'],
                                'matches_truncated': result['count'] > len(result['items'])}
            return {'url': page.url, 'title': (await page.title())[:512], 'fields': fields, 'rendered': True}
