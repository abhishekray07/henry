from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol

import httpx

from henry.types import ChannelContext, ConversationTranscript

if TYPE_CHECKING:
    from henry.interfaces import Memory, Sandbox


ToolSpec = Callable[..., Any]


@dataclass
class AgentDeps:
    ctx: ChannelContext
    memory: Memory
    sandbox: Sandbox
    http: httpx.AsyncClient
    settings: Any


@dataclass
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    tool_calls: int = 0
    cost_usd: float | None = None


RunStatus = Literal["ok", "budget_exceeded", "error"]


@dataclass
class RunResult:
    output: str
    usage: RunUsage = field(default_factory=RunUsage)
    status: RunStatus = "ok"
    error: str | None = None


class AgentRunner(Protocol):
    async def run(
        self,
        deps: AgentDeps,
        user_prompt: str,
        transcript: ConversationTranscript | None = None,
    ) -> RunResult: ...


@dataclass(frozen=True)
class SlackEvent:
    channel_id: str
    thread_ts: str
    user: str
    text: str
    event_id: str
    event_ts: str
    is_mention: bool
