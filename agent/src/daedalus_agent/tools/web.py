"""web_fetch: HTTP(S) GET through whatever proxy the environment sets.

In `daedalus agent` that proxy is ariadne, so only allowlisted domains resolve.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

import httpx
from pydantic import BaseModel, Field

from daedalus_agent.tools.base import ToolContext, ToolError, ToolSpec


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self.parts.append(f" <{href}> ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class WebFetchArgs(BaseModel):
    url: str = Field(description="http:// or https:// URL to GET.")
    raw: bool = Field(default=False, description="Return raw body instead of extracted text.")


async def _web_fetch(ctx: ToolContext, args: WebFetchArgs) -> str:
    if not re.match(r"^https?://", args.url):
        raise ToolError("only http:// and https:// URLs are allowed")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=ctx.web_timeout, trust_env=True
        ) as client:
            response = await client.get(args.url, headers={"User-Agent": "daedalus-agent/0.1"})
    except httpx.HTTPError as exc:
        raise ToolError(f"fetch failed: {exc}") from None
    ctype = response.headers.get("content-type", "")
    body = response.text
    if not args.raw and "html" in ctype:
        body = html_to_text(body)
    header = f"GET {response.url} -> {response.status_code} ({ctype or 'unknown type'})\n\n"
    return header + ctx.truncate(body, ctx.web_max_chars)


WEB_FETCH = ToolSpec(
    name="web_fetch",
    description="Fetch a URL over HTTP(S) and return its text (HTML is converted to text).",
    args=WebFetchArgs,
    run=_web_fetch,
    read_only=True,
    category="web",
)
