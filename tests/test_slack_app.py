from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from henry.contracts import SlackEvent
from henry.slack.app import dispatch_app_mention


@dataclass
class FakeClient:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("chat_postMessage", kwargs))
        return {"ts": "placeholder-ts"}

    async def chat_update(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append(("chat_update", kwargs))
        return {"ok": True}


@dataclass
class FakeDeduper:
    reserved: bool = True
    calls: list[str] = field(default_factory=list)

    async def reserve(self, event_id: str) -> bool:
        self.calls.append(event_id)
        return self.reserved


@pytest.mark.asyncio
async def test_dispatch_app_mention_acks_before_background_orchestrator_finishes() -> None:
    client = FakeClient()
    deduper = FakeDeduper()
    acked = asyncio.Event()
    orchestrator_started = asyncio.Event()
    finish_orchestrator = asyncio.Event()
    seen_events: list[SlackEvent] = []

    async def ack() -> None:
        acked.set()

    async def orchestrator(event: SlackEvent) -> list[str]:
        seen_events.append(event)
        orchestrator_started.set()
        await finish_orchestrator.wait()
        return ["first", "second"]

    task = await dispatch_app_mention(
        body={
            "event_id": "Ev123",
            "event": {
                "channel": "C123",
                "user": "U123",
                "text": "<@B123> hello",
                "ts": "1719170000.000100",
            },
        },
        client=client,
        ack=ack,
        orchestrator=orchestrator,
        deduper=deduper,
    )

    assert acked.is_set()
    assert task is not None
    await asyncio.wait_for(orchestrator_started.wait(), timeout=1)
    assert seen_events[0].event_id == "Ev123"
    assert client.calls[0][0] == "chat_postMessage"
    assert client.calls[0][1]["thread_ts"] == "1719170000.000100"

    finish_orchestrator.set()
    await asyncio.wait_for(task, timeout=1)

    assert ("chat_update", {"channel": "C123", "ts": "placeholder-ts", "text": "first"}) in client.calls
    assert ("chat_postMessage", {"channel": "C123", "thread_ts": "1719170000.000100", "text": "second"}) in client.calls


@pytest.mark.asyncio
async def test_dispatch_app_mention_dedups_before_posting_placeholder() -> None:
    client = FakeClient()
    deduper = FakeDeduper(reserved=False)
    acked = False

    async def ack() -> None:
        nonlocal acked
        acked = True

    async def orchestrator(event: SlackEvent) -> list[str]:
        raise AssertionError("duplicate events should not run")

    task = await dispatch_app_mention(
        body={
            "event_id": "Ev123",
            "event": {"channel": "C123", "user": "U123", "text": "hello", "ts": "1"},
        },
        client=client,
        ack=ack,
        orchestrator=orchestrator,
        deduper=deduper,
    )

    assert acked
    assert task is None
    assert client.calls == []
