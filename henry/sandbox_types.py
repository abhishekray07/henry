from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class SandboxPolicy:
    image: str = "henry-sandbox:base"
    workdir: str = "/workspace"
    mem_mb: int = 1024
    cpus: float = 1.0
    network: str = "none"
    allow_domains: tuple[str, ...] = ()
    default_timeout_s: int = 120
    ttl_s: int = 900


@dataclass(frozen=True)
class ExecRequest:
    cmd: Sequence[str]
    timeout_s: int | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: int = 0
    truncated: bool = False
