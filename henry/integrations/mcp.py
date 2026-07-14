from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_VAR_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


class MCPServerDef(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    tools: list[str] | None = None
    on_tool_error: Literal["error", "retry"] = "error"
    init_timeout: float = 5.0
    read_timeout: float = 60.0

    @model_validator(mode="after")
    def _exactly_one_transport(self) -> MCPServerDef:
        if bool(self.command) == bool(self.url):
            raise ValueError("server needs exactly one of 'command' (stdio) or 'url' (HTTP)")
        return self


def _expand(value: str, *, server: str) -> str:
    def sub(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        resolved = os.environ.get(name, default)
        if resolved is None:
            raise ValueError(f"mcp server {server!r}: undefined environment variable ${{{name}}}")
        return resolved

    return _VAR_RE.sub(sub, value)


def _expand_all(raw: dict, *, server: str) -> dict:
    def walk(value):
        if isinstance(value, str):
            return _expand(value, server=server)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return walk(raw)


def load_mcp_config(path: str | Path, *, explicit: bool) -> dict[str, MCPServerDef]:
    """Parse a Claude-Desktop-style ``mcpServers`` configuration file."""
    file = Path(path)
    if not file.exists():
        if explicit:
            raise FileNotFoundError(f"HENRY_MCP_CONFIG_PATH points to a missing file: {file}")
        return {}

    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON in {file.name} ({file}): {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("mcpServers"), dict):
        raise ValueError(f"{file}: top-level 'mcpServers' object is required")

    definitions: dict[str, MCPServerDef] = {}
    for name, raw in payload["mcpServers"].items():
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError(
                f"{file}: invalid server name {name!r} — names become tool prefixes and must match "
                f"{_NAME_RE.pattern}"
            )
        if not isinstance(raw, dict):
            raise ValueError(f"{file}: server {name!r} must be an object")
        try:
            validated = MCPServerDef.model_validate(raw)
            expanded = _expand_all(validated.model_dump(), server=name)
            definitions[name] = MCPServerDef.model_validate(expanded)
        except ValidationError as exc:
            raise ValueError(f"{file}: server {name!r}: {exc}") from exc
    return definitions
