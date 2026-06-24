from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext

from henry.contracts import AgentDeps, ToolSpec
from henry.types import MemoryItem


def memory_tools() -> list[ToolSpec]:
    async def read_memory(ctx: RunContext[AgentDeps], path: str | None = None) -> dict[str, Any]:
        """Read this Slack channel's memory snapshot, or a specific memory path."""
        channel_id = ctx.deps.ctx.channel_id
        if path:
            item = await ctx.deps.memory.get(channel_id, path)
            return {"item": _item_to_dict(item) if item else None}

        snapshot = await ctx.deps.memory.snapshot(channel_id)
        return {
            "rolling_summary": snapshot.rolling_summary,
            "open_tasks": snapshot.open_tasks,
            "key_facts": snapshot.key_facts,
            "paths": await ctx.deps.memory.list_paths(channel_id),
        }

    async def write_memory(
        ctx: RunContext[AgentDeps],
        content: str,
        kind: str = "fact",
        path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Write durable information to this Slack channel's memory."""
        item_metadata = dict(metadata or {})
        if path:
            item_metadata["path"] = path
        await ctx.deps.memory.remember(ctx.deps.ctx.channel_id, content, kind=kind, metadata=item_metadata)
        return "Memory saved."

    async def search_memory(ctx: RunContext[AgentDeps], query: str, k: int = 8) -> list[dict[str, Any]]:
        """Search this Slack channel's memory."""
        items = await ctx.deps.memory.recall(ctx.deps.ctx.channel_id, query, k=k)
        return [_item_to_dict(item) for item in items]

    return [read_memory, write_memory, search_memory]


def _item_to_dict(item: MemoryItem) -> dict[str, Any]:
    return {
        "path": item.path,
        "content": item.content,
        "kind": item.kind,
        "metadata": item.metadata,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "score": item.score,
    }
