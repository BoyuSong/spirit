from html.parser import HTMLParser
import ipaddress

import httpx

from companion.tools.registry import ToolArguments, ToolError
from pydantic import Field


class URLArguments(ToolArguments):
    url: str = Field(min_length=1, max_length=4096)


def public_url(url: str, domains: list[str]) -> httpx.URL:
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or parsed.userinfo or parsed.port not in (None, 443) or parsed.host not in domains:
        raise ToolError("url_not_allowed", "Use an HTTPS URL on a configured web domain")
    try:
        ipaddress.ip_address(parsed.host)
    except ValueError:
        pass
    else:
        raise ToolError("url_not_allowed", "IP address URLs are not supported")
    if parsed.host == "localhost" or parsed.host.endswith((".localhost", ".local")):
        raise ToolError("url_not_allowed", "Local network URLs are not supported")
    return parsed


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class WebReader:
    def __init__(self, domains: list[str], *, transport=None):
        self.domains = domains
        self.transport = transport

    async def read(self, args: URLArguments):
        url = public_url(args.url, self.domains)
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
            for _ in range(4):
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = public_url(str(url.join(response.headers["Location"])), self.domains)
                        continue
                    if response.status_code != 200:
                        raise ToolError("web_unavailable", f"Website returned HTTP {response.status_code}")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024:
                            raise ToolError("page_too_large", "Webpage exceeds 1 MiB")
                    media = response.headers.get("Content-Type", "").lower()
                    text = bytes(data).decode(response.encoding or "utf-8", errors="replace")
                    if "html" in media:
                        parser = VisibleText()
                        parser.feed(text)
                        text = " ".join(" ".join(parser.parts).split())
                    elif "text/" not in media:
                        raise ToolError("unsupported_media", "Website must return HTML or text")
                    return {"url": str(url), "text": text[:24000], "truncated": len(text) > 24000}
        raise ToolError("redirect_limit", "Too many webpage redirects")
