from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from henry.agent.runner import PydanticAgentRunner
from henry.config.registry import ResolvedConfig, load_channel_config
from henry.contracts import AgentDeps, AgentRunner, SlackEvent
from henry.db.session import make_engine, make_sessionmaker
from henry.integrations.mcp import MCPIntegration, load_mcp_config
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

_LOG = logging.getLogger(__name__)


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
    def enabled_integrations(self) -> tuple[str, ...] | Literal["*"]:
        if self.config.enabled_integrations == "*":
            return "*"
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
        for integration in self.integrations.values():
            if isinstance(integration, MCPIntegration):
                try:
                    await integration.aclose()
                except Exception:  # noqa: BLE001 - continue shutting down remaining resources
                    _LOG.warning(
                        "failed to close mcp server %r; its process may be orphaned",
                        integration.name,
                        exc_info=True,
                    )
        try:
            await self.http.aclose()
        finally:
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

    if integrations is not None:
        registry = integrations
    else:
        registry = discover()
        mcp_definitions = load_mcp_config(
            runtime_settings.mcp_config_path,
            explicit="mcp_config_path" in runtime_settings.model_fields_set,
        )
        overlap = sorted(set(registry) & set(mcp_definitions))
        if overlap:
            raise ValueError(f"mcp server name(s) collide with builtin integrations: {', '.join(overlap)}")
        for name, definition in mcp_definitions.items():
            registry[name] = MCPIntegration(name, definition)

    runtime_engine = engine or make_engine(runtime_settings)
    sessionmaker = make_sessionmaker(runtime_engine)
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
