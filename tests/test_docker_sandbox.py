from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from typing import Any

import pytest

from henry.sandbox import DockerSandbox
from henry.sandbox.docker import SandboxArchiveError, SandboxPathError
from henry.sandbox_types import SandboxPolicy


class _RecordingContainers:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def run(self, image: str, command: Any, **kwargs: Any) -> Any:
        self.kwargs = {"image": image, "command": command, **kwargs}
        return SimpleNamespace(id="container-1")


class _RecordingClient:
    def __init__(self) -> None:
        self.containers = _RecordingContainers()


def test_safe_path_blocks_workspace_escape() -> None:
    sandbox = DockerSandbox(client=object())
    policy = SandboxPolicy(workdir="/workspace")

    assert sandbox._safe_path(policy, "src/app.py") == "/workspace/src/app.py"
    assert sandbox._safe_path(policy, "/workspace/src/app.py") == "/workspace/src/app.py"
    with pytest.raises(SandboxPathError):
        sandbox._safe_path(policy, "../secret")
    with pytest.raises(SandboxPathError):
        sandbox._safe_path(policy, "/etc/passwd")


@pytest.mark.asyncio
async def test_start_applies_isolation_flags() -> None:
    client = _RecordingClient()
    sandbox = DockerSandbox(client=client, pids_limit=256)

    session = await sandbox.start(SandboxPolicy())

    kwargs = client.containers.kwargs
    assert session == "container-1"
    assert kwargs["network_mode"] == "none"
    assert kwargs["read_only"] is True
    assert kwargs["pids_limit"] == 256
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in kwargs["security_opt"]


@pytest.mark.asyncio
async def test_start_rejects_non_none_network() -> None:
    sandbox = DockerSandbox(client=_RecordingClient())
    with pytest.raises(ValueError, match="network='none'"):
        await sandbox.start(SandboxPolicy(network="bridge"))


@pytest.mark.asyncio
async def test_write_file_rejects_oversize_content() -> None:
    sandbox = DockerSandbox(client=object(), file_limit_bytes=8)
    with pytest.raises(SandboxArchiveError, match="write limit"):
        await sandbox.write_file("any-session", "f.txt", b"x" * 9)


def test_extract_single_regular_file_rejects_multi_member_archive() -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        directory = tarfile.TarInfo("dir")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        info = tarfile.TarInfo("dir/f")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))

    sandbox = DockerSandbox(client=object())
    with pytest.raises(SandboxArchiveError, match="exactly one regular file"):
        sandbox._extract_single_regular_file(stream.getvalue())
