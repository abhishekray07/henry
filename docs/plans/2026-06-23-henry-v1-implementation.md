# Henry V1 — Implementation Plan (parallel worktrees)

> **For Claude:** REQUIRED SUB-SKILLS — **superpowers:using-git-worktrees** (one worktree per workstream), **superpowers:test-driven-development** (RED→GREEN→commit every task), **superpowers:executing-plans** / **superpowers:subagent-driven-development** (drive each workstream).
>
> **Revision history:** v2 (2026-06-23) — reworked Stage 1 after a Codex review: Foundation now defines the **execution contracts** (`AgentDeps`, typed tool shape, `AgentRunner`, transcript/usage/Slack DTOs, sandbox session+policy), not just storage models; registry uses package-scanning (no shared-file edits); DB uses JSONB+indexes+naming conventions; async engine via factory (no import-time global); Alembic baseline manually reviewed.

**Goal:** Ship Henry V1 — an open-source, self-hosted, model-agnostic AI teammate in Slack that works in any channel, remembers each channel, and can read GitHub / search the web / run code in a sandbox.

**Architecture:** Slack Bolt (Socket Mode) → orchestrator (per-thread lock + per-channel config + memory) → Pydantic AI agent (any model) whose tools come from pluggable Integrations + a Docker sandbox. State in Postgres. See `docs/plans/2026-06-23-henry-design.md`.

**Tech stack:** Python 3.12 async, **pydantic-ai (pinned)**, slack-bolt, SQLAlchemy 2 async + asyncpg + Alembic, Docker SDK, pydantic-settings, httpx, pytest + pytest-asyncio. AGPL-3.0 + CLA.

---

## How the parallelism works

Stage 1 (Foundation) is sequential and **merges to `main` first** — it defines every shared **contract** (types + Protocols + DB + fakes) so Stage 2's 5 worktrees compile and test against stable seams. Each Stage 2 workstream owns a **disjoint directory** → zero merge conflicts. Stage 3 wires real impls + a real Slack smoke test.

```
Stage 1 FOUNDATION (sequential) ─► main
   ┌────────────┬────────────┬────────────┬────────────┐
   ▼            ▼            ▼            ▼            ▼     (parallel worktrees)
 WS-A agent  WS-B memory  WS-C integr  WS-D sandbox  WS-E slack/orch
   └────────────┴────────────┼────────────┴────────────┘
                              ▼
Stage 3 INTEGRATION (sequential) ─ wire + e2e smoke ─► main
```

| Workstream | Worktree / branch | Owns (only these) | Depends on (Foundation) |
|---|---|---|---|
| Foundation | `henry` / `main` | repo root, `henry/{__init__,branding,settings,types,interfaces,contracts}.py`, `henry/db/`, `henry/config/`, `henry/integrations/{__init__,registry}.py` + `builtins/__init__.py`, `henry/testing/` | — |
| WS-A Agent | `../henry-ws-a` / `ws-a-agent` | `henry/agent/` | `AgentDeps`, `AgentRunner`, `ToolSpec`, `Memory` |
| WS-B Memory | `../henry-ws-b` / `ws-b-memory` | `henry/memory/` | `Memory`, `ConversationTranscript`, `db.*` |
| WS-C Integrations | `../henry-ws-c` / `ws-c-integrations` | `henry/integrations/builtins/github.py`, `.../web.py` | `Integration`, `ToolSpec`, `AgentDeps` |
| WS-D Sandbox | `../henry-ws-d` / `ws-d-sandbox` | `henry/sandbox/` | `Sandbox`, `SandboxPolicy`, `ExecRequest`, `ExecResult` |
| WS-E Slack/Orch | `../henry-ws-e` / `ws-e-slack` | `henry/slack/`, `henry/orchestrator/` | all contracts + fakes (`FakeAgentRunner`) |
| Integration | `henry` / `main` | `henry/app.py`, `henry/wiring.py` | everything |

**Rule:** edit only your owned dirs; depend on Foundation contracts + `henry/testing` fakes, never another workstream's concrete code. **WS-C adds files under `integrations/builtins/` and is auto-discovered — it never edits a shared file.** If you think you must touch a shared file, the seam is wrong — stop and fix Foundation.

