"""neo tools — web fetch and web search (no API keys needed)."""

from __future__ import annotations

import html as html_lib
import os
import re
import urllib.parse

import httpx

from .base import Tool, ToolContext, ToolResult

_FETCH_TIMEOUT = 15.0
_FETCH_CAP = 30_000
_SEARCH_TIMEOUT = 15.0
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) neo/0.1"


def _make_client(timeout: float) -> httpx.AsyncClient:
    """Build an httpx client, working around an httpx<0.29 bug where
    bracketed IPv6 entries in no_proxy crash client construction."""
    kwargs = dict(follow_redirects=True, timeout=timeout, headers={"User-Agent": _UA})
    try:
        return httpx.AsyncClient(**kwargs)
    except httpx.InvalidURL:
        # Sandbox quirk: httpx<0.29 crashes parsing bracketed IPv6 no_proxy
        # entries, and certifi's bundle misses the egress proxy's CA. Bypass
        # env parsing and use the system CA bundle if present.
        proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
        )
        verify: object = True
        for bundle in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
            if os.path.exists(bundle):
                verify = bundle
                break
        return httpx.AsyncClient(proxy=proxy or None, trust_env=False, verify=verify, **kwargs)

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"[ \t\u00a0\x0b\x0c\r]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_LINK = re.compile(r'(?is)<a\s+[^>]*?href=(["\'])(.*?)\1[^>]*?>(.*?)</a>')

_RESULT_A = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
_RESULT_SNIPPET = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</(?:td|div)>', re.S
)
_RESULT_BLOCK = re.compile(r'class="result__body">(.*?)</td>', re.S)


def _html_to_text(page: str, *, markdown: bool = False, base_url: str = "") -> str:
    page = _SCRIPT_STYLE.sub(" ", page)
    if markdown:
        def _link(m: "re.Match[str]") -> str:
            href = html_lib.unescape(m.group(2)).strip()
            text = _TAGS.sub(" ", m.group(3)).strip()
            text = html_lib.unescape(_WS.sub(" ", text)).strip()
            if not text:
                return ""
            if href and not href.startswith(("javascript:", "#")):
                href = urllib.parse.urljoin(base_url, href)
                return f"[{text}]({href})"
            return text

        page = _LINK.sub(_link, page)
    page = _TAGS.sub(" ", page)
    page = html_lib.unescape(page)
    page = _WS.sub(" ", page)
    lines = [ln.strip() for ln in page.split("\n")]
    page = _BLANK_LINES.sub("\n\n", "\n".join(ln for ln in lines if ln))
    return page.strip()


class WebFetchTool(Tool):
    name = "webfetch"
    description = "Fetch a URL and return its text content (HTML is converted to text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch (http/https)."},
            "format": {
                "type": "string",
                "description": "markdown keeps links as [text](url); text strips them.",
                "enum": ["markdown", "text"],
                "default": "markdown",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    needs_approval = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = args["url"].strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(is_error=True, output="url must start with http:// or https://", title=self.name)
        fmt = args.get("format", "markdown")
        if fmt not in ("markdown", "text"):
            return ToolResult(is_error=True, output=f'format must be "markdown" or "text", got {fmt!r}', title=self.name)
        try:
            async with _make_client(_FETCH_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "html" in content_type or "<html" in resp.text[:2000].lower():
                    text = _html_to_text(resp.text, markdown=(fmt == "markdown"), base_url=str(resp.url))
                else:
                    text = resp.text
        except Exception as exc:  # noqa: BLE001
            return ToolResult(is_error=True, output=f"fetch failed: {type(exc).__name__}: {exc}", title=self.name)
        truncated = False
        if len(text) > _FETCH_CAP:
            text = text[:_FETCH_CAP]
            truncated = True
        out = text or "(empty page)"
        if truncated:
            out += f"\n... (page capped at {_FETCH_CAP} chars)"
        return ToolResult(output=out, title=url)


class WebSearchTool(Tool):
    name = "websearch"
    description = "Search the web (DuckDuckGo HTML endpoint, no API key). Returns title/url/snippet results."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "count": {"type": "integer", "description": "Max results (1-10).", "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    needs_approval = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = args["query"].strip()
        if not query:
            return ToolResult(is_error=True, output="query is empty", title=self.name)
        count = max(1, min(int(args.get("count", 5)), 10))
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
        try:
            async with _make_client(_SEARCH_TIMEOUT) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                page = resp.text
        except Exception as exc:  # noqa: BLE001
            return ToolResult(is_error=True, output=f"search failed: {type(exc).__name__}: {exc}", title=self.name)

        results: list[str] = []
        for m in _RESULT_A.finditer(page):
            href = html_lib.unescape(m.group(1)).strip()
            title = html_lib.unescape(_TAGS.sub(" ", m.group(2))).strip()
            title = _WS.sub(" ", title)
            # DuckDuckGo wraps external links in a redirect; extract the real target.
            parsed = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed.query)
            if "uddg" in qs and qs["uddg"]:
                href = qs["uddg"][0]
            if not href.startswith(("http://", "https://")):
                continue
            snippet = ""
            # Look for the snippet in the surrounding result block.
            block_start = page.rfind('class="result__body"', 0, m.start())
            if block_start != -1:
                block = page[block_start : block_start + 6000]
                sm = _RESULT_SNIPPET.search(block)
                if sm:
                    snippet = html_lib.unescape(_TAGS.sub(" ", sm.group(1))).strip()
                    snippet = _WS.sub(" ", snippet)
            entry = f"- [{title}]({href})" if title else f"- {href}"
            if snippet:
                entry += f"\n  {snippet}"
            results.append(entry)
            if len(results) >= count:
                break
        if not results:
            return ToolResult(output=f"(no results for {query!r})", title=f"websearch: {query}")
        return ToolResult(output="\n".join(results), title=f"websearch: {query}")
