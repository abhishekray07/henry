from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError

from henry.config.registry import ResolvedConfig
from henry.contracts import AgentDeps, AgentRunner, RunResult, RunUsage, SlackEvent
from henry.db.models import AuditLog, ProcessedEvent
from henry.interfaces import Memory
from henry.orchestrator.locks import ThreadLocks
from henry.slack.context import split_for_slack
from henry.types import ChannelContext, ConversationTranscript, ThreadMessage


@dataclass(frozen=True)
class AuditRecord:
    run_id: str
    channel_id: str
    thread_ts: str
    actor: str
    action: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int
    status: str
    error: str | None = None
    integration: str = ""


DepsFactory = Callable[[ChannelContext, ResolvedConfig], AgentDeps | Awaitable[AgentDeps]]
ConfigLoader = Callable[[SlackEvent], ResolvedConfig | Awaitable[ResolvedConfig]]
TranscriptFetcher = Callable[[SlackEvent], ConversationTranscript | Awaitable[ConversationTranscript]]
AuditSink = Callable[[AuditRecord], None | Awaitable[None]]
CleanupHook = Callable[[AgentDeps], None | Awaitable[None]]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _default_config() -> ResolvedConfig:
    from henry.config.registry import DEFAULTS_PATH

    return ResolvedConfig.model_validate(json.loads(DEFAULTS_PATH.read_text(encoding="utf-8")))


async def _default_config_loader(event: SlackEvent) -> ResolvedConfig:
    return _default_config()


async def _default_transcript_fetcher(event: SlackEvent) -> ConversationTranscript:
    return ConversationTranscript(
        channel_id=event.channel_id,
        thread_ts=event.thread_ts,
        messages=(ThreadMessage(role="user", text=event.text, user=event.user, ts=event.event_ts),),
    )


async def _noop_audit_sink(record: AuditRecord) -> None:
    return None


def _error_result(exc: BaseException) -> RunResult:
    return RunResult(
        output="I hit an internal error while handling that request.",
        usage=RunUsage(),
        status="error",
        error=str(exc),
    )


def _display_output(result: RunResult) -> str:
    if result.output:
        return result.output
    if result.status == "budget_exceeded":
        return "I stopped because this request hit its budget limit."
    if result.status == "error":
        return "I hit an internal error while handling that request."
    return ""


async def handle_request(
    event: SlackEvent,
    *,
    runner: AgentRunner,
    memory: Memory,
    deps_factory: DepsFactory,
    locks: ThreadLocks | None = None,
    config_loader: ConfigLoader | None = None,
    transcript_fetcher: TranscriptFetcher | None = None,
    audit_sink: AuditSink | None = None,
    cleanup: CleanupHook | None = None,
) -> list[str]:
    locks = locks or ThreadLocks()
    config_loader = config_loader or _default_config_loader
    transcript_fetcher = transcript_fetcher or _default_transcript_fetcher
    audit_sink = audit_sink or _noop_audit_sink

    async with locks.acquire(event.channel_id, event.thread_ts):
        run_id = uuid.uuid4().hex
        ctx = ChannelContext(
            channel_id=event.channel_id,
            thread_ts=event.thread_ts,
            actor_user_id=event.user or None,
            run_id=run_id,
        )
        started = time.perf_counter()
        config: ResolvedConfig | None = None
        deps: AgentDeps | None = None
        result: RunResult

        try:
            # Config and transcript loading run inside the audited block: a bad
            # channel_config row must produce an audit record, not a silent abort.
            config = await _maybe_await(config_loader(event))
            transcript = await _maybe_await(transcript_fetcher(event))
            deps = await _maybe_await(deps_factory(ctx, config))
            result = await runner.run(deps, event.text, transcript)
            if result.status == "ok":
                await memory.refresh_snapshot(event.channel_id, transcript)
        except Exception as exc:  # noqa: BLE001 - convert all exits into audited run results
            result = _error_result(exc)
        finally:
            if cleanup is not None and deps is not None:
                await _maybe_await(cleanup(deps))

        latency_ms = int((time.perf_counter() - started) * 1000)
        await _maybe_await(
            audit_sink(
                AuditRecord(
                    run_id=run_id,
                    channel_id=event.channel_id,
                    thread_ts=event.thread_ts,
                    actor=event.user,
                    action="agent.run",
                    model=config.model if config is not None else "",
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cost_usd=result.usage.cost_usd,
                    latency_ms=latency_ms,
                    status=result.status,
                    error=result.error,
                )
            )
        )
        return split_for_slack(_display_output(result))


def make_db_audit_sink(sessionmaker: Callable[[], Any]) -> AuditSink:
    async def sink(record: AuditRecord) -> None:
        cost = Decimal(str(record.cost_usd)) if record.cost_usd is not None else None
        async with sessionmaker() as session:
            session.add(
                AuditLog(
                    run_id=record.run_id,
                    channel_id=record.channel_id,
                    thread_ts=record.thread_ts,
                    actor=record.actor,
                    action=record.action,
                    integration=record.integration,
                    model=record.model,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_usd=cost,
                    latency_ms=record.latency_ms,
                    status=record.status,
                    error=record.error,
                )
            )
            await session.commit()

    return sink


class ProcessedEventDeduper:
    def __init__(self, sessionmaker: Callable[[], Any]) -> None:
        self._sessionmaker = sessionmaker

    async def reserve(self, event_id: str) -> bool:
        async with self._sessionmaker() as session:
            session.add(ProcessedEvent(event_id=event_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        return True