**Worktree setup (after Foundation on `main`):**
```bash
cd /Users/abhishekray/Projects/opslane/henry
for ws in a-agent b-memory c-integrations d-sandbox e-slack; do
  git worktree add ../henry-ws-${ws%%-*} -b ws-$ws main; done
# each: cd ../henry-ws-X && uv sync   (or pip install -e ".[dev]")
```
Stage 3 merge order: B, C, D, A, E.

---

# STAGE 1 — FOUNDATION  (`main`, sequential)

### Task F1: Init repo
**Files:** `pyproject.toml`, `LICENSE`, `CLA.md`, `.gitignore`, `README.md`, `compose.yaml`, `henry/__init__.py`.
- `git init -b main`.
- `pyproject.toml`: **pin** `pydantic-ai==<resolved>` (run `uv add pydantic-ai` and freeze the version — its API moves across minors), plus `slack-bolt>=1.21`, `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.30`, `alembic>=1.14`, `pydantic-settings>=2.6`, `httpx>=0.27`, `docker>=7.1`, `python-dotenv>=1.0`; dev: `pytest>=8`, `pytest-asyncio>=0.24`, `aiosqlite>=0.20` (test DB), `ruff>=0.7`. `[tool.pytest.ini_options] asyncio_mode="auto"`. hatchling.
- `LICENSE` = full AGPL-3.0 text (fetch from gnu.org/licenses/agpl-3.0.txt). `CLA.md` = standard individual CLA. `compose.yaml` = `postgres:17` (`henry/henry/henry`, port 5432).
- Commit: `chore: scaffold repo (AGPL-3.0)`.

### Task F2: Branding + settings
**Files:** `henry/branding.py`, `henry/settings.py`. **Test:** `tests/test_settings.py`.
- `branding.py`: `APP_NAME`, `BOT_DISPLAY_NAME`, `PACKAGE_NAME` — **the only place the name lives.**
- `settings.py`: `Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_prefix="HENRY_", env_file=".env", extra="ignore")`; fields: `database_url`, `slack_bot_token`, `slack_app_token`, `default_model="anthropic:claude-sonnet-4-6"`, `github_token`, `web_search_provider="tavily"`, `web_search_api_key=""`, `litellm_base_url=""` (governance), `max_run_usd=1.00`, `sandbox_image="henry-sandbox:base"`. `get_settings()`.
- Test: branding constants non-empty; settings read `HENRY_*` env. Commit.

### Task F3: Core types + shared contracts  *(EXPANDED — this is the seam)*
**Files:** `henry/types.py` (data), `henry/contracts.py` (agent/tool/run DTOs). **Test:** `tests/test_types.py`.

```python
# henry/types.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

@dataclass(frozen=True)
class ChannelContext:                 # host-supplied; channel_id NEVER from the model
    channel_id: str
    thread_ts: str
    actor_user_id: str | None = None
    run_id: str = ""

@dataclass
class MemoryItem:
    path: str
    content: str
    kind: str = "fact"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    score: float | None = None        # for ranked recall later

@dataclass
class ChannelState:
    channel_id: str
    rolling_summary: str = ""
    open_tasks: list[dict[str, Any]] = field(default_factory=list)
    key_facts: list[dict[str, Any]] = field(default_factory=list)

@dataclass(frozen=True)
class ThreadMessage:
    role: Literal["user", "assistant", "system"]
    text: str
    user: str | None = None
    ts: str | None = None

@dataclass(frozen=True)
class ConversationTranscript:          # typed Slack-derived input (NOT a raw str)
    channel_id: str
    thread_ts: str
    messages: tuple[ThreadMessage, ...]
    def render(self) -> str: ...        # canonical text rendering for prompts/summaries
```

