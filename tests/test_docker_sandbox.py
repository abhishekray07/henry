from __future__ import annotations

import base64
import io
import json
import tarfile
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from henry.sandbox import DockerSandbox
from henry.sandbox.docker import (
    KERNEL_ENTRY,
    SandboxArchiveError,
    SandboxError,
    SandboxPathError,
    SandboxSessionDestroyed,
    SandboxSessionNotFound,
)
from henry.sandbox_types import CellResult, SandboxPolicy


def _ok_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "outputs": [
            {"output_type": "stream", "name": "stdout", "text": "hi\n"},
            {
                "output_type": "execute_result",
                "data": {"text/plain": "42"},
                "metadata": {},
                "execution_count": 1,
            },
        ],
        "execution_count": 1,
        "timed_out": False,
        "truncated": False,
    }


class _KernelContainer:
    def __init__(
        self,
        *,
        ping_ok: bool = True,
        exec_payload: Any = None,
        exec_stdout: bytes | None = None,
        exec_exit: int = 0,
        exec_exception: Exception | None = None,
        ping_delay_s: float = 0,
        exec_entered: threading.Event | None = None,
        exec_release: threading.Event | None = None,
    ) -> None:
        self.id = "container-1"
        self.exec_calls: list[list[str]] = []
        self.removed = False
        self._ping_ok = ping_ok
        self._payload = _ok_payload() if exec_payload is None else exec_payload
        self._stdout = exec_stdout
        self._exec_exit = exec_exit
        self._exec_exception = exec_exception
        self._ping_delay_s = ping_delay_s
        self._exec_entered = exec_entered
        self._exec_release = exec_release

    def exec_run(self, cmd, **kwargs):
        self.exec_calls.append(cmd)
        if cmd[-1] == "ping":
            if self._ping_delay_s:
                time.sleep(self._ping_delay_s)
            return SimpleNamespace(exit_code=0 if self._ping_ok else 1, output=(b"", b""))
        if self._exec_entered is not None:
            self._exec_entered.set()
        if self._exec_release is not None:
            self._exec_release.wait(timeout=2)
        if self._exec_exception is not None:
            raise self._exec_exception
        stdout = self._stdout
        if stdout is None:
            stdout = json.dumps(self._payload).encode()
        return SimpleNamespace(exit_code=self._exec_exit, output=(stdout, b"kernel stderr"))

    def remove(self, force=False):
        self.removed = True
        if self._exec_release is not None:
            self._exec_release.set()


class _RecordingVolumes:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.removed = False

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        outer = self

        class _Volume:
            def remove(self, force=False):
                outer.removed = True

        return _Volume()


class _RecordingClient:
    def __init__(self, container: _KernelContainer | None = None) -> None:
        self.container = container or _KernelContainer()
        self.volumes = _RecordingVolumes()
        outer = self

        class _Containers:
            def __init__(self) -> None:
                self.kwargs: dict[str, Any] = {}

            def run(self, image: str, command: Any, **kwargs: Any) -> Any:
                self.kwargs = {"image": image, "command": command, **kwargs}
                return outer.container

        self.containers = _Containers()


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
    assert kwargs["tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=64m"}
    assert kwargs["mounts"][0]["Target"] == "/workspace"
    assert kwargs["mounts"][0]["Type"] == "volume"
    assert kwargs["mounts"][0]["ReadOnly"] is False
    assert client.volumes.kwargs["labels"]["henry.sandbox"] == "true"
    assert kwargs["command"] == ["python", KERNEL_ENTRY, "boot"]
    assert kwargs["environment"] == {
        "HOME": "/tmp",
        "JUPYTER_RUNTIME_DIR": "/tmp/jupyter-runtime",
        "IPYTHONDIR": "/tmp/ipython",
    }
    assert any(call[-1] == "ping" for call in client.container.exec_calls)


@pytest.mark.asyncio
async def test_start_cleans_up_when_never_ready() -> None:
    client = _RecordingClient(_KernelContainer(ping_ok=False))
    sandbox = DockerSandbox(client=client)

    with pytest.raises(SandboxError, match="did not become ready"):
        await sandbox.start(SandboxPolicy(), ready_timeout_s=0.02)

    assert client.container.removed is True
    assert client.volumes.removed is True


@pytest.mark.asyncio
async def test_start_deadline_bounds_a_stuck_ping() -> None:
    client = _RecordingClient(_KernelContainer(ping_delay_s=0.5))
    sandbox = DockerSandbox(client=client)
    started = time.monotonic()

    with pytest.raises(SandboxError, match="did not become ready"):
        await sandbox.start(SandboxPolicy(), ready_timeout_s=0.02)

    assert time.monotonic() - started < 0.3
    assert client.container.removed is True
    assert client.volumes.removed is True


