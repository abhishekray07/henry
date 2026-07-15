from __future__ import annotations

import asyncio
import io
import posixpath
import re
import tarfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field
from typing import Any

try:
    from pydantic_ai import RunContext
except ImportError:  # pragma: no cover - only used before dependencies are installed
    RunContext = Any

from henry.contracts import AgentDeps, ToolSpec
from henry.sandbox.docker import SandboxSessionDestroyed, SandboxSessionNotFound
from henry.sandbox_types import CellResult, SandboxPolicy

_MAX_CLONE_ARCHIVE_BYTES = 50 * 1024 * 1024
_MAX_CLONE_FILE_BYTES = 4 * 1024 * 1024
_MAX_CLONE_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_CLONE_FILES = 2_000
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass
class _CachedSession:
    sandbox: Any
    session: str


@dataclass
class _SessionSlot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cached: _CachedSession | None = None
    users: int = 0


_session_slots: dict[tuple[int, str], _SessionSlot] = {}
_session_registry_lock = asyncio.Lock()


def sandbox_tools() -> list[ToolSpec]:
    async def run_python(ctx: RunContext[AgentDeps], code: str, timeout_s: int | None = None) -> str:
        """Run Python in the run-scoped, network-isolated IPython kernel.

        Variables, imports, and definitions persist across calls within a task.
        Use ``!cmd`` for shell commands and ``%`` for IPython magics.

        A cell that times out or hits a kernel error restarts the kernel and
        discards that state; the result says so when it happens.
        """
        session = await _session_for(ctx.deps)
        try:
            result = await ctx.deps.sandbox.exec_cell(session, code, timeout_s=timeout_s)
        except (SandboxSessionNotFound, SandboxSessionDestroyed):
            # A cell running in parallel tore this session down first. Report the
            # reset the same way a first-hand timeout would, rather than raising.
            await _forget_session(ctx.deps, session)
            return f"error: the kernel session ended before this cell ran. {_KERNEL_RESET_NOTICE}"
        if _result_invalidates_session(result):
            await _forget_session(ctx.deps, session)
        return _format_cell_result(result)

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

    return [run_python, write_file, read_file, clone_repo]


async def clear_sandbox_session(deps: AgentDeps, *, destroy: bool = True) -> None:
    async with _locked_slot(_session_key(deps)) as slot:
        cached = slot.cached
        slot.cached = None
        if cached is not None and destroy:
            await cached.sandbox.destroy(cached.session)


async def _session_for(deps: AgentDeps) -> str:
    run_id = deps.ctx.run_id
    if not run_id:
        raise ValueError("ChannelContext.run_id is required for sandbox session ownership")
    key = _session_key(deps)
    async with _locked_slot(key) as slot:
        if slot.cached is not None:
            return slot.cached.session
        image = getattr(deps.settings, "sandbox_image", "henry-sandbox:base")
        session = await deps.sandbox.start(SandboxPolicy(image=image))
        slot.cached = _CachedSession(sandbox=deps.sandbox, session=session)
        return session


async def _forget_session(deps: AgentDeps, session: str) -> None:
    """Drop the cache entry for `session`, and only for that session.

    Cells run concurrently against one cached session, so a failure can surface
    after that session was already replaced. Clearing whatever happens to be
    cached would evict the healthy successor, and nothing else tracks it — the
    run-end cleanup reads this cache, so the container would leak.
    """
    async with _locked_slot(_session_key(deps)) as slot:
        if slot.cached is not None and slot.cached.session == session:
            slot.cached = None


@asynccontextmanager
async def _locked_slot(key: tuple[int, str]) -> AsyncIterator[_SessionSlot]:
    async with _session_registry_lock:
        slot = _session_slots.setdefault(key, _SessionSlot())
        slot.users += 1
    acquired = False
    try:
        await slot.lock.acquire()
        acquired = True
        yield slot
    finally:
        if acquired:
            slot.lock.release()
        async with _session_registry_lock:
            slot.users -= 1
            if slot.users == 0 and slot.cached is None and _session_slots.get(key) is slot:
                _session_slots.pop(key, None)


def _session_key(deps: AgentDeps) -> tuple[int, str]:
    return id(deps.sandbox), deps.ctx.run_id


def _result_invalidates_session(result: CellResult) -> bool:
    # Only the sandbox may declare teardown. Sandboxed code chooses its own
    # exception names, so matching on `ename` here let a cell raising a
    # look-alike KernelTransportError evict a healthy container from the cache
    # and leak it.
    return result.session_invalidated


_KERNEL_RESET_NOTICE = (
    "The kernel was restarted and its workspace was destroyed, so variables, imports, definitions, "
    "written files, and cloned repositories from earlier in this task are all gone."
)


def _render_outputs(result: CellResult) -> list[str]:
    parts: list[str] = []
    for output in result.outputs:
        if output.output_type == "stream":
            parts.append(output.text or "")
        elif output.output_type in {"execute_result", "display_data"}:
            data = output.data or {}
            if data.get("text/plain") is not None:
                parts.append(str(data["text/plain"]))
            for mime_type in data:
                if mime_type != "text/plain":
                    parts.append(f"[rich output: {mime_type}]")
        elif output.output_type == "error":
            traceback = output.traceback or [output.evalue or ""]
            parts.append("error:\n" + "\n".join(traceback))
    if result.truncated:
        parts.append("[output truncated]")
    return parts


def _format_cell_result(result: CellResult) -> str:
    if result.timed_out:
        # Output emitted before the deadline is the main debugging signal for a
        # stalled cell, so keep it instead of reporting the timeout alone.
        parts = _render_outputs(result)
        parts.append(f"timed out: cell exceeded its time limit. {_KERNEL_RESET_NOTICE}")
        return "\n".join(part for part in parts if part)
    parts = _render_outputs(result)
    if _result_invalidates_session(result):
        parts.append(_KERNEL_RESET_NOTICE)
    # A failed cell whose error output never made it back must not read as a
    # clean run. The traceback is dropped whenever earlier output has already
    # spent the byte budget, so a cell that prints a lot and then raises would
    # otherwise return its stdout and nothing else.
    if result.status != "ok" and not any(o.output_type == "error" for o in result.outputs):
        if result.truncated:
            parts.append("error: the cell raised, but its error output exceeded the output limit")
        else:
            parts.append("error: the kernel reported a failure but produced no output")
    text = "\n".join(part for part in parts if part)
    return text or "(no output)"


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