```python
# henry/contracts.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, TYPE_CHECKING
import httpx
from henry.types import ChannelContext

if TYPE_CHECKING:
    from henry.interfaces import Memory, Sandbox

# The Pydantic AI tool ABI WS-A and WS-C must agree on. A tool is a plain async
# callable whose first param is RunContext[AgentDeps] when it needs deps.
ToolSpec = Callable[..., Any]          # validated at registration by Pydantic AI

@dataclass
class AgentDeps:                       # the shared dependency object (Pydantic AI deps_type)
    ctx: ChannelContext
    memory: "Memory"
    sandbox: "Sandbox"
    http: httpx.AsyncClient
    settings: Any                       # henry.settings.Settings (avoid import cycle)

@dataclass
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    cost_usd: float = 0.0

@dataclass
class RunResult:
    output: str
    usage: RunUsage = field(default_factory=RunUsage)
    status: str = "ok"                  # ok | budget_exceeded | error
    error: str | None = None

class AgentRunner(Protocol):           # WS-A implements; WS-E calls; fake in testing
    async def run(self, deps: AgentDeps, user_prompt: str,
                  transcript: "ConversationTranscript | None" = None) -> RunResult: ...

@dataclass
class SlackEvent:                      # canonical event DTO WS-E builds from Bolt
    channel_id: str
    thread_ts: str
    user: str
    text: str
    event_ts: str                       # idempotency key
    is_mention: bool
```
Test: construct each; `ChannelContext`/`ThreadMessage` frozen; `ConversationTranscript.render()` includes message text. Commit.

### Task F4: Interfaces (Protocols)
**Files:** `henry/interfaces.py`. **Test:** `tests/test_interfaces.py` (fakes satisfy `isinstance`).
```python
# henry/interfaces.py
from __future__ import annotations
from typing import Protocol, runtime_checkable, Sequence
from henry.types import ChannelState, MemoryItem, ConversationTranscript
from henry.contracts import ToolSpec
from henry.sandbox_types import SandboxPolicy, ExecRequest, ExecResult   # F-types below

@runtime_checkable
class Memory(Protocol):
    async def remember(self, channel_id: str, content: str, kind: str = "fact",
                       metadata: dict | None = None) -> None: ...
    async def recall(self, channel_id: str, query: str, k: int = 8) -> list[MemoryItem]: ...   # lexical in v0
    async def list_paths(self, channel_id: str) -> list[str]: ...
    async def snapshot(self, channel_id: str) -> ChannelState: ...
    async def refresh_snapshot(self, channel_id: str, transcript: ConversationTranscript) -> None: ...

@runtime_checkable
class Sandbox(Protocol):
    async def start(self, policy: SandboxPolicy) -> str: ...          # session id
    async def exec(self, session: str, req: ExecRequest) -> ExecResult: ...
    async def write_file(self, session: str, path: str, content: bytes) -> None: ...
    async def read_file(self, session: str, path: str) -> bytes: ...
    async def destroy(self, session: str) -> None: ...

@runtime_checkable
class Integration(Protocol):
    name: str
    auth_type: str                       # "none" | "static_token" | "oauth"
    allowed_domains: Sequence[str]
    def tools(self) -> list[ToolSpec]: ...    # tools read deps.ctx at call time (no ctx param)
    def prompt_fragment(self) -> str: ...
```
```python
# henry/sandbox_types.py  (Foundation; WS-D imports these)
from dataclasses import dataclass, field
from typing import Sequence
@dataclass(frozen=True)
class SandboxPolicy:
    image: str = "henry-sandbox:base"; workdir: str = "/workspace"
    mem_mb: int = 1024; cpus: float = 1.0
    network: str = "none"; allow_domains: tuple[str, ...] = ()
    default_timeout_s: int = 120; ttl_s: int = 900
@dataclass(frozen=True)
class ExecRequest:
    cmd: Sequence[str]                   # argv, not a shell string
    timeout_s: int | None = None; cwd: str | None = None; env: dict = field(default_factory=dict)
@dataclass
class ExecResult:
    exit_code: int; stdout: str; stderr: str
    timed_out: bool = False; duration_ms: int = 0; truncated: bool = False
```
Test: minimal fakes satisfy each Protocol via `isinstance`. Commit.