@pytest.mark.asyncio
async def test_exec_cell_parses_ordered_outputs() -> None:
    client = _RecordingClient()
    sandbox = DockerSandbox(client=client)
    session = await sandbox.start(SandboxPolicy())

    result = await sandbox.exec_cell(session, "print('hi'); 40 + 2")

    assert [output.output_type for output in result.outputs] == ["stream", "execute_result"]
    assert result.outputs[1].data["text/plain"] == "42"
    exec_cmd = [call for call in client.container.exec_calls if "exec" in call][0]
    assert base64.b64decode(exec_cmd[3]).decode() == "print('hi'); 40 + 2"


@pytest.mark.asyncio
async def test_exec_cell_timeout_destroys_session() -> None:
    payload = {
        "status": "error",
        "outputs": [],
        "execution_count": 0,
        "timed_out": True,
        "truncated": False,
    }
    client = _RecordingClient(_KernelContainer(exec_payload=payload))
    sandbox = DockerSandbox(client=client)
    session = await sandbox.start(SandboxPolicy())

    result = await sandbox.exec_cell(session, "loop")

    assert result.timed_out is True
    assert client.container.removed is True
    with pytest.raises(SandboxSessionNotFound):
        await sandbox.exec_cell(session, "1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("container", "message"),
    [
        (_KernelContainer(exec_exit=1), "kernel exec failed"),
        (_KernelContainer(exec_stdout=b"not json"), "malformed kernel response"),
        (_KernelContainer(exec_stdout=b"\xff"), "malformed kernel response"),
        (_KernelContainer(exec_payload={"status": "wat", "outputs": []}), "malformed kernel response"),
        (_KernelContainer(exec_exception=RuntimeError("docker broke")), "kernel exec failed"),
    ],
)
async def test_exec_cell_transport_errors_invalidate_session(container, message) -> None:
    client = _RecordingClient(container)
    sandbox = DockerSandbox(client=client)
    session = await sandbox.start(SandboxPolicy())

    result = await sandbox.exec_cell(session, "code")

    assert result.status == "error"
    assert result.outputs[0].ename == "KernelTransportError"
    assert message in (result.outputs[0].evalue or "")
    assert client.container.removed is True


@pytest.mark.asyncio
async def test_exec_cell_oversize_payload_invalidates_session(monkeypatch) -> None:
    from henry.sandbox import docker as docker_module

    monkeypatch.setattr(docker_module, "_MAX_PAYLOAD_BYTES", 10)
    client = _RecordingClient()
    sandbox = DockerSandbox(client=client)
    session = await sandbox.start(SandboxPolicy())

    result = await sandbox.exec_cell(session, "code")

    assert result.outputs[0].ename == "KernelTransportError"
    assert "exceeded" in (result.outputs[0].evalue or "")
    assert client.container.removed is True


@pytest.mark.asyncio
async def test_concurrent_cell_after_timeout_is_refused() -> None:
    import asyncio

    entered = threading.Event()
    release = threading.Event()
    payload = {
        "status": "error",
        "outputs": [],
        "execution_count": 0,
        "timed_out": True,
        "truncated": False,
    }
    container = _KernelContainer(exec_payload=payload, exec_entered=entered, exec_release=release)
    client = _RecordingClient(container)
    sandbox = DockerSandbox(client=client)
    session = await sandbox.start(SandboxPolicy())

    first = asyncio.create_task(sandbox.exec_cell(session, "loop"))
    assert await asyncio.to_thread(entered.wait, 1)
    second = asyncio.create_task(sandbox.exec_cell(session, "after"))
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(isinstance(result, CellResult) and result.timed_out for result in results) == 1
    assert sum(isinstance(result, SandboxSessionDestroyed) for result in results) == 1
    assert len([call for call in container.exec_calls if "exec" in call]) == 1


@pytest.mark.asyncio
async def test_host_wall_timeout_invalidates_session(monkeypatch) -> None:
    from henry.sandbox import docker as docker_module

    monkeypatch.setattr(docker_module, "_CELL_EXEC_GRACE_S", 0.01)
    entered = threading.Event()
    release = threading.Event()
    container = _KernelContainer(exec_entered=entered, exec_release=release)
    client = _RecordingClient(container)
    sandbox = DockerSandbox(client=client)
    session = await sandbox.start(SandboxPolicy())

    result = await sandbox.exec_cell(session, "blocked transport", timeout_s=0)

    assert result.timed_out is True
    assert container.removed is True


@pytest.mark.asyncio
async def test_cancelling_cell_execution_invalidates_session() -> None:
    import asyncio

    entered = threading.Event()
    release = threading.Event()
    container = _KernelContainer(exec_entered=entered, exec_release=release)
    client = _RecordingClient(container)
    sandbox = DockerSandbox(client=client)
    session = await sandbox.start(SandboxPolicy())
    task = asyncio.create_task(sandbox.exec_cell(session, "blocked transport"))
    assert await asyncio.to_thread(entered.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert container.removed is True
    with pytest.raises(SandboxSessionNotFound):
        await sandbox.exec_cell(session, "1")


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
