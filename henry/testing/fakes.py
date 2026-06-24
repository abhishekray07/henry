from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

try:
    from pydantic_ai import RunContext
except ImportError:  # pragma: no cover - only used before dependencies are installed
    RunContext = Any

from henry.contracts import AgentDeps, RunResult, ToolSpec
from henry.sandbox_types import ExecRequest, ExecResult, SandboxPolicy
from henry.types import ChannelState, ConversationTranscript, MemoryItem


class FakeMemory:
    def __init__(self) -> None:
        self._items: dict[str, list[MemoryItem]] = defaultdict(list)
        self._snapshots: dict[str, ChannelState] = {}
        self.refreshed: list[tuple[str, ConversationTranscript]] = []

    async def remember(
        self,
        channel_id: str,
        content: str,
        kind: str = "fact",
        metadata: dict | None = None,
    ) -> None:
        index = len(self._items[channel_id]) + 1
        self._items[channel_id].append(
            MemoryItem(
                path=f"{kind}/{index}",
                content=content,
                kind=kind,
                metadata=metadata or {},
            )
        )

    async def recall(self, channel_id: str, query: str, k: int = 8) -> list[MemoryItem]:
        terms = query.lower().split()
        matches = [
            item
            for item in self._items[channel_id]
            if not terms or any(term in item.content.lower() or term in item.path.lower() for term in terms)
        ]
        return matches[:k]

    async def get(self, channel_id: str, path: str) -> MemoryItem | None:
        for item in self._items[channel_id]:
            if item.path == path:
                return item
        return None

    async def list_paths(self, channel_id: str) -> list[str]:
        return [item.path for item in self._items[channel_id]]

    async def snapshot(self, channel_id: str) -> ChannelState:
        return self._snapshots.get(channel_id, ChannelState(channel_id=channel_id))

    async def refresh_snapshot(self, channel_id: str, transcript: ConversationTranscript) -> None:
        self.refreshed.append((channel_id, transcript))
        self._snapshots[channel_id] = ChannelState(channel_id=channel_id, rolling_summary=transcript.render())


@dataclass
class FakeSandbox:
    canned_result: ExecResult = field(default_factory=lambda: ExecResult(exit_code=0, stdout="ok", stderr=""))
    calls: list[tuple[str, Any]] = field(default_factory=list)
    files: dict[tuple[str, str], bytes] = field(default_factory=dict)
    next_session: int = 1

    async def start(self, policy: SandboxPolicy) -> str:
        session = f"fake-session-{self.next_session}"
        self.next_session += 1
        self.calls.append(("start", policy))
        return session

    async def exec(self, session: str, req: ExecRequest) -> ExecResult:
        self.calls.append(("exec", session, req))
        return self.canned_result

    async def write_file(self, session: str, path: str, content: bytes) -> None:
        self.calls.append(("write_file", session, path, content))
        self.files[(session, path)] = content

    async def read_file(self, session: str, path: str) -> bytes:
        self.calls.append(("read_file", session, path))
        return self.files[(session, path)]

    async def destroy(self, session: str) -> None:
        self.calls.append(("destroy", session))


class FakeIntegration:
    name = "fake"
    auth_type = "none"
    allowed_domains: tuple[str, ...] = ()

    def tools(self) -> list[ToolSpec]:
        async def echo(ctx: RunContext[AgentDeps], text: str) -> str:
            return f"{ctx.deps.ctx.channel_id}:{text}"

        return [echo]

    def prompt_fragment(self) -> str:
        return "Fake integration is available for echo tests."


@dataclass
class FakeAgentRunner:
    result: RunResult = field(default_factory=lambda: RunResult(output="fake output"))
    calls: list[tuple[AgentDeps, str, ConversationTranscript | None]] = field(default_factory=list)

    async def run(
        self,
        deps: AgentDeps,
        user_prompt: str,
        transcript: ConversationTranscript | None = None,
    ) -> RunResult:
        self.calls.append((deps, user_prompt, transcript))
        return self.result