### Task F5: DB models + engine factory + migrations
**Files:** `henry/db/{__init__,models,session}.py`, `alembic.ini`, `henry/db/migrations/`. **Test:** `tests/test_db_models.py` (aiosqlite in-memory for unit; compose Postgres for the JSONB/index check).
- `models.py`: SQLAlchemy 2 `DeclarativeBase` with a **naming convention** (`Base.metadata = MetaData(naming_convention=...)`) so Alembic constraints are deterministic. Use **`JSONB`** (`sqlalchemy.dialects.postgresql.JSONB`) for all JSON columns; **timezone-aware** `DateTime(timezone=True)`.
  - `ChannelConfig(channel_id PK, system_prompt, model, enabled_integrations JSONB, ambient_on, budget_caps JSONB, updated_at)`
  - `ChannelMemory(channel_id, path) PK; content; kind; metadata JSONB; created_at; updated_at` — index `(channel_id)`.
  - `ChannelStateRow(channel_id PK, rolling_summary, open_tasks JSONB, key_facts JSONB, updated_at)`
  - `Task(id BigInt PK, channel_id, thread_ts, kind, status, run_at, payload JSONB, dedup_key UNIQUE)` — index `(status, run_at)`.
  - `AuditLog(id, run_id, channel_id, thread_ts, actor, action, integration, model, input_tokens, output_tokens, cost_usd Numeric, latency_ms, status, error, ts default now())` — index `(channel_id, ts)`.
- `session.py`: **factories, not import-time globals** — `make_engine(settings)` and `make_sessionmaker(engine)` (`expire_on_commit=False`). Tests build their own engine with `NullPool` and dispose it per test (AsyncSession is not safe across event loops).
- Alembic: `alembic init --template async henry/db/migrations`; wire `target_metadata=Base.metadata` + the naming convention; **autogenerate then manually review** the baseline; add `alembic check` to CI.
- Commit.

### Task F6: Channel config registry (async)
**Files:** `henry/config/{__init__,registry}.py`, `henry/config/defaults.yaml`. **Test:** `tests/test_config_registry.py`.
- `ResolvedConfig` = pydantic model (`model`, `enabled_integrations: list[str]`, `system_prompt`, `ambient_on`, `budget_caps`).
- `async load_channel_config(session, channel_id) -> ResolvedConfig` = deep-merge `defaults.yaml` with the `channel_config` row; **validate** unknown keys, that `enabled_integrations` ⊆ known names (validated in Stage 3 against the registry), model non-empty.
- Test merge + validation with a fake session/row. Commit.

### Task F7: Integration registry (package scan, no side-effect imports)
**Files:** `henry/integrations/{__init__,registry}.py`, `henry/integrations/builtins/__init__.py`. **Test:** `tests/test_integration_registry.py`.
```python
# henry/integrations/registry.py
import importlib, pkgutil
from henry.interfaces import Integration
def discover() -> dict[str, Integration]:
    import henry.integrations.builtins as pkg
    found: dict[str, Integration] = {}
    for m in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"{pkg.__name__}.{m.name}")
        integ = mod.get_integration()          # each builtin exposes get_integration()
        if integ.name in found: raise ValueError(f"duplicate integration {integ.name}")
        found[integ.name] = integ
    return found
def get_integrations(names, registry): return [registry[n] for n in names if n in registry]
```
**This is why WS-C never edits a shared file** — it drops `builtins/github.py` exposing `get_integration()` and discovery finds it. (Third-party packages later: Python entry points.) Test with a temp builtin module + duplicate-name error. Commit.

### Task F8: Fakes
**Files:** `henry/testing/{__init__,fakes}.py`. **Test:** `tests/test_fakes.py`.
- `FakeMemory` (dict-backed `Memory`, channel-scoped). `FakeSandbox` = **records calls deterministically** + a canned `ExecResult` (NOT a real subprocess — keep tests hermetic; an optional `LocalExecSandbox` can come later for integration tests). `FakeIntegration` returns one real `ToolSpec` (an async echo tool taking `RunContext[AgentDeps]`). `FakeAgentRunner` (returns a canned `RunResult`) for WS-E.
- Test each satisfies its Protocol + `FakeAgentRunner.run` returns a `RunResult`. Commit.

