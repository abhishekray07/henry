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
    async def conversations_replies(self, **kwargs: Any) -> Any: ...


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


async def dispatch_thread_message(
    *,
    body: dict[str, Any],
    client: SlackClient,
    ack: Ack,
    orchestrator: SlackOrchestrator,
    deduper: EventDeduper | None = None,
    bot_user_id_provider: Callable[[], Awaitable[str]],
) -> asyncio.Task[None] | None:
    """Answer a thread follow-up that didn't re-mention Henry.

    Henry replies only in threads it is already part of. Everything else stays
    silent: other bots and Henry's own posts (bot_id), edits/deletes (subtype),
    top-level channel chatter (no thread_ts), and messages that mention Henry —
    Slack delivers those as app_mention too, and that listener owns them.
    """
    await _maybe_await(ack())
    event = body.get("event")
    if not isinstance(event, dict):
        return None
    ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or "")
    if event.get("bot_id") or event.get("subtype"):
        return None
    if not thread_ts or thread_ts == ts:
        return None

    bot_user_id = await bot_user_id_provider()
    if not bot_user_id or f"<@{bot_user_id}>" in str(event.get("text") or ""):
        return None

    replies = await client.conversations_replies(
        channel=str(event.get("channel") or ""), ts=thread_ts, limit=50
    )
    thread_messages = replies.get("messages") or []
    if not any(str(message.get("user") or "") == bot_user_id for message in thread_messages):
        return None

    slack_event = build_slack_event(body, is_mention=False)
    return await _start_run(slack_event, client=client, orchestrator=orchestrator, deduper=deduper)


def create_slack_app(
    *,
    bot_token: str,
    orchestrator: SlackOrchestrator,
    deduper: EventDeduper | None = None,
) -> Any:
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=bot_token)
    bot_user_cache: dict[str, str] = {}

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
    async def handle_message(body: dict[str, Any], client: Any, ack: Ack) -> None:
        async def bot_user_id_provider() -> str:
            if "id" not in bot_user_cache:
                auth = await client.auth_test()
                bot_user_cache["id"] = str(auth.get("user_id") or "")
            return bot_user_cache["id"]

        await dispatch_thread_message(
            body=body,
            client=client,
            ack=ack,
            orchestrator=orchestrator,
            deduper=deduper,
            bot_user_id_provider=bot_user_id_provider,
        )

    return app
