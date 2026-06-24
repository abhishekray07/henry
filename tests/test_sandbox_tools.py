from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from henry.contracts import AgentDeps
from henry.sandbox.tools import (
    _MAX_CLONE_FILE_BYTES,
    _safe_files_from_github_tarball,
    _safe_relative_dest,
    clear_sandbox_session,
    sandbox_tools,
)
from henry.sandbox_types import ExecResult
from henry.testing import FakeMemory, FakeSandbox
from henry.types import ChannelContext


def _tool(name: str) -> Any:
    return {tool.__name__: tool for tool in sandbox_tools()}[name]


def _tarball(files: dict[str, bytes], *, symlink: bool = False) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        root = tarfile.TarInfo("owner-repo-sha/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for path, content in files.items():
            info = tarfile.TarInfo(f"owner-repo-sha/{path}")
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "../outside"
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def _deps(
    sandbox: FakeSandbox,
    http: httpx.AsyncClient | None = None,
    *,
    run_id: str = "run-1",
    github_token: str = "",
) -> AgentDeps:
    return AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1", run_id=run_id),
        memory=FakeMemory(),
        sandbox=sandbox,
        http=http or httpx.AsyncClient(),
        settings=SimpleNamespace(sandbox_image="test-image", github_token=github_token),
    )


@pytest.mark.asyncio
async def test_run_bash_starts_and_reuses_run_scoped_session() -> None:
    sandbox = FakeSandbox(canned_result=ExecResult(exit_code=0, stdout="hello", stderr=""))
    deps = _deps(sandbox)
    ctx = SimpleNamespace(deps=deps)

    first = await _tool("run_bash")(ctx, "echo hello")
    await _tool("write_file")(ctx, "notes.txt", "content")
    await clear_sandbox_session(deps)

    assert "exit 0" in first
    assert [call[0] for call in sandbox.calls].count("start") == 1
    assert ("write_file", "fake-session-1", "notes.txt", b"content") in sandbox.calls
    assert sandbox.calls[-1] == ("destroy", "fake-session-1")


@pytest.mark.asyncio
async def test_timeout_result_invalidates_cached_session() -> None:
    sandbox = FakeSandbox(canned_result=ExecResult(exit_code=124, stdout="", stderr="timeout", timed_out=True))
    deps = _deps(sandbox, run_id="timeout-run")
    ctx = SimpleNamespace(deps=deps)

    first = await _tool("run_bash")(ctx, "sleep 999", 1)
    sandbox.canned_result = ExecResult(exit_code=0, stdout="new", stderr="")
    second = await _tool("run_bash")(ctx, "echo new")
    await clear_sandbox_session(deps)

    assert "timed out" in first
    assert "exit 0" in second
    assert [call[0] for call in sandbox.calls].count("start") == 2


@pytest.mark.asyncio
async def test_clone_repo_fetches_host_side_and_copies_regular_files() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        assert request.url.host == "api.github.com"
        return httpx.Response(200, content=_tarball({"README.md": b"# hello", "src/app.py": b"print(1)"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sandbox = FakeSandbox()
        deps = _deps(sandbox, http, run_id="clone-run", github_token="ghp_secret")
        result = await _tool("clone_repo")(SimpleNamespace(deps=deps), "owner/repo", "main", "checkout")
        await clear_sandbox_session(deps)

    assert seen_headers["authorization"] == "Bearer ghp_secret"
    assert "ghp_secret" not in result
    assert sandbox.files[("fake-session-1", "checkout/README.md")] == b"# hello"
    assert sandbox.files[("fake-session-1", "checkout/src/app.py")] == b"print(1)"


@pytest.mark.asyncio
async def test_clone_repo_rejects_symlink_archive_members() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_tarball({"link": b""}, symlink=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        deps = _deps(FakeSandbox(), http, run_id="bad-clone")
        with pytest.raises(ValueError, match="links are not supported"):
            await _tool("clone_repo")(SimpleNamespace(deps=deps), "owner/repo")


@pytest.mark.asyncio
async def test_clone_repo_rejects_bad_repo_name() -> None:
    deps = _deps(FakeSandbox())
    with pytest.raises(ValueError, match="owner/name"):
        await _tool("clone_repo")(SimpleNamespace(deps=deps), "not-a-repo")


def test_safe_relative_dest_accepts_nested_and_rejects_escape() -> None:
    assert _safe_relative_dest("sub/dir") == "sub/dir"
    for bad in ["/abs", "..", "../x", "", ".", "a/../../b"]:
        with pytest.raises(ValueError):
            _safe_relative_dest(bad)


def _multi_root_tarball() -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for root in ("r1", "r2"):
            directory = tarfile.TarInfo(f"{root}/")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
            info = tarfile.TarInfo(f"{root}/file")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    return stream.getvalue()


def _traversal_tarball() -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        directory = tarfile.TarInfo("root/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        info = tarfile.TarInfo("root/../../etc/passwd")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    return stream.getvalue()


def test_safe_files_rejects_multiple_roots() -> None:
    with pytest.raises(ValueError, match="multiple roots"):
        _safe_files_from_github_tarball(_multi_root_tarball())


def test_safe_files_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="unsafe archive path"):
        _safe_files_from_github_tarball(_traversal_tarball())


def test_safe_files_enforces_total_size_cap() -> None:
    big = b"a" * _MAX_CLONE_FILE_BYTES
    files = {f"f{i}.txt": big for i in range(20)}  # 80 MB decompressed > 64 MB total cap
    with pytest.raises(ValueError, match="total size limit"):
        _safe_files_from_github_tarball(_tarball(files))
