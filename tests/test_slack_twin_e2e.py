"""Today's production failures, replayed forever: real henry against the Slack twin.

Full production path — AsyncSocketModeHandler -> twin websocket -> Bolt ->
runtime -> real MCP subprocesses. Only the model is scripted (FunctionModel)
so outcomes are deterministic. Each scenario here was a live incident or a
contract the twin was built to enforce.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import slack_sdk.web.async_client as wc
from pydantic_ai.messages import ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

from henry.db.models import Base, ChannelConfig
from henry.settings import Settings
from henry.slack.app import create_slack_app
from henry.testing import FakeSandbox
from henry.wiring import build_runtime

from slack_twin import BOT, SlackTwin

FIXTURES = Path(__file__).parent / "fixtures"
CH = "C-twin"


def _part(messages, cls):
    return next((p for p in messages[-1].parts if isinstance(p, cls)), None)


def driver_echo(messages, info):
    ret = _part(messages, ToolReturnPart)
    if ret is not None:
        return ModelResponse(parts=[TextPart(content=f"RESULT {ret.content}")])
    return ModelResponse(parts=[ToolCallPart(tool_name="echoer_echo_upper", args={"text": "hi"})])


def driver_ticket(messages, info):
    """The Help Scout incident: wrong id -> 404 -> self-correct to the real id."""
    ret = _part(messages, ToolReturnPart)
    if ret is not None:
        return ModelResponse(parts=[TextPart(content=f"TICKET {ret.content}")])
    if _part(messages, RetryPromptPart) is not None:
        return ModelResponse(
            parts=[ToolCallPart(tool_name="lookup_get_conversation", args={"conversation_id": "3384773514"})]
        )
    return ModelResponse(
        parts=[ToolCallPart(tool_name="lookup_get_conversation", args={"conversation_id": "5261"})]
    )


def driver_github(messages, info):
    """The GitHub 401 incident: use code search if offered, recover if it fails."""
    names = [t.name for t in info.function_tools]
    if _part(messages, RetryPromptPart) is not None or "search_code" not in names:
        return ModelResponse(parts=[TextPart(content="DONE-WITHOUT-GITHUB")])
    if _part(messages, ToolReturnPart) is not None:
        return ModelResponse(parts=[TextPart(content="GITHUB-WORKED")])
    return ModelResponse(parts=[ToolCallPart(tool_name="search_code", args={"query": "Verbindung"})])


def driver_plain(messages, info):
    return ModelResponse(parts=[TextPart(content="ANSWERED")])


def _gh401_http() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(401, json={"message": "Requires authentication"})
        return httpx.Response(500, json={"error": "unexpected host"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _wait_final(twin: SlackTwin, root: str, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for m in twin.threads.get((CH, root), []):
            if m.get("user") == BOT and m["text"] and m["text"] != "Working on it...":
                return m["text"]
        await asyncio.sleep(0.05)
    return "(no final reply within timeout)"


@asynccontextmanager
async def henry_on_twin(tmp_path, monkeypatch, *, servers, enabled, model, http=None):
    twin = SlackTwin()
    base = await twin.start()

    original_init = wc.AsyncWebClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["base_url"] = base
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(wc.AsyncWebClient, "__init__", patched_init)
    monkeypatch.setattr("henry.agent.runner.build_model", lambda name, settings, http_client: model)

    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/twin.sqlite",
        default_model="test",
        mcp_config_path=str(config),
    )
    runtime = build_runtime(settings, http=http or httpx.AsyncClient(), sandbox=FakeSandbox())
    async with runtime.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if enabled is not None:
        async with runtime.sessionmaker() as session:
            session.add(ChannelConfig(channel_id=CH, enabled_integrations=enabled))
            await session.commit()

    app = create_slack_app(bot_token="xoxb-fake", orchestrator=runtime.handle_event, deduper=runtime.deduper)
    handler = AsyncSocketModeHandler(app, "xapp-fake")
    try:
        await handler.connect_async()
        for _ in range(100):
            if twin.ws is not None:
                break
            await asyncio.sleep(0.05)
        assert twin.ws is not None, "henry never connected to the twin socket"
        yield twin
    finally:
        try:
            await handler.disconnect_async()
        except Exception:
            pass
        await runtime.close()
        await twin.stop()


def _echo_servers() -> dict:
    return {"echoer": {"command": sys.executable, "args": [str(FIXTURES / "echo_mcp_server.py")]}}


async def test_one_broken_mcp_server_costs_only_its_tools(stub_agent_tools, tmp_path, monkeypatch) -> None:
    servers = {**_echo_servers(), "broken": {"command": "/nonexistent/broken-mcp-binary"}}
    async with henry_on_twin(
        tmp_path,
        monkeypatch,
        servers=servers,
        enabled=["echoer", "broken"],
        model=FunctionModel(driver_echo),
    ) as twin:
        root = await twin.mention(CH, "U1", "use the echo tool")
        assert await _wait_final(twin, root) == "RESULT ECHO:HI"


async def test_tool_error_feeds_back_for_self_correction(stub_agent_tools, tmp_path, monkeypatch) -> None:
    servers = {"lookup": {"command": sys.executable, "args": [str(FIXTURES / "lookup_mcp_server.py")]}}
    async with henry_on_twin(
        tmp_path,
        monkeypatch,
        servers=servers,
        enabled=["lookup"],
        model=FunctionModel(driver_ticket),
    ) as twin:
        root = await twin.mention(CH, "U1", "help me debug conversation/3384773514/5261")
        assert await _wait_final(twin, root) == "TICKET conversation 3384773514: customer cannot log in"


async def test_wildcard_skips_unconfigured_github(stub_agent_tools, tmp_path, monkeypatch) -> None:
    async with henry_on_twin(
        tmp_path,
        monkeypatch,
        servers={},
        enabled=None,  # no channel row -> wildcard
        model=FunctionModel(driver_github),
        http=_gh401_http(),
    ) as twin:
        root = await twin.mention(CH, "U1", "debug the aquantic ticket")
        assert await _wait_final(twin, root) == "DONE-WITHOUT-GITHUB"


async def test_thread_follow_up_gets_answered_without_a_mention(
    stub_agent_tools, tmp_path, monkeypatch
) -> None:
    async with henry_on_twin(
        tmp_path, monkeypatch, servers={}, enabled=[], model=FunctionModel(driver_plain)
    ) as twin:
        root = await twin.mention(CH, "U1", "summarize this thread")
        assert await _wait_final(twin, root) == "ANSWERED"

        before = twin.bot_msgs(CH, root)
        await twin.reply_in_thread(CH, root, "U1", "yes, please go ahead")
        assert await twin.wait_for_bot_reply(CH, root, since=before, timeout=15.0)


async def test_thread_follow_up_stays_silent_for_other_bots_and_edits(
    stub_agent_tools, tmp_path, monkeypatch
) -> None:
    async with henry_on_twin(
        tmp_path, monkeypatch, servers={}, enabled=[], model=FunctionModel(driver_plain)
    ) as twin:
        root = await twin.mention(CH, "U1", "summarize this thread")
        assert await _wait_final(twin, root) == "ANSWERED"
        before = twin.bot_msgs(CH, root)

        await twin.reply_in_thread(CH, root, "U9", "beep", bot_id="B9")
        await twin.reply_in_thread(CH, root, "U1", "edited text", subtype="message_changed")
        # A mention inside the thread is delivered as app_mention too; the
        # message listener must not double-handle it.
        await twin.reply_in_thread(CH, root, "U1", f"<@{BOT}> more please")

        await asyncio.sleep(1.5)
        assert twin.bot_msgs(CH, root) == before


async def test_thread_follow_up_ignores_threads_henry_never_joined(
    stub_agent_tools, tmp_path, monkeypatch
) -> None:
    async with henry_on_twin(
        tmp_path, monkeypatch, servers={}, enabled=[], model=FunctionModel(driver_plain)
    ) as twin:
        await twin.reply_in_thread(CH, "999.000000", "U1", "hello?")
        await asyncio.sleep(1.5)
        assert twin.bot_msgs(CH, "999.000000") == 0
