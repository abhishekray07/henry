from __future__ import annotations

from typing import Any, Literal, Protocol, Sequence, runtime_checkable

from henry.contracts import ToolSpec
from henry.sandbox_types import ExecRequest, ExecResult, SandboxPolicy
from henry.types import ChannelState, ConversationTranscript, MemoryItem


@runtime_checkable
class Memory(Protocol):
    async def remember(
        self,
        channel_id: str,
        content: str,
        kind: str = "fact",
        metadata: dict | None = None,
    ) -> None: ...

    async def recall(self, channel_id: str, query: str, k: int = 8) -> list[MemoryItem]: ...

    async def get(self, channel_id: str, path: str) -> MemoryItem | None: ...

    async def list_paths(self, channel_id: str) -> list[str]: ...

    async def snapshot(self, channel_id: str) -> ChannelState: ...

    async def refresh_snapshot(self, channel_id: str, transcript: ConversationTranscript) -> None: ...


@runtime_checkable
class Sandbox(Protocol):
    async def start(self, policy: SandboxPolicy) -> str: ...

    async def exec(self, session: str, req: ExecRequest) -> ExecResult: ...

    async def write_file(self, session: str, path: str, content: bytes) -> None: ...

    async def read_file(self, session: str, path: str) -> bytes: ...

    async def destroy(self, session: str) -> None: ...


@runtime_checkable
class Integration(Protocol):
    name: str
    auth_type: Literal["none", "static_token", "oauth"]
    allowed_domains: Sequence[str]

    def tools(self) -> list[ToolSpec]: ...

    def prompt_fragment(self) -> str: ...


@runtime_checkable
class ToolsetProvider(Protocol):
    """Optional integration capability: contribute a pydantic-ai toolset (e.g. an MCP server).

    Deliberately separate from Integration so existing runtime_checkable isinstance
    checks on Integration implementers stay valid.
    """

    def toolset(self) -> Any: ...
