from __future__ import annotations

from henry.types import ChannelState


def build_instructions(base: str, snapshot: ChannelState, fragments: list[str]) -> str:
    sections = [base.strip()]

    cleaned_fragments = [fragment.strip() for fragment in fragments if fragment.strip()]
    if cleaned_fragments:
        sections.append("<integrations>\n" + "\n\n".join(cleaned_fragments) + "\n</integrations>")

    memory_lines = [f"channel_id: {snapshot.channel_id}"]
    if snapshot.rolling_summary:
        memory_lines.append(f"rolling_summary: {snapshot.rolling_summary}")
    if snapshot.key_facts:
        memory_lines.append(f"key_facts: {snapshot.key_facts}")
    if snapshot.open_tasks:
        memory_lines.append(f"open_tasks: {snapshot.open_tasks}")
    sections.append("<channel_memory>\n" + "\n".join(memory_lines) + "\n</channel_memory>")

    return "\n\n".join(section for section in sections if section)
