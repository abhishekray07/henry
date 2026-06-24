from __future__ import annotations

import io
import posixpath
import re
import tarfile
from dataclasses import dataclass
from typing import Any

try:
    from pydantic_ai import RunContext
except ImportError:  # pragma: no cover - only used before dependencies are installed
    RunContext = Any

from henry.contracts import AgentDeps, ToolSpec
from henry.sandbox_types import ExecRequest, ExecResult, SandboxPolicy

_MAX_CLONE_ARCHIVE_BYTES = 50 * 1024 * 1024
_MAX_CLONE_FILE_BYTES = 4 * 1024 * 1024
_MAX_CLONE_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_CLONE_FILES = 2_000
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass
class _CachedSession:
    sandbox: Any
    session: str


_sessions: dict[tuple[int, str], _CachedSession] = {}


def sandbox_tools() -> list[ToolSpec]:
    async def run_bash(ctx: RunContext[AgentDeps], command: str, timeout_s: int | None = None) -> str:
        """Run a bash command inside the run-scoped, network-disabled sandbox."""
        session = await _session_for(ctx.deps)
        result = await ctx.deps.sandbox.exec(
            session,
            ExecRequest(cmd=("bash", "-lc", command), timeout_s=timeout_s),
        )
        if result.timed_out:
            _forget_session(ctx.deps)
        return _format_exec_result(result)

    async def write_file(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
        """Write UTF-8 text into the run-scoped sandbox workspace."""
        session = await _session_for(ctx.deps)
        data = content.encode("utf-8")
        await ctx.deps.sandbox.write_file(session, path, data)
        return f"wrote {len(data)} bytes to {path}"

    async def read_file(ctx: RunContext[AgentDeps], path: str) -> str:
        """Read a UTF-8 text file from the run-scoped sandbox workspace."""
        session = await _session_for(ctx.deps)
        data = await ctx.deps.sandbox.read_file(session, path)
        return data.decode("utf-8", errors="replace")

    async def clone_repo(ctx: RunContext[AgentDeps], repo: str, ref: str = "HEAD", dest: str = "repo") -> str:
        """Fetch a GitHub repo archive host-side and copy safe regular files into the sandbox."""
        if not _REPO_RE.match(repo):
            raise ValueError("repo must be in owner/name form")
        safe_dest = _safe_relative_dest(dest)
        archive = await _download_github_tarball(ctx.deps, repo, ref)
        files = _safe_files_from_github_tarball(archive)
        session = await _session_for(ctx.deps)
        for rel_path, data in files:
            await ctx.deps.sandbox.write_file(session, posixpath.join(safe_dest, rel_path), data)
        return f"copied {len(files)} files from {repo}@{ref} into {safe_dest}"

    return [run_bash, write_file, read_file, clone_repo]


async def clear_sandbox_session(deps: AgentDeps, *, destroy: bool = True) -> None:
    cached = _sessions.pop(_session_key(deps), None)
    if cached is not None and destroy:
        await deps.sandbox.destroy(cached.session)


async def _session_for(deps: AgentDeps) -> str:
    run_id = deps.ctx.run_id
    if not run_id:
        raise ValueError("ChannelContext.run_id is required for sandbox session ownership")
    key = _session_key(deps)
    cached = _sessions.get(key)
    if cached is not None:
        return cached.session
    image = getattr(deps.settings, "sandbox_image", "henry-sandbox:base")
    session = await deps.sandbox.start(SandboxPolicy(image=image))
    _sessions[key] = _CachedSession(sandbox=deps.sandbox, session=session)
    return session


def _forget_session(deps: AgentDeps) -> None:
    _sessions.pop(_session_key(deps), None)


def _session_key(deps: AgentDeps) -> tuple[int, str]:
    return id(deps.sandbox), deps.ctx.run_id


def _format_exec_result(result: ExecResult) -> str:
    status = "timed out" if result.timed_out else f"exit {result.exit_code}"
    parts = [status]
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr}")
    if result.truncated:
        parts.append("[output truncated]")
    return "\n".join(parts)


async def _download_github_tarball(deps: AgentDeps, repo: str, ref: str) -> bytes:
    owner, name = repo.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{name}/tarball/{ref}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = getattr(deps.settings, "github_token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with deps.http.stream("GET", url, headers=headers, follow_redirects=True, timeout=30.0) as response:
        response.raise_for_status()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_CLONE_ARCHIVE_BYTES:
                raise ValueError("repository archive exceeds sandbox clone size limit")
    return bytes(content)


def _safe_files_from_github_tarball(archive_bytes: bytes) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        root: str | None = None
        for member in archive.getmembers():
            normalized = posixpath.normpath(member.name)
            if member.name.startswith("/") or normalized == ".." or normalized.startswith("../"):
                raise ValueError(f"unsafe archive path: {member.name}")
            parts = normalized.split("/")
            if root is None:
                root = parts[0]
            if not parts or parts[0] != root:
                raise ValueError("repository archive contains multiple roots")
            if len(parts) == 1:
                continue
            rel_path = posixpath.normpath(posixpath.join(*parts[1:]))
            if rel_path == "." or rel_path.startswith("../") or rel_path.startswith("/"):
                raise ValueError(f"unsafe repository path: {member.name}")
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                raise ValueError(f"repository archive links are not supported: {rel_path}")
            if not member.isfile():
                raise ValueError(f"repository archive member is not a regular file: {rel_path}")
            if member.size > _MAX_CLONE_FILE_BYTES:
                raise ValueError(f"repository file exceeds sandbox clone size limit: {rel_path}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"repository file could not be extracted: {rel_path}")
            data = extracted.read(_MAX_CLONE_FILE_BYTES + 1)
            if len(data) > _MAX_CLONE_FILE_BYTES:
                raise ValueError(f"repository file exceeds sandbox clone size limit: {rel_path}")
            total_bytes += len(data)
            if total_bytes > _MAX_CLONE_TOTAL_BYTES:
                raise ValueError("repository archive exceeds sandbox clone total size limit")
            files.append((rel_path, data))
            if len(files) > _MAX_CLONE_FILES:
                raise ValueError("repository archive exceeds sandbox clone file-count limit")
    return files


def _safe_relative_dest(dest: str) -> str:
    if "\x00" in dest or dest.startswith("/"):
        raise ValueError("dest must be a relative sandbox path")
    normalized = posixpath.normpath(dest)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise ValueError("dest must stay inside the sandbox workspace")
    return normalized
