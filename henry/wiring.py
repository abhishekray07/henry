from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from henry.agent.runner import PydanticAgentRunner
from henry.config.registry import ResolvedConfig, load_channel_config
from henry.contracts import AgentDeps, AgentRunner, SlackEvent
from henry.db.session import make_engine, make_sessionmaker
from henry.integrations.registry import discover
from henry.interfaces import Integration, Sandbox
from henry.memory.postgres import PostgresMemory
from henry.orchestrator.locks import ThreadLocks
from henry.orchestrator.runner import (
    ProcessedEventDeduper,
    TranscriptFetcher,
    handle_request,
    make_db_audit_sink,
)
from henry.sandbox.docker import DockerSandbox
from henry.sandbox.tools import clear_sandbox_session
from henry.settings import Settings, get_settings
from henry.types import ChannelContext


@dataclass(frozen=True)
class RunSettings:
    base: Settings
    config: ResolvedConfig

    @property
    def default_model(self) -> str:
        return self.config.model or self.base.default_model

    @property
    def system_prompt(self) -> str:
        return self.config.system_prompt

    @property
    def enabled_integrations(self) -> tuple[str, ...]:
        return tuple(self.config.enabled_integrations)

    @property
    def max_run_usd(self) -> float:
        raw = self.config.budget_caps.get("max_run_usd", self.base.max_run_usd)
        return float(raw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)


@dataclass
class HenryRuntime:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: Any
    http: httpx.AsyncClient
    memory: PostgresMemory
    sandbox: Sandbox
    integrations: dict[str, Integration]
    locks: ThreadLocks
    runner: AgentRunner
    transcript_fetcher: TranscriptFetcher | None = None

    @property
    def deduper(self) -> ProcessedEventDeduper:
        return ProcessedEventDeduper(self.sessionmaker)

    async def load_config(self, event: SlackEvent) -> ResolvedConfig:
        async with self.sessionmaker() as session:
            return await load_channel_config(
                session,
                event.channel_id,
                known_integrations=set(self.integrations),
            )

    async def deps_factory(self, ctx: ChannelContext, config: ResolvedConfig) -> AgentDeps:
        return AgentDeps(
            ctx=ctx,
            memory=self.memory,
            sandbox=self.sandbox,
            http=self.http,
            settings=RunSettings(self.settings, config),
        )

    async def cleanup(self, deps: AgentDeps) -> None:
        await clear_sandbox_session(deps)

    async def handle_event(self, event: SlackEvent) -> list[str]:
        return await handle_request(
            event,
            runner=self.runner,
            memory=self.memory,
            deps_factory=self.deps_factory,
            locks=self.locks,
            config_loader=self.load_config,
            transcript_fetcher=self.transcript_fetcher,
            audit_sink=make_db_audit_sink(self.sessionmaker),
            cleanup=self.cleanup,
        )

    async def close(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()


def build_runtime(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    http: httpx.AsyncClient | None = None,
    sandbox: Sandbox | None = None,
    integrations: dict[str, Integration] | None = None,
) -> HenryRuntime:
    runtime_settings = settings or get_settings()
    runtime_engine = engine or make_engine(runtime_settings)
    sessionmaker = make_sessionmaker(runtime_engine)
    registry = integrations if integrations is not None else discover()
    memory = PostgresMemory(sessionmaker)
    runtime_http = http or httpx.AsyncClient()
    runtime_sandbox = sandbox or DockerSandbox()
    return HenryRuntime(
        settings=runtime_settings,
        engine=runtime_engine,
        sessionmaker=sessionmaker,
        http=runtime_http,
        memory=memory,
        sandbox=runtime_sandbox,
        integrations=registry,
        locks=ThreadLocks(),
        runner=PydanticAgentRunner(registry.values()),
    )
