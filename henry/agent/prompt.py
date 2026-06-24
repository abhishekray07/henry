from __future__ import annotations

from typing import Any

from henry.types import ChannelState


def _render_items(items: list[dict[str, Any]]) -> str:
    lines = []
    for item in items:
        parts = [f"{key}: {value}" for key, value in item.items()]
        lines.append("- " + ", ".join(parts) if parts else "- (empty)")
    return "\n".join(lines)


def build_instructions(base: str, snapshot: ChannelState, fragments: list[str]) -> str:
    sections = [base.strip()]

    cleaned_fragments = [fragment.strip() for fragment in fragments if fragment.strip()]
    if cleaned_fragments:
        sections.append("<integrations>\n" + "\n\n".join(cleaned_fragments) + "\n</integrations>")

    memory_lines = [f"channel_id: {snapshot.channel_id}"]
    if snapshot.rolling_summary:
        memory_lines.append(f"rolling_summary: {snapshot.rolling_summary}")
    if snapshot.key_facts:
        memory_lines.append("key_facts:\n" + _render_items(snapshot.key_facts))
    if snapshot.open_tasks:
        memory_lines.append("open_tasks:\n" + _render_items(snapshot.open_tasks))
    sections.append("<channel_memory>\n" + "\n".join(memory_lines) + "\n</channel_memory>")

    return "\n\n".join(section for section in sections if section)
