from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from henry.agent.runner import PydanticAgentRunner
from henry.contracts import SlackEvent
from henry.db.models import Base, ChannelConfig
from henry.integrations.mcp import MCPIntegration, MCPServerDef
from henry.settings import Settings
from henry.testing import FakeSandbox
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


def _broken_def() -> MCPServerDef:
    return MCPServerDef.model_validate({"command": "/nonexistent/broken-mcp-binary"})


async def _run(integration: MCPIntegration, prompt: str, deps):
    runner = PydanticAgentRunner([integration], model="test")
    return await runner.run(deps, prompt)


async def test_mcp_tool_flows_through_runner(stub_agent_tools, agent_deps) -> None:
    integration = MCPIntegration("echoer", _stdio_def("echo_mcp_server.py"))
    try:
        result = await _run(integration, "use the echo tool", agent_deps)
        assert result.status == "ok"
        assert "ECHO:" in result.output
    finally:
        await integration.aclose()


async def test_allowlist_excludes_tools_end_to_end(stub_agent_tools, agent_deps) -> None:
    integration = MCPIntegration("echoer", _stdio_def("echo_mcp_server.py", tools=["echo_upper"]))
    try:
        result = await _run(integration, "use every tool you have", agent_deps)
        assert result.status == "ok"
        assert "ECHO:" in result.output
        assert "HIDDEN-TOOL-RAN" not in result.output
    finally:
        await integration.aclose()


async def test_server_death_heals_on_next_run(stub_agent_tools, agent_deps, tmp_path) -> None:
    marker = tmp_path / "flaky-marker"
    integration = MCPIntegration(
        "flaky",
        _stdio_def("flaky_mcp_server.py", env={"HENRY_TEST_FLAKY_MARKER": str(marker)}),
    )
    try:
        first = await _run(integration, "call the flaky tool", agent_deps)
        assert first.status == "error"
        assert marker.exists()

        second = await _run(integration, "call the flaky tool", agent_deps)
        assert second.status == "ok"
        assert "recovered:" in second.output
    finally:
        await integration.aclose()


async def test_one_unreachable_server_costs_only_its_own_tools(stub_agent_tools, agent_deps) -> None:
    """A dead MCP server must not take down runs that never needed it."""
    broken = MCPIntegration("broken", _broken_def())
    healthy = MCPIntegration("echoer", _stdio_def("echo_mcp_server.py"))
    try:
        runner = PydanticAgentRunner([broken, healthy], model="test")

        result = await runner.run(agent_deps, "use the echo tool")

        assert result.status == "ok"
        assert "ECHO:" in result.output
    finally:
        await healthy.aclose()
        await broken.aclose()


async def test_tool_error_feeds_back_so_the_model_can_self_correct(stub_agent_tools, agent_deps) -> None:
    """A 404-style tool error must reach the model as data, not kill the run.

    Mirrors the Help Scout failure: the model picks the wrong number out of a URL,
    the API 404s, and the model must get a chance to retry with the right id.
    """
    integration = MCPIntegration("lookup", _stdio_def("lookup_mcp_server.py"))
    seen_retry_content: list[str] = []

    def driver(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        retry = next((p for p in messages[-1].parts if isinstance(p, RetryPromptPart)), None)
        if retry is not None:
            seen_retry_content.append(str(retry.content))
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="lookup_get_conversation",
                        args={"conversation_id": "3384773514"},
                    )
                ]
            )
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="lookup_get_conversation",
                        args={"conversation_id": "5261"},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="found it: customer cannot log in")])

    try:
        runner = PydanticAgentRunner([integration], model=FunctionModel(driver))

        result = await runner.run(agent_deps, "help me debug conversation/3384773514/5261")

        assert result.status == "ok"
        assert "found it" in result.output
        assert len(seen_retry_content) == 1
        assert "404" in seen_retry_content[0]
    finally:
        await integration.aclose()


async def test_aclose_never_raises_for_a_server_that_never_connected() -> None:
    integration = MCPIntegration("broken", _broken_def())
    toolset = integration.toolset()
    try:
        async with toolset:
            raise AssertionError("connecting to a nonexistent binary should fail")
    except AssertionError:
        raise
    except Exception:
        pass

    await integration.aclose()  # must not raise, even though the server never came up

    assert integration._toolset is None


async def _assert_pid_gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"mcp server pid {pid} still alive 10s after close")


async def test_aclose_terminates_the_subprocess(stub_agent_tools, agent_deps, tmp_path) -> None:
    pid_file = tmp_path / "server.pid"
    integration = MCPIntegration(
        "echoer",
        _stdio_def("echo_mcp_server.py", env={"HENRY_TEST_PID_FILE": str(pid_file)}),
    )
    pid: int | None = None
    try:
        result = await _run(integration, "use the echo tool", agent_deps)
        assert result.status == "ok"
        pid = int(pid_file.read_text())
    finally:
        await integration.aclose()
        if pid is not None:
            await _assert_pid_gone(pid)


async def test_full_path_mcp_json_to_slack_reply_to_shutdown(
    stub_agent_tools, monkeypatch, tmp_path
) -> None:
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
