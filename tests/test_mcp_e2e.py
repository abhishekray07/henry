from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

from henry.agent.runner import PydanticAgentRunner
from henry.contracts import AgentDeps, SlackEvent
from henry.db.models import Base, ChannelConfig
from henry.integrations.mcp import MCPIntegration, MCPServerDef
from henry.settings import Settings
from henry.testing import FakeMemory, FakeSandbox
from henry.types import ChannelContext
from henry.wiring import build_runtime

FIXTURES = Path(__file__).parent / "fixtures"


def _stdio_def(script: str, env: dict[str, str] | None = None, **overrides) -> MCPServerDef:
    return MCPServerDef.model_validate(
        {
            "command": sys.executable,
            "args": [str(FIXTURES / script)],
            "env": env or {},
            **overrides,
        }
    )


async def _deps() -> AgentDeps:
    return AgentDeps(
        ctx=ChannelContext(channel_id="C1", thread_ts="T1", run_id="R1"),
        memory=FakeMemory(),
        sandbox=FakeSandbox(),
        http=httpx.AsyncClient(),
        settings=Settings(default_model="test"),
    )


async def _run(integration: MCPIntegration, prompt: str, monkeypatch):
    monkeypatch.setattr("henry.agent.runner.memory_tools", lambda: [])
    monkeypatch.setattr("henry.agent.runner.sandbox_tools", lambda: [])
    deps = await _deps()
    try:
        runner = PydanticAgentRunner([integration], model="test")
        return await runner.run(deps, prompt)
    finally:
        await deps.http.aclose()


async def test_mcp_tool_flows_through_runner(monkeypatch) -> None:
    integration = MCPIntegration("echoer", _stdio_def("echo_mcp_server.py"))
    try:
        result = await _run(integration, "use the echo tool", monkeypatch)
        assert result.status == "ok"
        assert "ECHO:" in result.output
    finally:
        await integration.aclose()


async def test_allowlist_excludes_tools_end_to_end(monkeypatch) -> None:
    integration = MCPIntegration("echoer", _stdio_def("echo_mcp_server.py", tools=["echo_upper"]))
    try:
        result = await _run(integration, "use every tool you have", monkeypatch)
        assert result.status == "ok"
        assert "ECHO:" in result.output
        assert "HIDDEN-TOOL-RAN" not in result.output
    finally:
        await integration.aclose()


async def test_server_death_heals_on_next_run(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "flaky-marker"
    integration = MCPIntegration(
        "flaky",
        _stdio_def("flaky_mcp_server.py", env={"HENRY_TEST_FLAKY_MARKER": str(marker)}),
    )
    try:
        first = await _run(integration, "call the flaky tool", monkeypatch)
        assert first.status == "error"
        assert marker.exists()

        second = await _run(integration, "call the flaky tool", monkeypatch)
        assert second.status == "ok"
        assert "recovered:" in second.output
    finally:
        await integration.aclose()


async def _assert_pid_gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"mcp server pid {pid} still alive 10s after close")


async def test_aclose_terminates_the_subprocess(monkeypatch, tmp_path) -> None:
    pid_file = tmp_path / "server.pid"
    integration = MCPIntegration(
        "echoer",
        _stdio_def("echo_mcp_server.py", env={"HENRY_TEST_PID_FILE": str(pid_file)}),
    )
    pid: int | None = None
    try:
        result = await _run(integration, "use the echo tool", monkeypatch)
        assert result.status == "ok"
        pid = int(pid_file.read_text())
    finally:
        await integration.aclose()
        if pid is not None:
            await _assert_pid_gone(pid)


async def test_full_path_mcp_json_to_slack_reply_to_shutdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("henry.agent.runner.memory_tools", lambda: [])
    monkeypatch.setattr("henry.agent.runner.sandbox_tools", lambda: [])
    monkeypatch.setenv("HENRY_E2E_PID_FILE", str(tmp_path / "server.pid"))

    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echoer": {
                        "command": sys.executable,
                        "args": [str(FIXTURES / "echo_mcp_server.py")],
                        "env": {"HENRY_TEST_PID_FILE": "${HENRY_E2E_PID_FILE}"},
                        "tools": ["echo_upper"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        default_model="test",
        mcp_config_path=str(config),
    )
    runtime = build_runtime(settings, http=httpx.AsyncClient(), sandbox=FakeSandbox())
    pid: int | None = None
    try:
        async with runtime.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with runtime.sessionmaker() as session:
            session.add(ChannelConfig(channel_id="C-e2e", enabled_integrations=["echoer"]))
            await session.commit()

        chunks = await runtime.handle_event(
            SlackEvent(
                channel_id="C-e2e",
                thread_ts="T1",
                user="U1",
                text="use the echo tool",
                event_id="Ev-e2e",
                event_ts="1.0",
                is_mention=True,
            )
        )

        assert any("ECHO:" in chunk for chunk in chunks)
        pid = int((tmp_path / "server.pid").read_text())
    finally:
        await runtime.close()
        if pid is not None:
            await _assert_pid_gone(pid)
