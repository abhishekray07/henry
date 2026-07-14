from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULTS_PATH = Path(__file__).with_name("defaults.json")


class ResolvedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    enabled_integrations: list[str] | Literal["*"] = Field(default_factory=list)
    system_prompt: str
    ambient_on: bool = False
    budget_caps: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        # Empty means "no channel override": RunSettings falls back to HENRY_DEFAULT_MODEL.
        return value.strip()


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_defaults() -> dict[str, Any]:
    return json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))


def _row_to_overrides(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "_mapping"):
        row = row._mapping
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        data = {
            name: getattr(row, name)
            for name in ("system_prompt", "model", "enabled_integrations", "ambient_on", "budget_caps")
            if hasattr(row, name)
        }
    data.pop("channel_id", None)
    if data.get("enabled_integrations") == "*":
        raise ValueError(
            "channel_config.enabled_integrations must be an explicit list; "
            "'*' is only supported as the built-in default"
        )
    return {key: value for key, value in data.items() if value not in (None, "")}


async def _fetch_config_row(session: Any, channel_id: str) -> Any:
    if hasattr(session, "get_channel_config"):
        return await session.get_channel_config(channel_id)

    from henry.db.models import ChannelConfig

    return await session.get(ChannelConfig, channel_id)


async def load_channel_config(
    session: Any,
    channel_id: str,
    *,
    known_integrations: set[str] | None = None,
) -> ResolvedConfig:
    row = await _fetch_config_row(session, channel_id)
    raw = _deep_merge(_load_defaults(), _row_to_overrides(row))
    resolved = ResolvedConfig.model_validate(raw)

    if known_integrations is not None and resolved.enabled_integrations != "*":
        unknown = set(resolved.enabled_integrations) - known_integrations
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown integrations enabled for {channel_id}: {names}")
    return resolved
