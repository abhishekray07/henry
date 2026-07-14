"""Slack-ingress e2e: verbatim event_callback payloads through the real Bolt app.

Covers the seam no other test spans: Bolt event routing -> build_slack_event ->
dedupe -> runtime -> real MCP subprocess -> reply posted back to Slack. The Slack
Web API is served by a local HTTP fake (the real slack_sdk client talks to it);
only the Socket Mode websocket transport itself is bypassed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from aiohttp import web
from slack_bolt.request.async_request import AsyncBoltRequest

from henry.db.models import Base, ChannelConfig
from henry.settings import Settings
from henry.slack.app import create_slack_app
from henry.testing import FakeSandbox
from henry.wiring import build_runtime

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSlackServer:
    """Local stand-in for slack.com's Web API; records what Henry shows the user."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self._runner: web.AppRunner | None = None
        self.base_url = ""

    async def __aenter__(self) -> FakeSlackServer:
        app = web.Application()
        app.router.add_post("/auth.test", self._auth_test)
        app.router.add_post("/chat.postMessage", self._post_message)
        app.router.add_post("/chat.update", self._update)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}/"
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @staticmethod
    async def _payload(request: web.Request) -> dict[str, Any]:
        if request.content_type == "application/json":
            return dict(await request.json())
        return dict(await request.post())

    async def _auth_test(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "url": "https://test.slack.com/",
                "team_id": "T1",
                "user_id": "U-bot",
                "bot_id": "B1",
                "user": "henry",
            }
        )

    async def _post_message(self, request: web.Request) -> web.Response:
        data = await self._payload(request)
        self.posted.append(data)
        return web.json_response(
            {"ok": True, "channel": data.get("channel"), "ts": f"placeholder-{len(self.posted)}"}
        )

    async def _update(self, request: web.Request) -> web.Response:
        data = await self._payload(request)
        self.updated.append(data)
        return web.json_response({"ok": True, "ts": data.get("ts")})


def _mention_envelope(text: str, event_id: str = "Ev-socket-e2e") -> dict[str, Any]:
    """A verbatim Events API body, as Slack delivers it over Socket Mode."""
    return {
        "token": "verification-token",
        "team_id": "T1",
        "api_app_id": "A1",
        "type": "event_callback",
        "event_id": event_id,
        "event_time": 1700000000,
        "event": {
            "type": "app_mention",
            "user": "U-human",
            "text": text,
            "ts": "1700000000.000100",
            "channel": "C-socket-e2e",
            "event_ts": "1700000000.000100",
        },
    }


async def _make_runtime(tmp_path, mcp_servers: dict[str, Any]):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": mcp_servers}), encoding="utf-8")
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        default_model="test",
        mcp_config_path=str(config),
    )
    runtime = build_runtime(settings, http=httpx.AsyncClient(), sandbox=FakeSandbox())
    async with runtime.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Scope the channel to the MCP servers under test; the test model calls every
    # tool it sees, so wildcard enablement would drag builtin integrations in too.
    async with runtime.sessionmaker() as session:
        session.add(
            ChannelConfig(channel_id="C-socket-e2e", enabled_integrations=list(mcp_servers))
        )
        await session.commit()
    return runtime


def _wire_app(runtime, slack: FakeSlackServer):
    app = create_slack_app(
        bot_token="xoxb-test",
        orchestrator=runtime.handle_event,
        deduper=runtime.deduper,
    )
    # Bolt builds a fresh AsyncWebClient per request, inheriting base_url from
    # app.client — pointing it at the local fake routes every Web API call there.
    app.client.base_url = slack.base_url
    return app


async def _wait_for_update(slack: FakeSlackServer, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if slack.updated:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Slack placeholder was never updated with a result")


async def test_raw_slack_mention_flows_to_mcp_tool_reply(stub_agent_tools, tmp_path) -> None:
    runtime = await _make_runtime(
        tmp_path,
        {
            "echoer": {
                "command": sys.executable,
                "args": [str(FIXTURES / "echo_mcp_server.py")],
                "tools": ["echo_upper"],
            }
        },
    )
    try:
        async with FakeSlackServer() as slack:
            app = _wire_app(runtime, slack)

            response = await app.async_dispatch(
                AsyncBoltRequest(
                    body=_mention_envelope("<@U-bot> use the echo tool"), mode="socket_mode"
                )
            )

            assert response.status == 200
            await _wait_for_update(slack)

            assert slack.posted[0]["channel"] == "C-socket-e2e"
            assert slack.posted[0]["text"] == "Working on it..."
            assert "ECHO:" in slack.updated[0]["text"]

            # Slack redelivers events; a replayed envelope must be deduped, not re-run.
            await app.async_dispatch(
                AsyncBoltRequest(
                    body=_mention_envelope("<@U-bot> use the echo tool"), mode="socket_mode"
                )
            )
            await asyncio.sleep(0.2)
            assert len(slack.posted) == 1
    finally:
        await runtime.close()


async def test_raw_mention_with_failing_tool_still_updates_slack(stub_agent_tools, tmp_path) -> None:
    """A tool that errors must end in a deterministic Slack update, never a hung placeholder."""
    runtime = await _make_runtime(
        tmp_path,
        {
            "lookup": {
                "command": sys.executable,
                "args": [str(FIXTURES / "lookup_mcp_server.py")],
            }
        },
    )
    try:
        async with FakeSlackServer() as slack:
            app = _wire_app(runtime, slack)

            await app.async_dispatch(
                AsyncBoltRequest(
                    body=_mention_envelope("<@U-bot> look up conversation 5261", event_id="Ev-fail"),
                    mode="socket_mode",
                )
            )
            await _wait_for_update(slack)

            text = slack.updated[0]["text"]
            assert text
            assert text != "Working on it..."
    finally:
        await runtime.close()
