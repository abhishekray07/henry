# Henry — Design Document

> **Status:** Design locked via brainstorming + `/grill-me` (2026-06-23). Ready for an implementation plan.
> **Name:** "Henry" is a **placeholder** — all branding flows from one config constant so it can be renamed in one place.
> **License:** AGPL-3.0 + CLA.

## 1. What we're building

An **open-source, self-hosted, model-agnostic "Claude Tag"** — an AI teammate that lives in Slack: you `@mention` it in a channel, it does real work in the thread where the whole team can watch, it remembers the channel over time, and (later phases) it can act proactively and run long jobs.

It is the open version of Anthropic's Claude Tag, differentiated by the **wedge**:

| | Claude Tag (Anthropic) | Henry (this project) |
|---|---|---|
| Where it runs | Anthropic's cloud | **Your own box** (self-hosted) |
| Which model | Anthropic + Opus only | **Any model** (bring-your-own-LLM) |
| Openness | Closed | **Open source + auditable** |

Henry is grown from an existing internal bot (Opslane's `support-engineer`, ~1.3k LOC: Slack Bolt + an agent loop + a few API tools). That bot already does Claude Tag's flagship use case — triage a support bug by reading the codebase. Henry generalizes it.

## 2. Locked decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| — | Wedge | Self-hosted + any model + private/auditable | The thing Claude Tag can't be |
| — | Foundation | Fresh repo; **port patterns** from `support-engineer` | Clean OSS repo; we swap the engine anyway |
| 1 | Project home | New repo at `/henry`; `support-engineer` stays Opslane-internal | Its HelpScout/LogRocket tools become a **private integration pack** (proof the seam works) |
| 2 | V1 tools | **GitHub + Web search + `run_code`** | Self-contained demo: "ask about → triage → fix → test → PR." No Opslane-specifics |
| 3 | Database | **Postgres** from the start | Memory + config + tasks; pgvector-ready |
| 4 | Memory | **Build our own** per-channel notebook, behind a `Memory` interface | Small enough to fit in context; **mem0** swaps in later for scale. Not supermemory (closed/Cloudflare-locked) |
| 5 | Model layer | **Pydantic AI native now**, LiteLLM **gateway at governance** | Pydantic AI alone = any model; LiteLLM's real value is the gateway (spend caps/audit) |
| 6 | Sandbox | **Docker per session** (default), swappable | Reproducible clone→test, self-hosted, no cloud. microVM/managed swap in for hostile/multi-tenant |
| 7 | Concurrency | **Per-thread lock** + DB-protected memory writes | Parallel across threads, orderly within one; "one barista per ticket" |
| 8 | Engine | **Pydantic AI** agent loop (replaces `claude-agent-sdk`) | Model-agnostic, MCP-native, type-safe Python |
| 8 | Integrations | **Option D** — internal `Integration` seam; formal plugins later | Get the clean seam without ceremony; memory & scheduler are **core**, not plugins |
| 8 | Secrets | **Host-side, never sent to the model** | Delivers privacy. (NOT "hidden from tool code" — that's the deferred egress proxy) |
| 8 | Not Composio / not real CLIs | MCP + Python function tools | Hosted middleman breaks self-host; CLIs are a tax for a chat bot |
| 8 | Name / License | "Henry" (placeholder) / **AGPL-3.0 + CLA** | Easy to rename; AGPL keeps self-hosting free, deters closed resellers, CLA preserves a future cloud; loosen later if needed |

### The security distinction we will not blur
- **What we deliver in V1:** *secrets are never **sent to the model*** (not in prompts, tool schemas, tool results, exceptions, or logs).
- **What we do NOT yet claim:** *secrets are inaccessible to tool code.* That requires the sandbox **egress proxy** (junior's model) and is deferred. We will not market one as the other.

## 3. Architecture

```
Slack (Bolt, Socket Mode)
   │  every message
   ▼
GATE              "should I act?"  — direct @mention = yes; ambient (phase 2) = cheap classifier
   │  act
   ▼
ORCHESTRATOR      acquire per-thread lock · load channel_config (the channel's "identity") · load memory snapshot
   │
   ▼
AGENT  (Pydantic AI loop, any model)
   │   tools ───────────────┐
   │                        ▼
   │                 INTEGRATIONS (behind the Integration seam)
   │                   • GitHub (API, static token)
   │                   • Web search
   │                   • run_code  ──►  SANDBOX (Docker per session)
   │                   • read_memory / write_memory
   ▼
Slack thread      status via Assistant set_status · streamed progress · chunked final answer

  side: MEMORY (Postgres) · SCHEDULER (phase 3) · GOVERNANCE wraps all (phase 4)
```

### Components
- **Transport — Slack Bolt, Socket Mode (async).** No public URL needed (great for self-host). HTTP endpoint + per-user OAuth arrive when OAuth-based integrations land.
- **Gate.** V1: pass-through for direct `@mentions`. Phase 2 adds the cheap-classifier ambient gate + cooldowns.
- **Orchestrator.** Resolves `channel_id`/`thread_ts`, takes the per-thread lock, loads `channel_config` + `channel_state`, builds the agent, runs it, persists results, refreshes the snapshot.
- **Agent runtime — Pydantic AI.** Model chosen per channel via native provider config (any LLM). Tools registered from the channel's enabled integrations.
- **Integrations.** Tools + auth + a prompt snippet, behind an internal interface (§4).
- **Sandbox.** Docker container per session for `run_code` (§6).
- **Memory.** Per-channel notebook in Postgres (§5).
- **Scheduler / Governance.** Later phases.

## 4. The Integration seam (Option D)

Integrations are not formal plugins yet — they implement a thin **internal interface**. We promote to formal `plugin.yaml` + `SKILL.md` packages (junior-style) only when we have third-party contributors, multi-provider OAuth, >3–5 integrations, or channel-level enable/disable.

```python
class Integration(Protocol):
    name: str
    enabled_channels: list[str] | Literal["*"]
    required_scopes: list[str]
    auth_type: Literal["none", "static_token", "oauth"]   # V1 uses none / static_token
    allowed_domains: list[str]                              # for later egress allowlisting
    redaction_rules: list[Redactor]                         # keep secrets out of model-visible text/logs
    audit_labels: dict[str, str]

    def tools(self, ctx: ChannelContext) -> list[Tool]:     # Pydantic AI tools (or MCP toolset)
        ...
    def prompt_fragment(self) -> str:                        # how the model should use it
        ...
```

- **First-party tools** = Pydantic AI function tools. **External tools** = MCP toolsets. Both register through `tools()`.
- **Memory and scheduler are core**, not integrations — but they expose the same tool-registration shape so they slot in uniformly.
- Per-channel scoping (which integrations a channel may use) lives in `channel_config`, enforced by the host — never by model input.

## 5. Memory

### Model
Three layers by lifespan: **the thread** (live, ephemeral — already read), **the channel notebook** (long-term, shared, per channel — *this is the memory*), **org-wide** (later).

The channel notebook has two representations:
- **`channel_memory`** — the free-form notebook the agent reads/writes via tools (facts, decisions, task log).
- **`channel_state`** — a small always-fresh snapshot (rolling summary + open tasks + key facts) injected into the system prompt every turn, so the agent has context without a tool round-trip.

### Read path (every turn)
load `channel_state` → inject snapshot into the system prompt → agent may call `read_memory`/`search_memory` for detail → plus the live thread.

### Write path (hybrid)
During work the agent calls `write_memory` / `update_task` for durable items → after the turn, a **cheap model pass** refreshes `rolling_summary` + `open_tasks` from the conversation + notebook (keeps the snapshot current and bounded without trusting the model to self-summarize).

### Interface (so mem0 can drop in later)
```python
class Memory(Protocol):
    async def remember(self, channel_id: str, content: str, kind: str) -> None: ...
    async def recall(self, channel_id: str, query: str, k: int = 8) -> list[MemoryItem]: ...
    async def snapshot(self, channel_id: str) -> ChannelState: ...
```
Default impl = Postgres notebook. Upgrade impl = **mem0** (self-hosted on our Postgres/pgvector) when a channel outgrows context or needs semantic search.

### Isolation (non-negotiable)
Every read/write is fenced to `channel_id`, which the **host** supplies from run context — never from anything the model typed. Channel A can never read Channel B.

## 6. Sandbox (`run_code`)

**Taking actions** (open a PR, create a ticket) = permissioned API tools, **no sandbox** (safety = scoping). **Running code** (clone repo, edit, run tests, data analysis) = **Docker container per session**.

### Lifecycle
1. Start a container from a shipped base image (git + Python + Node + build tools); working dir `/workspace`.
2. `git clone` into `/workspace` using a scoped, short-lived token passed as a hidden env value (never shown to the model).
3. Agent edits files and runs commands (`pytest`, etc.) via `run_bash`; container persists for the task so files/deps survive between commands.
4. Container destroyed on task end/timeout.

### Walls
- **Filesystem:** container sees only its `/workspace`; cannot read host files, `.env`, or other channels' containers; throwaway.
- **Network:** deny-by-default + per-integration domain allowlist (e.g. github.com, package registry).
- **Resources:** CPU/memory/timeout caps (Docker-native) so a runaway/logic-bomb can't take down the host.

### Interface (swappable)
```python
class Sandbox(Protocol):
    async def start(self, image: str, limits: Limits) -> Session: ...
    async def exec(self, session: Session, cmd: str) -> ExecResult: ...
    async def write_file(self, session: Session, path: str, content: bytes) -> None: ...
    async def read_file(self, session: Session, path: str) -> bytes: ...
    async def destroy(self, session: Session) -> None: ...
```
Default = Docker. Swap-ins: OS-level `sandbox-runtime` (lightest, no container) for simple scripts; E2B / Daytona / Modal microVMs for hostile/multi-tenant/scale.

**Isolation caveat (documented):** Docker shares the host kernel — *medium* strength, fine for semi-trusted (your own) repos. Hostile code or multi-tenant → flip to a microVM backend.

## 7. Concurrency

**Per-thread lock + DB-protected memory writes.** A Slack thread runs one agent at a time (no two runs racing in one conversation); different threads in the same channel run in parallel (one slow task doesn't freeze the channel). The shared `channel_memory`/`channel_state` are guarded by Postgres transactions so parallel threads can't corrupt them.

V1 is a single async process; the per-thread lock is in-process. When we scale to multiple replicas, the lock becomes a **Postgres advisory lock** (or Redis) keyed by thread.

## 8. Data model (Postgres)

```sql
channel_config(
  channel_id TEXT PRIMARY KEY,
  system_prompt TEXT, model TEXT, enabled_integrations JSONB,
  ambient_on BOOL DEFAULT false, budget_caps JSONB, updated_at TIMESTAMPTZ
)
channel_memory(
  channel_id TEXT, path TEXT, content TEXT, updated_at TIMESTAMPTZ,
  PRIMARY KEY (channel_id, path)
)
channel_state(
  channel_id TEXT PRIMARY KEY,
  rolling_summary TEXT, open_tasks JSONB, key_facts JSONB, updated_at TIMESTAMPTZ
)
tasks(                      -- phase 3 (durable scheduler); table seeded earlier
  id BIGSERIAL PRIMARY KEY, channel_id TEXT, thread_ts TEXT,
  kind TEXT, run_at TIMESTAMPTZ, status TEXT, payload JSONB, created_at TIMESTAMPTZ
)
audit_log(                  -- phase 4 (governance); cheap to start logging early
  id BIGSERIAL PRIMARY KEY, channel_id TEXT, actor TEXT, action TEXT,
  integration TEXT, tokens INT, cost_usd NUMERIC, ts TIMESTAMPTZ
)
```
Access via async SQLAlchemy/SQLModel so the schema is owned and migratable.

## 9. V1 scope & non-goals

**In V1:** any-channel operation; per-channel memory; Pydantic AI + any model; GitHub + Web + `run_code` (Docker sandbox); per-thread concurrency; Socket Mode; chunked replies + `set_status`; basic per-run budget cap (carried from `support-engineer`).

**Explicitly NOT in V1 (later phases):** ambient/proactive mode; durable scheduler / long-running tasks; per-user OAuth + HTTP callback; LiteLLM gateway; formal plugin manifests; sandbox egress-proxy credential injection; admin UI / App Home; vector/semantic memory (mem0); multi-replica scaling.

## 10. Roadmap

0. **Horizontalize** — fresh repo; Pydantic AI + LiteLLM-native engine; drop the single-channel filter; `channel_config` registry; the `Integration` seam; port GitHub + Web tools.
1. **Memory** — Postgres; `channel_memory` + `channel_state`; read/write-memory tools; hybrid write refresh. *(V1 ends here, plus ↓)*
   - **Sandboxed `run_code`** — Docker `Sandbox` backend; the build/test/fix loop.
2. **Ambient** — cheap gate; notify-only; cooldowns; follow-ups.
3. **Scheduler** — durable `tasks` table + worker; self-scheduled long jobs (replaces ephemeral asyncio).
4. **Governance** — per-channel/org spend caps; audit log; per-user OAuth + private resume; LiteLLM gateway; formal plugins; (optional) sandbox egress proxy; multiplayer/handoff polish + admin UI.

## 11. Project meta

- **Repo:** new git repo at `/Users/abhishekray/Projects/opslane/henry`. License **AGPL-3.0** + a **CLA** for outside contributors (preserves dual-license/cloud optionality).
- **Branding:** one config constant (`APP_NAME`, bot display name, package name) — renaming "Henry" is a single-file change.
- **Layout (proposed):** `henry/` (package: `app.py` harness, `agent/`, `integrations/`, `memory/`, `sandbox/`, `config/`, `db/`), `tests/`, `docs/`, `compose.yaml` (Postgres for dev), `pyproject.toml`.
- **Packaging:** Python 3.12, async throughout; `pip`/`uv` installable; `docker compose up` brings Postgres.

## 12. Proposed defaults (open — veto any)

These weren't deep-grilled; these are the recommended defaults to be confirmed at plan time:
- **`Integration` interface fields** as in §4 (Codex's list).
- **Web-search provider** pluggable; default to Pydantic AI's built-in web search or a simple keyed provider (Tavily/Brave/Exa).
- **Per-channel config** = a default config file + per-channel DB overrides; polished admin UI deferred to governance.
- **Replies** = Slack Assistant `set_status("…")` for "what Claude's doing" + streamed progress + chunked final (reuse `support-engineer`'s 3900-char splitter).
- **Cross-model quality** = a small eval/capability harness early (Codex's warning: "model-agnostic ≠ tool-calling-equivalent"); document which models are known-good.
- **Identity** = a single shared bot/service identity in V1 (configured tokens); per-user identity arrives with OAuth.
- **Budget** = carry `support-engineer`'s per-run `max_budget_usd`; per-channel/org caps at governance.
```
