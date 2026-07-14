from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from pydantic_ai import RunContext

from henry.contracts import AgentDeps, ToolSpec

_API_BASE = "https://api.github.com"
_MAX_BODY_CHARS = 8_000
_DEFAULT_PER_PAGE = 10


def _redact(text: str, token: str) -> str:
    if not token:
        return text
    return text.replace(token, "[redacted]")


def _repo_path(owner: str, repo: str) -> str:
    if not owner or not repo or "/" in owner or "/" in repo:
        raise ValueError("owner and repo must be simple GitHub owner/name segments")
    return f"{quote(owner, safe='')}/{quote(repo, safe='')}"


def _content_path(path: str) -> str:
    cleaned = path.strip("/")
    if not cleaned or "\x00" in cleaned:
        raise ValueError("path must be a non-empty repository path")
    if ".." in cleaned.split("/"):
        raise ValueError("path must not contain parent directory segments")
    return quote(cleaned, safe="/")


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "henry-integration",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _request_json(ctx: RunContext[AgentDeps], method: str, path: str, **kwargs: Any) -> Any:
    token = ctx.deps.settings.github_token
    try:
        response = await ctx.deps.http.request(
            method,
            f"{_API_BASE}{path}",
            headers=_headers(token),
            timeout=20.0,
            **kwargs,
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}
    except Exception as exc:
        raise RuntimeError(_redact(f"github request failed: {exc}", token)) from exc


def _trim(value: str, max_chars: int = _MAX_BODY_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n...[truncated]"


def _write_enabled(settings: Any, channel_id: str) -> bool:
    channels = getattr(settings, "github_write_enabled_channels", ())
    if isinstance(channels, str):
        channels = [item.strip() for item in channels.split(",") if item.strip()]
    return channel_id in set(channels)


async def search_code(ctx: RunContext[AgentDeps], query: str, repo: str | None = None, limit: int = 10) -> list[dict]:
    """Search GitHub code and return redacted match summaries."""

    per_page = max(1, min(limit, 25))
    search_query = query if repo is None else f"{query} repo:{repo}"
    data = await _request_json(
        ctx,
        "GET",
        "/search/code",
        params={"q": search_query, "per_page": per_page},
    )
    token = ctx.deps.settings.github_token
    items = data.get("items", []) if isinstance(data, dict) else []
    return [
        {
            "name": _redact(str(item.get("name", "")), token),
            "path": _redact(str(item.get("path", "")), token),
            "repository": _redact(str(item.get("repository", {}).get("full_name", "")), token),
            "url": _redact(str(item.get("html_url", "")), token),
        }
        for item in items[:per_page]
    ]


async def get_file(
    ctx: RunContext[AgentDeps],
    owner: str,
    repo: str,
    path: str,
    ref: str | None = None,
) -> dict[str, str]:
    """Read a text file from a GitHub repository."""

    params = {"ref": ref} if ref else None
    data = await _request_json(
        ctx,
        "GET",
        f"/repos/{_repo_path(owner, repo)}/contents/{_content_path(path)}",
        params=params,
    )
    token = ctx.deps.settings.github_token
    if not isinstance(data, dict):
        raise RuntimeError("github returned an unexpected file response")
    if data.get("encoding") != "base64":
        return {
            "path": _redact(str(data.get("path", path)), token),
            "content": "",
            "message": "github file content was not base64 encoded",
        }

    import base64

    raw = base64.b64decode(str(data.get("content", "")).encode("ascii"), validate=False)
    content = raw.decode("utf-8", errors="replace")
    return {
        "path": _redact(str(data.get("path", path)), token),
        "sha": _redact(str(data.get("sha", "")), token),
        "content": _redact(_trim(content), token),
    }


async def list_commits(
    ctx: RunContext[AgentDeps],
    owner: str,
    repo: str,
    ref: str | None = None,
    path: str | None = None,
    limit: int = _DEFAULT_PER_PAGE,
) -> list[dict]:
    """List recent commits for a GitHub repository."""

    params: dict[str, Any] = {"per_page": max(1, min(limit, 25))}
    if ref:
        params["sha"] = ref
    if path:
        params["path"] = path
    data = await _request_json(ctx, "GET", f"/repos/{_repo_path(owner, repo)}/commits", params=params)
    token = ctx.deps.settings.github_token
    commits = data if isinstance(data, list) else []
    return [
        {
            "sha": _redact(str(item.get("sha", "")), token),
            "message": _redact(str(item.get("commit", {}).get("message", "")), token),
            "author": _redact(str(item.get("commit", {}).get("author", {}).get("name", "")), token),
            "url": _redact(str(item.get("html_url", "")), token),
        }
        for item in commits
    ]


async def open_pr(
    ctx: RunContext[AgentDeps],
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
) -> dict[str, str]:
    """Open a pull request when this Slack channel has explicit write capability."""

    if not _write_enabled(ctx.deps.settings, ctx.deps.ctx.channel_id):
        return {"status": "blocked", "message": "GitHub write tools are not enabled for this channel."}
    data = await _request_json(
        ctx,
        "POST",
        f"/repos/{_repo_path(owner, repo)}/pulls",
        json={"title": title, "head": head, "base": base, "body": body},
    )
    token = ctx.deps.settings.github_token
    return {
        "status": "created",
        "number": _redact(str(data.get("number", "")), token),
        "url": _redact(str(data.get("html_url", "")), token),
    }


async def create_issue(
    ctx: RunContext[AgentDeps],
    owner: str,
    repo: str,
    title: str,
    body: str = "",
) -> dict[str, str]:
    """Create an issue when this Slack channel has explicit write capability."""

    if not _write_enabled(ctx.deps.settings, ctx.deps.ctx.channel_id):
        return {"status": "blocked", "message": "GitHub write tools are not enabled for this channel."}
    data = await _request_json(
        ctx,
        "POST",
        f"/repos/{_repo_path(owner, repo)}/issues",
        json={"title": title, "body": body},
    )
    token = ctx.deps.settings.github_token
    return {
        "status": "created",
        "number": _redact(str(data.get("number", "")), token),
        "url": _redact(str(data.get("html_url", "")), token),
    }


@dataclass(frozen=True)
class GithubIntegration:
    name: str = "github"
    auth_type: str = "static_token"
    allowed_domains: tuple[str, ...] = ("api.github.com", "github.com")

    def tools(self) -> list[ToolSpec]:
        return [search_code, get_file, list_commits, open_pr, create_issue]

    def prompt_fragment(self) -> str:
        return (
            "GitHub tools can search code, read files, and list commits. Pull request and issue creation "
            "are unavailable unless the current Slack channel has explicit GitHub write capability."
        )

    def is_configured(self, settings: Any) -> bool:
        # Without a token, code search and most reads 401/rate-limit immediately;
        # wildcard channels should not receive tools that can only fail.
        return bool(getattr(settings, "github_token", ""))


def get_integration() -> GithubIntegration:
    return GithubIntegration()