**Foundation done → `main` is the stable base. Create the 5 worktrees.**

---

# STAGE 2 — PARALLEL WORKSTREAMS

> TDD every task, commit per task, only your owned dirs, depend on Foundation contracts + `henry/testing` fakes. Use Pydantic AI's `TestModel`/`FunctionModel` so no task hits a real LLM.

## WS-A — Agent runtime  (`henry/agent/`)
- **A1 model** `model.py`: `build_model(model_str, settings)` — native string by default; if `settings.litellm_base_url`, build `OpenAIChatModel(model_str, provider=OpenAIProvider(base_url=..., api_key=...))`. Test native string → model object, no network.
- **A2 prompt** `prompt.py`: `build_instructions(base, snapshot: ChannelState, fragments: list[str]) -> str` — stable base before the cache point, `<channel_memory>` snapshot after. Test snapshot fields appear.
- **A3 runner** `runner.py`: implement `AgentRunner`. Build `Agent(build_model(...), deps_type=AgentDeps, instructions=..., tools=[*integration tools, *memory_tools(), *sandbox_tools()])`; `run()` calls `agent.run(prompt, deps=deps, usage_limits=UsageLimits(...))`, maps result → `RunResult` (output + usage + status). `memory_tools`/`sandbox_tools` imported behind `henry/agent/_tools.py` (stubs from `henry.testing` until B/D merge — one-line swap in Stage 3). Test with `TestModel` + `FakeIntegration`: the agent calls the echo tool; budget path returns `status="budget_exceeded"`. Commit each.

## WS-B — Memory  (`henry/memory/`)
- **B1** `postgres.py`: `PostgresMemory` implements `Memory` over `db.models` (session injected). `recall` = lexical scan over `channel_memory` (v0). **Every query filters `channel_id`.** Test write→snapshot→recall; assert channel A's data is invisible from channel B.
- **B2** `tools.py`: `memory_tools()` → `read_memory`/`write_memory`/`search_memory`, each `RunContext[AgentDeps]`, using `ctx.deps.ctx.channel_id` (host) + `ctx.deps.memory`. **channel_id is never a model-visible arg.** Test with `FakeMemory`.
- **B3** `summarizer.py`: `refresh_snapshot(channel_id, transcript: ConversationTranscript)` runs a cheap model over `transcript.render()` → updates `rolling_summary`/`open_tasks`. Test with `FunctionModel` returning a canned summary. Commit each.

## WS-C — Integrations  (`henry/integrations/builtins/github.py`, `web.py`)
- **C1 github**: tools `search_code`/`get_file`/`list_commits` + write tools `open_pr`/`create_issue` (permissioned, no sandbox), each `RunContext[AgentDeps]`; token from `deps.settings.github_token` attached host-side (header, never returned/logged). `GithubIntegration(name="github", auth_type="static_token", allowed_domains=["api.github.com","github.com"])`; module exposes `get_integration()`. Mock httpx; assert token in header, NOT in tool output. 
- **C2 web**: `web_search`/`web_fetch`, provider from `settings.web_search_provider`; `WebIntegration`; `get_integration()`. Mock provider.
- **C3**: test `discover()` finds `github` + `web`. (No shared-file edit — discovery is automatic.) Commit each.

## WS-D — Sandbox  (`henry/sandbox/`)
- **D1** `docker.py`: `DockerSandbox` implements `Sandbox`. **Docker SDK calls are blocking → wrap in `asyncio.to_thread`.** `start(policy)` creates a container: `mem_limit`, `nano_cpus`, `network_mode="none"` (or a custom allowlisted network), `read_only=True` + writable `/workspace`; `exec(session, req)` = `exec_run(req.cmd, ...)` with a **timeout enforced by killing the container** (exec_run has no native timeout) → set `timed_out`; cap+`truncated` stdout; `write_file`/`read_file` via `put_archive`/`get_archive`; `destroy` removes. Test (`@pytest.mark.docker`, skip if no Docker): start→write→exec `cat`→read→destroy; assert a host path is unreadable inside.
- **D2** `Dockerfile.base` (+ build doc): git, python3, node, build-essential → `henry-sandbox:base`.
- **D3** `tools.py`: `sandbox_tools(sandbox)` → `run_bash`/`write_file`/`clone_repo` (`RunContext[AgentDeps]`); session keyed by `ctx.deps.ctx.thread_ts` (one box per thread-task, reused within the task); `clone_repo` injects scoped GitHub token host-side. Test with `FakeSandbox`. Commit each.

