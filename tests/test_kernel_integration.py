"""Real-Docker kernel smoke tests. Run with ``uv run pytest -m integration -q``."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from henry.contracts import AgentDeps
from henry.sandbox import DockerSandbox
from henry.sandbox.docker import SandboxError
from henry.sandbox.tools import clear_sandbox_session, sandbox_tools
from henry.sandbox_types import CellResult, SandboxPolicy
from henry.testing import FakeMemory
from henry.types import ChannelContext

pytestmark = pytest.mark.integration


def _tool(name: str):
    return {tool.__name__: tool for tool in sandbox_tools()}[name]


def _tool_deps(sandbox: DockerSandbox, run_id: str) -> AgentDeps:
    """Wire the real tools to a real sandbox.

    Every other run_python test injects FakeSandbox, so the tool layer's
    behaviour against a real kernel is only covered here.
    """
    return AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1", run_id=run_id),
        memory=FakeMemory(),
        sandbox=sandbox,
        http=httpx.AsyncClient(),
        settings=SimpleNamespace(sandbox_image="henry-sandbox:base", github_token=""),
    )


def _live_containers(sandbox: DockerSandbox) -> int:
    return len(sandbox._client.containers.list(all=True, filters={"label": "henry.sandbox=true"}))


def _docker_or_skip() -> None:
    pytest.importorskip("docker")
    try:
        sandbox = DockerSandbox()
        sandbox._client.ping()
    except Exception:
        pytest.skip("no Docker daemon")
    try:
        sandbox._client.images.get("henry-sandbox:base")
    except Exception:
        pytest.skip("henry-sandbox:base not built")


def _text(result: CellResult) -> str:
    return "".join(output.text or "" for output in result.outputs if output.output_type == "stream")


def _result_value(result: CellResult) -> str | None:
    for output in result.outputs:
        if output.output_type == "execute_result":
            return str((output.data or {}).get("text/plain", "")).strip()
    return None


@pytest.mark.asyncio
async def test_state_and_order_persist_across_cells() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    session = await sandbox.start(SandboxPolicy())
    try:
        assigned = await sandbox.exec_cell(session, "x = 40")
        calculated = await sandbox.exec_cell(session, "x + 2")
        ordered = await sandbox.exec_cell(
            session,
            "from IPython.display import display\nprint('before')\ndisplay({'value': 42})\nprint('after')",
        )
        error = await sandbox.exec_cell(session, "raise ValueError('boom')")

        assert assigned.status == "ok"
        assert _result_value(calculated) == "42"
        assert calculated.execution_count > assigned.execution_count
        assert [output.output_type for output in ordered.outputs] == [
            "stream",
            "display_data",
            "stream",
        ]
        assert _text(ordered) == "before\nafter\n"
        assert error.status == "error"
        assert error.outputs[-1].ename == "ValueError"
    finally:
        await sandbox.destroy(session)


@pytest.mark.asyncio
async def test_failing_shell_escape_is_reported_as_an_error() -> None:
    """`run_python` advertises `!cmd`, so a failing one must not look successful.

    ipykernel's ZMQInteractiveShell overrides system_piped without honouring
    IPython's system_raise_on_error trait, so setting that trait does nothing
    here — the kernel startup wraps `shell.system` instead.
    """
    _docker_or_skip()
    sandbox = DockerSandbox()
    session = await sandbox.start(SandboxPolicy())
    try:
        failed = await sandbox.exec_cell(session, "!false")
        missing = await sandbox.exec_cell(session, "!test -f /definitely/missing")
        worked = await sandbox.exec_cell(session, "!echo hello")

        assert failed.status == "error"
        assert missing.status == "error"
        # A succeeding command must still behave exactly as before.
        assert worked.status == "ok"
        assert "hello" in _text(worked)
    finally:
        await sandbox.destroy(session)


@pytest.mark.asyncio
async def test_kernel_startup_does_not_leak_helpers_into_the_user_namespace() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    session = await sandbox.start(SandboxPolicy())
    try:
        result = await sandbox.exec_cell(session, "[n for n in dir() if n.startswith('_henry')]")
        assert _result_value(result) == "[]"
    finally:
        await sandbox.destroy(session)


@pytest.mark.asyncio
async def test_egress_is_blocked() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    session = await sandbox.start(SandboxPolicy())
    try:
        result = await sandbox.exec_cell(
            session,
            "import socket\n"
            "try:\n"
            " socket.create_connection(('api.anthropic.com', 443), 3)\n"
            " print('REACHED')\n"
            "except Exception:\n"
            " print('BLOCKED')",
        )
        assert "BLOCKED" in _text(result)
        assert "REACHED" not in _text(result)
    finally:
        await sandbox.destroy(session)


@pytest.mark.asyncio
async def test_input_does_not_hang() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    session = await sandbox.start(SandboxPolicy())
    try:
        result = await sandbox.exec_cell(session, "input('prompt: ')", timeout_s=10)
        assert result.status == "error"
        assert result.timed_out is False
    finally:
        await sandbox.destroy(session)


@pytest.mark.asyncio
async def test_unicode_output_is_byte_bounded() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    session = await sandbox.start(SandboxPolicy())
    try:
        result = await sandbox.exec_cell(session, "print('é' * 200_000)")
        assert result.truncated is True
        assert len(_text(result).encode("utf-8")) <= 256 * 1024
    finally:
        await sandbox.destroy(session)


@pytest.mark.asyncio
async def test_tool_spoofing_a_transport_error_does_not_leak_a_container() -> None:
    """Sandboxed code must not be able to talk the host into orphaning its container.

    A cell can define and raise its own ``KernelTransportError``. When the host
    inferred teardown from ``ename``, this evicted the session from the tool
    cache while the container kept running — and nothing else destroys an
    untracked session, so it leaked for good.
    """
    _docker_or_skip()
    sandbox = DockerSandbox()
    deps = _tool_deps(sandbox, "integration-spoof")
    ctx = SimpleNamespace(deps=deps)
    before = _live_containers(sandbox)
    try:
        await _tool("run_python")(
            ctx, "class KernelTransportError(Exception): pass\nraise KernelTransportError('gotcha')"
        )
        await _tool("run_python")(ctx, "1 + 1")
        # A second container here means the spoof evicted the healthy session.
        assert _live_containers(sandbox) - before == 1
    finally:
        await clear_sandbox_session(deps)
    assert _live_containers(sandbox) - before == 0


@pytest.mark.asyncio
async def test_tool_keeps_kernel_state_across_calls_and_shares_the_workspace() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    deps = _tool_deps(sandbox, "integration-stateful")
    ctx = SimpleNamespace(deps=deps)
    try:
        await _tool("write_file")(ctx, "data.txt", "hello from host")
        await _tool("run_python")(ctx, "import math\nvals = [1, 2, 3]")

        computed = await _tool("run_python")(ctx, "sum(vals) * math.factorial(3)")
        from_disk = await _tool("run_python")(ctx, "print(open('/workspace/data.txt').read())")

        assert "36" in computed
        assert "hello from host" in from_disk
    finally:
        await clear_sandbox_session(deps)


@pytest.mark.asyncio
async def test_tool_timeout_keeps_partial_output_and_recovers_on_a_fresh_kernel() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    deps = _tool_deps(sandbox, "integration-timeout")
    ctx = SimpleNamespace(deps=deps)
    try:
        reported = await _tool("run_python")(
            ctx, "print('partial', flush=True)\nimport time\ntime.sleep(30)", 1
        )

        # Output from before the deadline is the debugging signal for a stalled cell.
        assert "partial" in reported
        assert "timed out" in reported
        assert "kernel was restarted" in reported

        assert "42" in await _tool("run_python")(ctx, "7 * 6")
    finally:
        await clear_sandbox_session(deps)


@pytest.mark.asyncio
async def test_tool_rejects_a_non_positive_timeout_without_killing_the_session() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    deps = _tool_deps(sandbox, "integration-badtimeout")
    ctx = SimpleNamespace(deps=deps)
    try:
        await _tool("run_python")(ctx, "keep = 'alive'")

        with pytest.raises(ValueError, match="must be positive"):
            await _tool("run_python")(ctx, "1", 0)

        assert "alive" in await _tool("run_python")(ctx, "keep")
    finally:
        await clear_sandbox_session(deps)


@pytest.mark.asyncio
async def test_timeout_destroys_session_then_fresh_start_works() -> None:
    _docker_or_skip()
    sandbox = DockerSandbox()
    session = await sandbox.start(SandboxPolicy())
    timed_out = await sandbox.exec_cell(session, "import time; time.sleep(30)", timeout_s=1)
    assert timed_out.timed_out is True
    with pytest.raises(SandboxError):
        await sandbox.exec_cell(session, "1 + 1")

    fresh = await sandbox.start(SandboxPolicy())
    try:
        assert _result_value(await sandbox.exec_cell(fresh, "1 + 1")) == "2"
    finally:
        await sandbox.destroy(fresh)
