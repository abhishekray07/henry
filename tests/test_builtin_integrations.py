from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from henry.contracts import AgentDeps
from henry.integrations.builtins.github import (
    GithubIntegration,
    create_issue,
    get_file,
    open_pr,
    search_code,
)
from henry.integrations.builtins.web import WebIntegration, web_fetch, web_search
from henry.integrations.registry import discover
from henry.testing import FakeMemory, FakeSandbox
from henry.types import ChannelContext


class ToolContext:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps


def _deps(handler, settings: Any, channel_id: str = "C1") -> tuple[ToolContext, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return await handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
    deps = AgentDeps(
        ctx=ChannelContext(channel_id=channel_id, thread_ts="T1", run_id="R1"),
        memory=FakeMemory(),
        sandbox=FakeSandbox(),
        http=client,
        settings=settings,
    )
    return ToolContext(deps), requests


@pytest.mark.asyncio
async def test_github_search_sends_token_header_without_returning_it() -> None:
    token = "ghp_secret"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {token}"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": token,
                        "path": "src/app.py",
                        "repository": {"full_name": "owner/repo"},
                        "html_url": f"https://github.com/owner/repo/{token}",
                    }
                ]
            },
        )

    ctx, _ = _deps(handler, SimpleNamespace(github_token=token))
    try:
        result = await search_code(ctx, "build_agent")  # type: ignore[arg-type]
    finally:
        await ctx.deps.http.aclose()

    assert token not in repr(result)
    assert result[0]["name"] == "[redacted]"
    assert result[0]["url"].endswith("[redacted]")


@pytest.mark.asyncio
async def test_github_get_file_decodes_base64_content() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo/contents/README.md"
        return httpx.Response(
            200,
            json={
                "path": "README.md",
                "sha": "abc123",
                "encoding": "base64",
                "content": base64.b64encode(b"hello from github").decode("ascii"),
            },
        )

    ctx, _ = _deps(handler, SimpleNamespace(github_token=""))
    try:
        result = await get_file(ctx, "owner", "repo", "README.md")  # type: ignore[arg-type]
    finally:
        await ctx.deps.http.aclose()

    assert result["content"] == "hello from github"


@pytest.mark.asyncio
async def test_github_write_tools_are_channel_gated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "should not be called"})

    ctx, requests = _deps(handler, SimpleNamespace(github_token="token", github_write_enabled_channels=[]))
    try:
        result = await open_pr(ctx, "owner", "repo", "title", "head", "main")  # type: ignore[arg-type]
    finally:
        await ctx.deps.http.aclose()

    assert result["status"] == "blocked"
    assert requests == []


@pytest.mark.asyncio
async def test_github_write_tools_post_when_channel_is_allowed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/owner/repo/issues"
        return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/owner/repo/issues/7"})

    ctx, _ = _deps(
        handler,
        SimpleNamespace(github_token="token", github_write_enabled_channels=["C1"]),
    )
    try:
        result = await create_issue(ctx, "owner", "repo", "bug")  # type: ignore[arg-type]
    finally:
        await ctx.deps.http.aclose()

    assert result == {
        "status": "created",
        "number": "7",
        "url": "https://github.com/owner/repo/issues/7",
    }


@pytest.mark.asyncio
async def test_web_search_uses_tavily_provider() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.tavily.com/search"
        payload = httpx.Request("POST", request.url, content=request.content)
        assert payload.content
        return httpx.Response(
            200,
            json={"results": [{"title": "Result", "url": "https://example.com", "content": "summary"}]},
        )

    ctx, _ = _deps(
        handler,
        SimpleNamespace(web_search_provider="tavily", web_search_api_key="tvly_secret", web_allowed_domains=["example.com"]),
    )
    try:
        result = await web_search(ctx, "henry", limit=1)  # type: ignore[arg-type]
    finally:
        await ctx.deps.http.aclose()

    assert result == [{"title": "Result", "url": "https://example.com", "content": "summary"}]


@pytest.mark.asyncio
async def test_web_fetch_blocks_loopback_before_http_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="should not be called")

    ctx, requests = _deps(handler, SimpleNamespace(web_allowed_domains=["*"]))
    try:
        with pytest.raises(ValueError, match="non-public address"):
            await web_fetch(ctx, "http://127.0.0.1/admin")  # type: ignore[arg-type]
    finally:
        await ctx.deps.http.aclose()

    assert requests == []


@pytest.mark.asyncio
async def test_web_fetch_enforces_allowed_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="should not be called")

    ctx, requests = _deps(handler, SimpleNamespace(web_allowed_domains=["example.com"]))
    try:
        with pytest.raises(ValueError, match="not allowed"):
            await web_fetch(ctx, "https://not-example.test")  # type: ignore[arg-type]
    finally:
        await ctx.deps.http.aclose()

    assert requests == []


@pytest.mark.asyncio
async def test_web_fetch_extracts_html_and_follows_safe_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    from henry.integrations.builtins import web

    async def fake_resolve(hostname: str) -> list[str]:
        assert hostname.endswith("example.com")
        return ["93.184.216.34"]

    monkeypatch.setattr(web, "_resolve_host_ips", fake_resolve)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><script>ignore()</script><body><h1>Hello</h1><p>World</p></body></html>",
        )

    ctx, _ = _deps(handler, SimpleNamespace(web_allowed_domains=["example.com"]))
    try:
        result = await web_fetch(ctx, "https://docs.example.com/start")  # type: ignore[arg-type]
    finally:
        await ctx.deps.http.aclose()

    assert result["url"] == "https://docs.example.com/final"
    assert result["text"] == "Hello\nWorld"
    assert result["truncated"] is False


def test_builtin_discover_finds_github_and_web() -> None:
    registry = discover()

    assert isinstance(registry["github"], GithubIntegration)
    assert isinstance(registry["web"], WebIntegration)