## WS-E — Slack harness + orchestrator  (`henry/slack/`, `henry/orchestrator/`)
- **E1** `slack/context.py`: build `ConversationTranscript` + `SlackEvent` from a Bolt `conversations_replies` payload; `_split_for_slack` (3900-char chunker). Pure functions; test with a fake payload.
- **E2** `orchestrator/locks.py`: `ThreadLocks` = `asyncio.Lock` per `(channel_id, thread_ts)`. Test same-thread serializes / diff-thread concurrent. Doc the Postgres-advisory-lock swap for multi-replica.
- **E3** `orchestrator/runner.py`: `handle_request(event: SlackEvent, runner: AgentRunner, memory, deps_factory)` — take thread lock, `load_channel_config`, build `AgentDeps`, `await runner.run(deps, prompt, transcript)`, `await memory.refresh_snapshot(...)`, write `AuditLog`, return chunked output. Test full path with `FakeAgentRunner` + fakes: returns output, refresh + audit called.
- **E4** `slack/app.py`: `AsyncApp` + `AsyncSocketModeHandler`; `@app.event("app_mention")` (+ dedup on `event_ts`); post `set_status`/placeholder, spawn `handle_request` (don't block ack), `chat_update` + chunked replies. **Any channel** (no channel filter). Mock client; assert placeholder + orchestrator call. Commit each.

---

# STAGE 3 — INTEGRATION  (`main`, sequential; merge B, C, D, A, E first)

- **Z1 wire** `henry/wiring.py` + `henry/app.py`: build `make_engine/make_sessionmaker`, `PostgresMemory`, `DockerSandbox`, `discover()` integrations, `ThreadLocks`, the real `AgentRunner`; swap WS-A's `_tools.py` stub → real `memory_tools`/`sandbox_tools`; validate `enabled_integrations` against the registry. Test wiring builds against compose Postgres.
- **Z2 migrations/runbook**: `docker compose up -d db && alembic upgrade head && python -m henry.app`; `README` run steps.
- **Z3 e2e smoke (actually run it; evidence required):** invite the bot to two real channels and verify:
  1. `@Henry` in ch1 "what does `build_agent` do in this repo?" → uses GitHub tool, answers. *(any-channel + GitHub)*
  2. `@Henry` in ch1 and ch2 at once → both respond in parallel. *(per-thread concurrency)*
  3. Tell ch1 a fact; in a new thread ask it to recall → remembers; ch2 does NOT know it. *(memory + isolation)*
  4. `@Henry` "write python that prints fib(10) and run it" → runs in Docker, returns output. *(run_code)*
  5. `grep` the logs/audit for the GitHub token → **empty** (secrets never surface).
  Record under "Smoke results."
- **Z4 gate:** `ruff check .` → `pytest -q` → Z3. Don't claim done until Z3 shows real output (verification-before-completion).

## Definition of Done (V1)
All workstreams merged; `pytest` green; `ruff` clean; `docker compose up` + `python -m henry.app` runs; Z3 shows any-channel ✅, parallel threads ✅, per-channel memory + isolation ✅, sandboxed run_code ✅, token-not-in-logs ✅.

## Open / proposed defaults (veto any)
Web provider (Tavily default) · per-channel config editing (defaults file + DB row; admin UI later) · replies via `set_status` + chunked final · cross-model eval harness (Codex: "model-agnostic ≠ tool-calling-equivalent") · single shared bot identity in V1 (per-user identity with OAuth later) · agent within-run history uses Pydantic AI `message_history`; cross-run continuity = channel memory (no ModelMessage persistence in V1).
```
