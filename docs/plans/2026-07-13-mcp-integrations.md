# MCP Server Support for Henry

## Context

Henry's design premise (docs/plans/2026-06-23-henry-design.md:100) is "External tools = MCP toolsets," but the implementation only supports hand-built Python integrations (`github`, `web`). This came to a head when @Henry couldn't investigate a HelpScout link — the answer shouldn't be a hand-built HelpScout integration, it should be "add a HelpScout MCP server to config and enable it for the channel." This plan adds MCP servers as first-class integrations: defined in a Claude-Desktop-style `mcp.json`, gated per-channel via the existing `enabled_integrations` mechanism, and attached to the per-run pydantic-ai `Agent` as toolsets.

Verified environment facts: pydantic-ai 2.0.0 (installed) ships a FastMCP-based `pydantic_ai.mcp.MCPToolset` (NOT the older `MCPServerStdio` classes; the `mcp` extra is already installed: `mcp` 1.28.0 + `fastmcp-slim` 3.4.2). `MCPToolset` is a ref-counted async context manager; `agent.run()` enters the run's toolsets at run start and exits them at run end. `fastmcp.client.transports.StdioTransport` defaults `keep_alive=True`: the subprocess survives context exits and is reused on the next connect — and is NOT killed by merely exiting the toolset context.

**Revision note (post-review):** an earlier draft pinned MCP connections open from startup (`runtime.start()`) with a reconnect guard. Two independent reviews (internal + Codex) found that design unimplementable: the refcount never reaches zero while pinned, so a dead server can never reconnect (`pydantic_ai/mcp.py:1072`; fastmcp `client.py:553` rejects restart at nonzero nesting), the guard had no async execution hook, and startup-connecting every configured server contradicts per-channel opt-in. This revision drops startup pinning entirely.

**Revision note 2 (product decision):** the OSS version targets a single-person, single-workspace deployment — operator, admin, and user are the same person. Per-channel opt-in was multitenancy friction with no one to serve: adding a server to `mcp.json` already expresses intent. Enablement therefore defaults to **on everywhere** via a `"*"` sentinel, and `channel_config` becomes the exception/tuning mechanism (e.g. write-capable servers only in an ops channel) rather than a gate. The per-server `tools` allowlist and no-auto-retry defaults now carry the safety weight. When a hosted/multi-tenant version happens, definitions move behind a real admin console and per-channel opt-in returns as the default — nothing here blocks that.

## Design

### Lifecycle: lazy per-run, keep-alive processes, explicit shutdown

