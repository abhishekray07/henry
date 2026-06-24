from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from pydantic_ai import RunContext

from henry.contracts import AgentDeps, ToolSpec

_MAX_FETCH_BYTES = 1_000_000
_MAX_TEXT_CHARS = 20_000
_MAX_REDIRECTS = 3
_TIMEOUT_S = 15.0
_TAVILY_URL = "https://api.tavily.com/search"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def _configured_domains(ctx: RunContext[AgentDeps], fallback: tuple[str, ...]) -> tuple[str, ...]:
    value = getattr(ctx.deps.settings, "web_allowed_domains", fallback)
    if isinstance(value, str):
        return tuple(item.strip().lower() for item in value.split(",") if item.strip())
    return tuple(str(item).lower() for item in value)


def _domain_allowed(hostname: str, allowed_domains: tuple[str, ...]) -> bool:
    hostname = hostname.lower().rstrip(".")
    for domain in allowed_domains:
        domain = domain.lower().rstrip(".")
        if domain == "*":
            return True
        if hostname == domain or hostname.endswith(f".{domain}"):
            return True
    return False


def _assert_public_ip(address: str) -> None:
    ip = ipaddress.ip_address(address)
    metadata = ipaddress.ip_address("169.254.169.254")
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip == metadata
    ):
        raise ValueError(f"blocked non-public address {address}")


async def _resolve_host_ips(hostname: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, None, type=0)
    return sorted({item[4][0] for item in infos})


async def _validate_fetch_url(ctx: RunContext[AgentDeps], url: str, allowed_domains: tuple[str, ...]) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("web_fetch only supports http and https URLs")
    if not parsed.hostname:
        raise ValueError("web_fetch requires a hostname")
    if not _domain_allowed(parsed.hostname, allowed_domains):
        raise ValueError(f"domain {parsed.hostname} is not allowed")
    for address in await _resolve_host_ips(parsed.hostname):
        _assert_public_ip(address)
    return parsed.geturl()


def _extract_text(content_type: str, body: bytes) -> str:
    raw = body.decode("utf-8", errors="replace")
    if "html" not in content_type.lower():
        return raw
    parser = _TextExtractor()
    parser.feed(raw)
    return parser.text()


def _trim_text(text: str) -> tuple[str, bool]:
    if len(text) <= _MAX_TEXT_CHARS:
        return text, False
    return f"{text[:_MAX_TEXT_CHARS]}\n...[truncated]", True


async def web_search(ctx: RunContext[AgentDeps], query: str, limit: int = 5) -> list[dict]:
    """Search the web through the configured provider."""

    provider = str(getattr(ctx.deps.settings, "web_search_provider", "tavily")).lower()
    max_results = max(1, min(limit, 10))
    if provider == "tavily":
        api_key = getattr(ctx.deps.settings, "web_search_api_key", "")
        if not api_key:
            raise RuntimeError("web search provider tavily requires HENRY_WEB_SEARCH_API_KEY")
        response = await ctx.deps.http.post(
            _TAVILY_URL,
            json={"api_key": api_key, "query": query, "max_results": max_results},
            timeout=_TIMEOUT_S,
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("results", []) if isinstance(data, dict) else []
        return [
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "content": str(item.get("content", ""))[:2_000],
            }
            for item in results[:max_results]
        ]
    raise RuntimeError(f"unsupported web search provider {provider}")


async def web_fetch(ctx: RunContext[AgentDeps], url: str) -> dict[str, Any]:
    """Fetch a public web page after SSRF, redirect, domain, timeout, and size checks."""

    allowed_domains = _configured_domains(ctx, WebIntegration.allowed_domains)
    current_url = await _validate_fetch_url(ctx, url, allowed_domains)
    redirects = 0
    while True:
        async with ctx.deps.http.stream(
            "GET",
            current_url,
            follow_redirects=False,
            timeout=_TIMEOUT_S,
        ) as response:
            if response.is_redirect:
                redirects += 1
                if redirects > _MAX_REDIRECTS:
                    raise RuntimeError("web_fetch exceeded redirect limit")
                location = response.headers.get("location")
                if not location:
                    raise RuntimeError("web_fetch received redirect without location")
                current_url = await _validate_fetch_url(ctx, urljoin(current_url, location), allowed_domains)
                continue
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            truncated_bytes = False
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                chunks.append(chunk)
                if total > _MAX_FETCH_BYTES:
                    truncated_bytes = True
                    break
            body = b"".join(chunks)[:_MAX_FETCH_BYTES]
            text, truncated_text = _trim_text(_extract_text(response.headers.get("content-type", ""), body))
            return {
                "url": current_url,
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "text": text,
                "truncated": truncated_bytes or truncated_text,
            }


@dataclass(frozen=True)
class WebIntegration:
    name: str = "web"
    auth_type: str = "none"
    allowed_domains: tuple[str, ...] = ("*",)

    def tools(self) -> list[ToolSpec]:
        return [web_search, web_fetch]

    def prompt_fragment(self) -> str:
        return (
            "Web tools can search through the configured provider and fetch allowed public HTTP(S) pages. "
            "Fetches block private, loopback, link-local, and metadata addresses."
        )


def get_integration() -> WebIntegration:
    return WebIntegration()
