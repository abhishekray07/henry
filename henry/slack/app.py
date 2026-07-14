from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from henry.contracts import SlackEvent
from henry.slack.context import build_slack_event


class SlackClient(Protocol):
    async def chat_postMessage(self, **kwargs: Any) -> Any: ...
    async def chat_update(self, **kwargs: Any) -> Any: ...


class EventDeduper(Protocol):
    async def reserve(self, event_id: str) -> bool: ...


Ack = Callable[[], None | Awaitable[None]]
SlackOrchestrator = Callable[[SlackEvent], Awaitable[list[str]]]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _run_and_update_slack(
    *,
    event: SlackEvent,
    client: SlackClient,
    placeholder_ts: str,
    orchestrator: SlackOrchestrator,
) -> None:
    try:
        chunks = await orchestrator(event)
    except Exception:  # noqa: BLE001 - Slack should receive a deterministic failure update
        chunks = ["I hit an internal error while handling that request."]

    first, *rest = chunks or [""]
    await client.chat_update(channel=event.channel_id, ts=placeholder_ts, text=first)
    for chunk in rest:
        await client.chat_postMessage(channel=event.channel_id, thread_ts=event.thread_ts, text=chunk)


async def _start_run(
    event: SlackEvent,
    *,
    client: SlackClient,
    orchestrator: SlackOrchestrator,
    deduper: EventDeduper | None,
) -> asyncio.Task[None] | None:
    if deduper is not None and not await deduper.reserve(event.event_id):
        return None

    placeholder = await client.chat_postMessage(
        channel=event.channel_id,
        thread_ts=event.thread_ts,
        text="Working on it...",
    )
    # slack_sdk returns an AsyncSlackResponse (mapping-like, not a dict subclass)
    placeholder_ts = str(placeholder.get("ts") or "")
    return asyncio.create_task(
        _run_and_update_slack(
            event=event,
            client=client,
            placeholder_ts=placeholder_ts,
            orchestrator=orchestrator,
        )
    )


async def dispatch_app_mention(
    *,
    body: dict[str, Any],
    client: SlackClient,
    ack: Ack,
    orchestrator: SlackOrchestrator,
    deduper: EventDeduper | None = None,
) -> asyncio.Task[None] | None:
    await _maybe_await(ack())
    event = build_slack_event(body)
    return await _start_run(event, client=client, orchestrator=orchestrator, deduper=deduper)




def create_slack_app(
    *,
    bot_token: str,
    orchestrator: SlackOrchestrator,
    deduper: EventDeduper | None = None,
) -> Any:
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=bot_token)

    @app.event("app_mention")
    async def handle_app_mention(body: dict[str, Any], client: SlackClient, ack: Ack) -> None:
        await dispatch_app_mention(
            body=body,
            client=client,
            ack=ack,
            orchestrator=orchestrator,
            deduper=deduper,
        )

    @app.event("message")
    async def handle_message(ack: Ack) -> None:
        # V1 gate: Henry acts on direct @mentions only — even inside threads
        # (docs/plans/2026-06-23-henry-design.md §Gate; ambient mode is phase 2).
        # Acking keeps Bolt from logging unhandled-request noise for ordinary
        # channel traffic.
        await _maybe_await(ack())

    return app
