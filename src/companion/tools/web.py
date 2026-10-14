from html.parser import HTMLParser
import ipaddress

import httpx

from companion.tools.registry import ToolArguments, ToolError
from pydantic import Field


class URLArguments(ToolArguments):
    url: str = Field(min_length=1, max_length=4096)


class PageArguments(URLArguments):
    offset: int = Field(default=0, ge=0, le=1024 * 1024)
    limit: int = Field(default=12000, ge=1, le=24000)


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
        self.title_parts = []
        self.in_title = False
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag == "a" and not self.hidden:
            href = dict(attrs).get("href")
            if href and len(self.links) < 100:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title_parts.append(data)


class WebReader:
    def __init__(self, domains: list[str], *, transport=None):
        self.domains = domains
        self.transport = transport

    async def capabilities(self, _):
        return {"read_domains": self.domains, "formats": ["HTML", "text"],
                "max_page_bytes": 1024 * 1024, "max_chunk_chars": 24000,
                "javascript": False, "pagination": "Use next_offset with the same URL"}

    async def read(self, args: PageArguments):
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
                    title = ""
                    links = []
                    if "html" in media:
                        parser = VisibleText()
                        parser.feed(text)
                        text = " ".join(" ".join(parser.parts).split())
                        title = " ".join(" ".join(parser.title_parts).split())[:512]
                        for href in parser.links:
                            try:
                                link = url.join(href)
                                public_url(str(link), [link.host])
                            except (ValueError, httpx.InvalidURL, ToolError):
                                continue
                            if len(str(link)) <= 1024 and str(link) not in [item["url"] for item in links]:
                                links.append({"url": str(link), "readable": link.host in self.domains})
                            if len(links) == 20:
                                break
                    elif "text/" not in media:
                        raise ToolError("unsupported_media", "Website must return HTML or text")
                    offset, limit = getattr(args, "offset", 0), getattr(args, "limit", 12000)
                    end = min(len(text), offset + limit)
                    return {"url": str(url), "title": title, "text": text[offset:end],
                            "offset": offset, "total_chars": len(text), "truncated": end < len(text),
                            "next_offset": end if end < len(text) else None, "links": links}
        raise ToolError("redirect_limit", "Too many webpage redirects")
