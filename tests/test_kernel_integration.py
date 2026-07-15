"""Real-Docker kernel smoke tests. Run with ``uv run pytest -m integration -q``."""

from __future__ import annotations

import pytest

from henry.sandbox import DockerSandbox
from henry.sandbox.docker import SandboxError
from henry.sandbox_types import CellResult, SandboxPolicy

pytestmark = pytest.mark.integration


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
