from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from henry.config.registry import ResolvedConfig
from henry.contracts import AgentDeps, RunResult, RunUsage, SlackEvent
from henry.orchestrator.runner import AuditRecord, handle_request
from henry.testing.fakes import FakeAgentRunner, FakeMemory, FakeSandbox
from henry.types import ChannelContext, ConversationTranscript, ThreadMessage


@dataclass
class RecordingAuditSink:
    records: list[AuditRecord] = field(default_factory=list)

    async def __call__(self, record: AuditRecord) -> None:
        self.records.append(record)


def _event() -> SlackEvent:
    return SlackEvent(
        channel_id="C123",
        thread_ts="T123",
        user="U123",
        text="ship it",
        event_id="Ev123",
        event_ts="T123",
        is_mention=True,
    )


async def _config_loader(event: SlackEvent) -> ResolvedConfig:
    assert event.event_id == "Ev123"
    return ResolvedConfig(
        model="test:model",
        enabled_integrations=[],
        system_prompt="base",
        ambient_on=False,
        budget_caps={},
    )


async def _transcript_fetcher(event: SlackEvent) -> ConversationTranscript:
    return ConversationTranscript(
        channel_id=event.channel_id,
        thread_ts=event.thread_ts,
        messages=(ThreadMessage(role="user", text=event.text, user=event.user, ts=event.event_ts),),
    )


@pytest.mark.asyncio
async def test_handle_request_runs_agent_refreshes_memory_and_writes_audit() -> None:
    memory = FakeMemory()
    sandbox = FakeSandbox()
    audit = RecordingAuditSink()
    runner = FakeAgentRunner(
        result=RunResult(
            output="done",
            usage=RunUsage(input_tokens=4, output_tokens=2, requests=1, tool_calls=1, cost_usd=None),
        )
    )
    cleanup_calls: list[AgentDeps] = []

    async def deps_factory(ctx: ChannelContext, config: ResolvedConfig) -> AgentDeps:
        assert ctx.channel_id == "C123"
        assert ctx.thread_ts == "T123"
        assert ctx.actor_user_id == "U123"
        assert ctx.run_id
        assert config.model == "test:model"
        return AgentDeps(
            ctx=ctx,
            memory=memory,
            sandbox=sandbox,
            http=httpx.AsyncClient(),
            settings=object(),
        )

    async def cleanup(deps: AgentDeps) -> None:
        cleanup_calls.append(deps)
        await deps.http.aclose()

    chunks = await handle_request(
        _event(),
        runner=runner,
        memory=memory,
        deps_factory=deps_factory,
        config_loader=_config_loader,
        transcript_fetcher=_transcript_fetcher,
        audit_sink=audit,
        cleanup=cleanup,
    )

    assert chunks == ["done"]
    assert runner.calls[0][1] == "ship it"
    assert memory.refreshed[0][0] == "C123"
    assert len(audit.records) == 1
    assert audit.records[0].status == "ok"
    assert audit.records[0].input_tokens == 4
    assert cleanup_calls


@pytest.mark.asyncio
async def test_handle_request_audits_runner_exception_and_returns_error_chunk() -> None:
    class ExplodingRunner:
        async def run(self, deps: AgentDeps, user_prompt: str, transcript: ConversationTranscript | None = None) -> Any:
            raise RuntimeError("model unavailable")

    memory = FakeMemory()
    audit = RecordingAuditSink()

    async def deps_factory(ctx: ChannelContext, config: ResolvedConfig) -> AgentDeps:
        return AgentDeps(
            ctx=ctx,
            memory=memory,
            sandbox=FakeSandbox(),
            http=httpx.AsyncClient(),
            settings=object(),
        )

    async def cleanup(deps: AgentDeps) -> None:
        await deps.http.aclose()

    chunks = await handle_request(
        _event(),
        runner=ExplodingRunner(),
        memory=memory,
        deps_factory=deps_factory,
        config_loader=_config_loader,
        transcript_fetcher=_transcript_fetcher,
        audit_sink=audit,
        cleanup=cleanup,
    )

    assert chunks == ["I hit an internal error while handling that request."]
    assert audit.records[0].status == "error"
    assert "model unavailable" in (audit.records[0].error or "")
    assert memory.refreshed == []
