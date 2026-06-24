from __future__ import annotations

import asyncio
import io
import os
import posixpath
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from henry.sandbox_types import ExecRequest, ExecResult, SandboxPolicy

try:
    import docker
    from docker.errors import APIError, DockerException, NotFound
    from docker.models.containers import Container
    from docker.types import Mount
except ImportError:  # pragma: no cover - dependency is declared by the project
    docker = None  # type: ignore[assignment]
    APIError = DockerException = NotFound = RuntimeError  # type: ignore[misc,assignment]
    Container = Any  # type: ignore[misc,assignment]
    Mount = Any  # type: ignore[misc,assignment]


class SandboxError(RuntimeError):
    pass


class SandboxSessionNotFound(SandboxError):
    pass


class SandboxSessionDestroyed(SandboxError):
    pass


class SandboxPathError(ValueError):
    pass


class SandboxArchiveError(SandboxError):
    pass


@dataclass
class _Session:
    container: Container
    workspace_volume: Any
    policy: SandboxPolicy
    created_at: float
    destroyed: bool = False


class DockerSandbox:
    def __init__(
        self,
        client: Any | None = None,
        *,
        stdout_limit_bytes: int = 64 * 1024,
        file_limit_bytes: int = 4 * 1024 * 1024,
        pids_limit: int = 512,
    ) -> None:
        if docker is None and client is None:  # pragma: no cover - import guard
            raise SandboxError("docker SDK is not installed")
        self._client = client or _docker_client_from_env()
        self._sessions: dict[str, _Session] = {}
        self._stdout_limit_bytes = stdout_limit_bytes
        self._file_limit_bytes = file_limit_bytes
        self._pids_limit = pids_limit

    async def start(self, policy: SandboxPolicy) -> str:
        if policy.network != "none":
            raise ValueError("V1 sandbox only supports network='none'; fetch external data host-side")

        def _start() -> tuple[Container, Any]:
            volume_name = f"henry-sandbox-{uuid4().hex}"
            volume = self._client.volumes.create(
                name=volume_name,
                labels={"henry.sandbox": "true", "henry.sandbox.volume": "workspace"},
            )
            tmpfs = {
                "/tmp": "rw,noexec,nosuid,size=64m",
            }
            try:
                container = self._client.containers.run(
                    policy.image,
                    ["sleep", "infinity"],
                    detach=True,
                    read_only=True,
                    working_dir=policy.workdir,
                    mem_limit=f"{policy.mem_mb}m",
                    nano_cpus=int(policy.cpus * 1_000_000_000),
                    pids_limit=self._pids_limit,
                    network_mode="none",
                    tmpfs=tmpfs,
                    mounts=[
                        Mount(
                            target=policy.workdir,
                            source=volume_name,
                            type="volume",
                            read_only=False,
                        )
                    ],
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                    labels={"henry.sandbox": "true", "henry.sandbox.created_at": str(time.time())},
                )
            except Exception:
                volume.remove(force=True)
                raise
            return container, volume

        container, volume = await asyncio.to_thread(_start)
        self._sessions[container.id] = _Session(
            container=container,
            workspace_volume=volume,
            policy=policy,
            created_at=time.time(),
        )
        return str(container.id)

    async def exec(self, session: str, req: ExecRequest) -> ExecResult:
        state = self._get_session(session)
        timeout_s = req.timeout_s if req.timeout_s is not None else state.policy.default_timeout_s
        cwd = self._safe_path(state.policy, req.cwd or state.policy.workdir)
        started = time.monotonic()

        def _exec_run() -> Any:
            return state.container.exec_run(
                list(req.cmd),
                workdir=cwd,
                environment=req.env,
                demux=True,
            )

        try:
            docker_result = await asyncio.wait_for(asyncio.to_thread(_exec_run), timeout=timeout_s)
        except TimeoutError:
            await self._remove_session(session, force=True)
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"command timed out after {timeout_s}s",
                timed_out=True,
                duration_ms=self._duration_ms(started),
            )

        exit_code = int(getattr(docker_result, "exit_code", 1))
        stdout_bytes, stderr_bytes = self._split_exec_output(getattr(docker_result, "output", b""))
        stdout, stdout_truncated = self._decode_and_cap(stdout_bytes)
        stderr, stderr_truncated = self._decode_and_cap(stderr_bytes)
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            duration_ms=self._duration_ms(started),
            truncated=stdout_truncated or stderr_truncated,
        )

    async def write_file(self, session: str, path: str, content: bytes) -> None:
        if len(content) > self._file_limit_bytes:
            raise SandboxArchiveError(f"file exceeds {self._file_limit_bytes} byte sandbox write limit")
        state = self._get_session(session)
        container_path = self._safe_path(state.policy, path)
        directory = posixpath.dirname(container_path)
        filename = posixpath.basename(container_path)
        if not filename:
            raise SandboxPathError("path must name a file")

        def _write() -> None:
            mkdir_result = state.container.exec_run(["mkdir", "-p", directory], workdir=state.policy.workdir)
            if int(getattr(mkdir_result, "exit_code", 1)) != 0:
                raise SandboxArchiveError(f"failed to create sandbox directory: {directory}")

            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                info = tarfile.TarInfo(filename)
                info.size = len(content)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))
            stream.seek(0)
            if not state.container.put_archive(directory, stream.getvalue()):
                raise SandboxArchiveError(f"failed to write sandbox file: {container_path}")

        await asyncio.to_thread(_write)

    async def read_file(self, session: str, path: str) -> bytes:
        state = self._get_session(session)
        container_path = self._safe_path(state.policy, path)

        def _read() -> bytes:
            chunks, _stat = state.container.get_archive(container_path)
            data = bytearray()
            for chunk in chunks:
                data.extend(chunk)
                if len(data) > self._file_limit_bytes:
                    raise SandboxArchiveError(f"archive exceeds {self._file_limit_bytes} byte sandbox read limit")
            return self._extract_single_regular_file(bytes(data))

        return await asyncio.to_thread(_read)

    async def destroy(self, session: str) -> None:
        await self._remove_session(session, force=True)

    async def reap_expired(self) -> int:
        now = time.time()
        expired = [
            session_id
            for session_id, state in self._sessions.items()
            if now - state.created_at > state.policy.ttl_s
        ]
        for session_id in expired:
            await self._remove_session(session_id, force=True)
        return len(expired)

    def _get_session(self, session: str) -> _Session:
        state = self._sessions.get(session)
        if state is None:
            raise SandboxSessionNotFound(f"unknown sandbox session: {session}")
        if state.destroyed:
            raise SandboxSessionDestroyed(f"sandbox session is destroyed: {session}")
        return state

    async def _remove_session(self, session: str, *, force: bool) -> None:
        state = self._sessions.pop(session, None)
        if state is None:
            return
        state.destroyed = True

        def _remove() -> None:
            # Always attempt volume cleanup, even if container removal fails, so a
            # transient Docker error does not orphan the workspace volume.
            container_exc: SandboxError | None = None
            try:
                state.container.remove(force=force)
            except NotFound:
                pass
            except (APIError, DockerException) as exc:
                container_exc = SandboxError(f"failed to remove sandbox session {session}")
                container_exc.__cause__ = exc

            try:
                state.workspace_volume.remove(force=True)
            except NotFound:
                pass
            except (APIError, DockerException) as exc:
                if container_exc is None:
                    raise SandboxError(f"failed to remove sandbox workspace volume {session}") from exc

            if container_exc is not None:
                raise container_exc

        await asyncio.to_thread(_remove)

    def _safe_path(self, policy: SandboxPolicy, path: str) -> str:
        if "\x00" in path:
            raise SandboxPathError("paths may not contain NUL bytes")
        workdir = posixpath.normpath(policy.workdir)
        raw_path = path if path.startswith("/") else posixpath.join(workdir, path)
        normalized = posixpath.normpath(raw_path)
        if normalized == "." or normalized == "/":
            raise SandboxPathError("path must stay inside the sandbox workspace")
        if normalized != workdir and not normalized.startswith(f"{workdir}/"):
            raise SandboxPathError(f"path escapes sandbox workspace: {path}")
        return normalized

    def _extract_single_regular_file(self, archive_bytes: bytes) -> bytes:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            members = archive.getmembers()
            regular_files = [member for member in members if member.isfile()]
            if len(regular_files) != 1 or len(regular_files) != len(members):
                raise SandboxArchiveError("sandbox archive must contain exactly one regular file")
            member = regular_files[0]
            self._validate_archive_member(member)
            if member.size > self._file_limit_bytes:
                raise SandboxArchiveError(f"file exceeds {self._file_limit_bytes} byte sandbox read limit")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SandboxArchiveError("sandbox file could not be extracted")
            data = extracted.read(self._file_limit_bytes + 1)
            if len(data) > self._file_limit_bytes:
                raise SandboxArchiveError(f"file exceeds {self._file_limit_bytes} byte sandbox read limit")
            return data

    def _validate_archive_member(self, member: tarfile.TarInfo) -> None:
        name = member.name
        normalized = posixpath.normpath(name)
        if name.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise SandboxArchiveError(f"unsafe archive member path: {name}")
        if member.issym() or member.islnk():
            raise SandboxArchiveError(f"archive member may not be a link: {name}")

    def _decode_and_cap(self, data: bytes) -> tuple[str, bool]:
        truncated = len(data) > self._stdout_limit_bytes
        if truncated:
            data = data[: self._stdout_limit_bytes]
        return data.decode("utf-8", errors="replace"), truncated

    def _split_exec_output(self, output: Any) -> tuple[bytes, bytes]:
        if isinstance(output, tuple):
            stdout, stderr = output
            return stdout or b"", stderr or b""
        if isinstance(output, bytes):
            return output, b""
        return str(output).encode(), b""

    def _duration_ms(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)


def _docker_client_from_env() -> Any:
    if os.environ.get("DOCKER_HOST"):
        return docker.from_env()

    desktop_socket = Path.home() / ".docker" / "run" / "docker.sock"
    if desktop_socket.exists():
        return docker.DockerClient(base_url=f"unix://{desktop_socket}")

    return docker.from_env()