- No `runtime.start()`. `agent.run(...)` already enters/exits the run's toolsets (`pydantic_ai/agent/__init__.py:1491`). The refcount returns to zero after each run, so **a dead server heals on the next run's re-enter** — no reconnect guard needed. A server death mid-run fails that one run (caught by the runner's existing exception handling → audited error result).
- stdio servers: `StdioTransport(keep_alive=True)` (the default) keeps the subprocess alive across runs, so per-run entering costs a session handshake, not a process spawn. HTTP servers re-handshake per run — acceptable for V1.
- Servers a channel never enables are never spawned/connected (per-run toolsets come only from `_active_integrations`).
- Shutdown: exiting the context does NOT kill keep-alive subprocesses (`fastmcp/client/transports/stdio.py:29,70`). `MCPIntegration.aclose()` explicitly closes the underlying fastmcp client/transport; `HenryRuntime.close()` calls `aclose()` on every `MCPIntegration` (each in try/except, idempotent, safe if never connected) BEFORE closing http/engine.
- **Fix existing latent leak while here:** in `henry/app.py` `amain()`, the `try/finally runtime.close()` currently begins only around `handler.start_async()`; a failure in `create_slack_app`, `auth_test`, or fetcher wiring leaks the engine/http — and would leak MCP processes too. Move everything after `build_runtime(...)` inside the `try`.

### Config — `mcp.json`

Path from new `Settings.mcp_config_path` (env `HENRY_MCP_CONFIG_PATH`, default `"mcp.json"`). Claude-Desktop `mcpServers` shape plus Henry-specific optional keys (`description`, `tools`, `on_tool_error`, `init_timeout`, `read_timeout`) — these extra keys are why we parse ourselves rather than reuse `pydantic_ai.mcp.load_mcp_toolsets` (its schema doesn't carry them; name recovery alone would not justify a custom parser):

```json
{
  "mcpServers": {
    "helpscout": {
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${HELPSCOUT_API_KEY}"},
      "description": "Read Help Scout conversations and customers",
      "tools": ["get_conversation", "search_conversations"],
      "on_tool_error": "error",
      "read_timeout": 60
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
      "env": {"KEY": "${SOME_VAR:-default}"}
    }
  }
}
```

Rules:
- `${VAR}` / `${VAR:-default}` expansion mirroring pydantic-ai's semantics; secrets come from `.env` (already loaded by `main()` via `load_dotenv`). Undefined `${VAR}` without default → raise at startup naming server + variable.
- Server names must match `^[a-zA-Z0-9_-]{1,64}$` (they become tool-name prefixes; providers enforce this pattern). Warn at load time that long server names + long tool names can exceed provider tool-name limits (not verifiable until runtime).
- `command/args/env/cwd` (stdio) XOR `url/headers` (HTTP/SSE) — exactly one; else raise.
- Malformed JSON / schema violation → raise at startup with server name + path.
- **Missing file:** `load_mcp_config(path, *, explicit: bool)` — wiring passes `explicit = "mcp_config_path" in settings.model_fields_set`. Explicit path missing → raise (an operator typo must not silently disable integrations). Default path missing → `{}` with a debug log.

### Enablement — on everywhere by default (single-tenant)

- `enabled_integrations` gains a `"*"` sentinel meaning "all registered integrations." `defaults.json` ships `"enabled_integrations": "*"` — configuring a server in `mcp.json` is enough; no per-channel SQL. (The original design doc's protocol sketch already had `enabled_channels: "*"`; this restores that intent.)
- Resolution: `load_channel_config` skips unknown-name validation when the value is `"*"`; the runner's `_active_integrations` treats `"*"` as "all registered" (this keeps the sentinel working even for the orchestrator's default config loader, which has no `known_integrations` set). `RunSettings.enabled_integrations` passes the sentinel through.
- A `channel_config` row with an explicit list still overrides to scope a channel down (the exception mechanism, e.g. write-capable servers only in an ops channel); a row's explicit list is validated against known names exactly as today.
- Trade-off, documented: every enabled server's tool schemas ride on every run. Fine at a handful of servers; per-channel scoping is the relief valve, smart routing is future work.

### Adapter — new `henry/integrations/mcp.py`

- `MCPServerDef` pydantic model per the schema above.
- `MCPIntegration` satisfies the existing `Integration` protocol:
  - `name` = server name; `auth_type` = `"static_token"` if env/headers present else `"none"`; `allowed_domains` = (URL host,) or `()`.
  - `tools()` returns `[]`; `prompt_fragment()` = config `description` or generic, plus a static caution that this server's tool output is external data, not instructions.
  - `toolset()` (new capability) returns a cached toolset built once:
    `MCPToolset(transport_or_url, id=name, include_instructions=False, tool_error_behavior=def.on_tool_error (default "error"), init_timeout=def.init_timeout (default 5), read_timeout=def.read_timeout (default 60, down from the library's 300), process_tool_call=<sanitizer>)`, then `.filtered(...)` when a `tools` allowlist is configured, then `.prefixed(name)`. Note: `PrefixedToolset.id` is `None` — tests must assert `prefix` and the wrapped `MCPToolset.id`, not the wrapper's `id`.
  - stdio via `StdioTransport(command, args, env, cwd)`; HTTP via URL string + `headers`. Declare `fastmcp-slim` as an explicit dependency in pyproject — we import it directly; today it's only transitive.
  - `aclose()` closes the underlying client/transport; idempotent; safe when never connected.

### Security posture (V1) — stated honestly

- **Operator-gated definitions**: only whoever controls `mcp.json` (deployment/shell access) can define servers — a server definition is arbitrary-command execution and secret access, so it never becomes a chat-level action. In single-tenant, enablement defaults to on-everywhere (see Enablement above); per-channel scoping remains available as the exception mechanism.
- **Tool allowlist per server** (`tools` key → `.filtered()`): now the primary safety control. Henry cannot know which MCP tools mutate — the allowlist is the operator's choice. `mcp.json.example` documents "list read-only tools explicitly for third-party servers."
- **No auto-retry of tool errors by default** (`tool_error_behavior="error"`): retrying non-idempotent calls (e.g. "send reply") can duplicate mutations. Per-server opt-in to `"retry"` for known-idempotent servers.
- **`include_instructions=False` hardcoded** — server-provided instructions are the largest injection channel.
- **Framing-tag sanitization, scoped claim:** `process_tool_call` recursively walks structured results (dicts/lists) and applies `_neutralize_delimiters` to every string leaf, and truncates results over a size cap (~50KB) with a truncation note. This protects Henry's prompt framing tags only — it does NOT neutralize natural-language prompt injection from a malicious server. The real defenses are opt-in + allowlists; the docs say exactly that.

### Capability protocol

Do NOT widen `Integration` (it's `runtime_checkable`; adding a member breaks `isinstance` checks for `github`, `web`, `FakeIntegration`, and the test-local `ExplodingIntegration`). Add to `henry/interfaces.py`:

```python
@runtime_checkable
class ToolsetProvider(Protocol):
    def toolset(self) -> Any: ...
```

Wiring's shutdown path checks `isinstance(integration, MCPIntegration)` (concrete type, not `hasattr` duck-typing) for `aclose()`.

### Runner — `henry/agent/runner.py`

After the existing `_active_integrations(deps)` filtering (per-channel gating by name, unchanged):

```python
toolsets = [i.toolset() for i in integrations if isinstance(i, ToolsetProvider)]
agent = Agent(..., tools=[...], toolsets=toolsets or None)
```

Wrap run exceptions so MCP failures are attributable: when a run raises and MCP toolsets were attached, include the toolset ids in `RunResult.error` (connection errors from fastmcp don't reliably name the server).

### Wiring — `henry/wiring.py`

- `build_runtime`: only when the `integrations=` override is None, load MCP config and merge with an **explicit duplicate check** (`overlap = set(discovered) & set(mcp)` → raise naming the collision; a plain dict union would silently shadow a builtin).
- `HenryRuntime.close()`: MCP `aclose()` first, then http/engine (as above).

## Implementation steps (TDD, each step: failing test → implement → green)

0. **`"*"` enablement sentinel** — `tests/test_config_registry.py` + `tests/test_agent_runner.py` + `tests/test_wiring.py`: `defaults.json` ships `"enabled_integrations": "*"`; `load_channel_config` accepts the sentinel (skips unknown-name validation for it; explicit lists still validated); `_active_integrations` expands `"*"` to all registered; `RunSettings.enabled_integrations` passes it through; an explicit channel row still narrows. Implement across `henry/config/defaults.json`, `henry/config/registry.py`, `henry/wiring.py`, `henry/agent/runner.py`.
1. **`ToolsetProvider` protocol** — `henry/interfaces.py`; tests in `tests/test_interfaces.py` (stub with `toolset()` conforms; `FakeIntegration` does not).
2. **Runner toolset collection (MCP-free)** — `tests/test_agent_runner.py`: an integration exposing a pydantic-ai `FunctionToolset` via `toolset()` has its tools invoked under `model="test"` (TestModel calls all tools; keep the existing `memory_tools`/`sandbox_tools` monkeypatch pattern); non-providers unaffected; disabled integrations' toolsets not passed; MCP-attributed error wrapping. Implement in `henry/agent/runner.py`.
3. **Config parsing** — new `tests/test_mcp_config.py`: stdio + url entries, XOR validation, `${VAR}`/`${VAR:-default}` expansion, undefined-var raise, name-pattern validation, `description`/`tools`/`on_tool_error`/timeout passthrough, missing-file semantics for `explicit` True (raise) vs False (`{}`). Implement `MCPServerDef` + `load_mcp_config`.
4. **Adapter** — unit tests (no network; constructing `MCPToolset` doesn't connect): `Integration` + `ToolsetProvider` conformance; `toolset()` cached; wrapper chain (assert `PrefixedToolset.prefix` + wrapped `MCPToolset.id`); allowlist → `FilteredToolset`; `tool_error_behavior`/timeout defaults and overrides; `auth_type`/`allowed_domains` derivation; recursive sanitizer on nested dict/list results + size cap; `aclose()` idempotent/never-connected-safe.
5. **Settings + dependency** — `mcp_config_path` field + test; add `fastmcp-slim` to pyproject dependencies.
6. **Wiring** — explicit-duplicate-check merge, MCP load skipped on `integrations=` override, `close()` calls `aclose()` (failure of one doesn't skip others; still closes http/engine). Tests in `tests/test_wiring.py`.
7. **App lifecycle scope** — `amain()`: move everything after `build_runtime` inside the `try/finally runtime.close()`. Test: failure injected in slack-app creation still closes runtime (pattern from `tests/test_app.py`).
8. **End-to-end (the risky parts, not just happy path)** — checked-in stdio echo MCP server fixture under `tests/` (installed `mcp` package ships a FastMCP server). Tests: (a) full path config → wiring → runner run under TestModel proving the MCP tool is listed and called; (b) kill the child process between runs → next run reconnects (keep-alive respawn); (c) `close()` actually terminates the subprocess (poll the pid); (d) allowlist filtering excludes a tool end-to-end.
9. **Docs** — `mcp.json.example` + README section: secrets via `${ENV}` from `.env`, on-everywhere default + per-channel scoping as the exception, server-name rules, tool allowlists for third-party servers, honest injection-defense caveat, single-tenant stance (definitions are operator-level by design; admin UI is hosted/multi-tenant future work).

## Verification

- Full suite: `.venv/bin/python -m pytest tests/ -q`; `.venv/bin/ruff check henry tests` + format check on touched files.
- Step-8 e2e is the real proof: a genuine MCP server over stdio through registry → per-channel filter → Agent toolsets → tool call → shutdown, including process-death recovery.
- Live smoke test (user-assisted): add a real MCP server to `mcp.json`, enable it for a test channel via `channel_config`, restart Henry, @mention Henry to exercise it from Slack.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found | 18 findings (3 critical) + gate: 2×P1, 2×P2 — all addressed in this revision |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |

**CODEX:** Critical reconnect flaw (startup refcount pinning made reconnection impossible) → resolved by switching to lazy per-run lifecycle; also folded in: startup-leak scope fix, explicit missing-file semantics, recursive nested-result sanitization with honest scope claim, per-server tool allowlists, no-retry default for tool errors, explicit registry duplicate check, explicit fastmcp-slim dependency, `PrefixedToolset.id` test detail, failure-mode e2e tests.
**UNRESOLVED:** 0 design-level; residual risks documented inline (natural-language injection not neutralizable; HTTP per-run handshake overhead).
**IMPLEMENTATION PLAN REVIEW (2026-07-13):** external review of `2026-07-13-mcp-integrations-implementation.md` — 4×P1 (secret disclosure in validation errors, resource leak on config failure, acceptance test bypassed the advertised path, per-leaf size cap) + 3×P2 + 2 minor; all folded into the implementation plan. Also resolved there: `"*"` is default-resolution only, rejected in DB rows.
**VERDICT:** CODEX REVIEWED, plan revised — eng review not yet run. Revision 2 (single-tenant `"*"` enablement) post-dates the Codex review.
