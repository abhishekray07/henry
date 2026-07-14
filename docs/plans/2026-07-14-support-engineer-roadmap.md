# Henry — Support-Engineer Roadmap

**Date:** 2026-07-14
**Status:** Design / roadmap (not yet an implementation plan)
**Supersedes framing in:** `2026-06-23-henry-design.md` (roadmap phases), by promoting the
deferred sandbox-egress and execution work to the front.

---

## 1. The bet

Every AI support product on the market (Intercom Fin, Decagon, Sierra, Pylon) competes on
the *easy* half of the job: deflecting simple questions and drafting replies. **Almost none
of them reproduce a real bug against real production data and show a reproducible proof.**

That is Henry's wedge, and it happens to be the expensive half of the founder's own time:
the **20–30% of tickets that are genuine bugs**. Own that. For the other 50–60% (answerable
questions), Henry drafts a reply and moves on — it does not try to out-Fin Fin.

**One-line goal:** turn a vague customer report into a root cause *verified against
production data*, shown as a re-runnable notebook, plus a drafted reply, a filed issue with
proof, and a draft fix PR.

The target loop (this is a support engineer's actual job, ranked by pain):

```
triage → reproduce → diagnose against real data → escalate or fix → reply → document
```

Henry works in **two investigation modes**, both sharing the same sandbox + notebook:
- **Diagnose** — reproduce a bug against real data (needs read-only prod DB).
- **Demonstrate** — when no doc exists, drive the product in a browser, screenshot the steps,
  and answer visually (needs a browser, no DB). This is what a support engineer does when the
  answer isn't written down anywhere: go into the product and figure it out.

## 2. Decisions locked (this session)

| Area | Decision |
|---|---|
| Capability scope | Full support-engineer toolkit (prod data, Jira, code, session replay). Not precious about which vendor — wire what's high-leverage. |
| Where issues arrive | Slack + Help Scout / email. |
| Help Scout | **Read-only ingest** — pull tickets in to work on. Henry never writes back to Help Scout. |
| Who Henry faces | **Slack-only, internal copilot.** No customer Slack Connect channel exists. Henry drafts + posts replies in *your* Slack; you relay to the customer. |
| Bug resolution depth | **Diagnose + escalation packet + draft fix PR.** Root cause + verifying notebook + filed Jira/GitHub issue, plus a draft PR with a proposed fix for review. |
| Notebook | **Both, staged.** Capture every step during the run; render a hosted page; post a link + short summary in the Slack thread. |
| Sandbox engine | **Adopt support-console's live IPython kernel** as Henry's execution engine (stateful cells, rich output, notebook is a free byproduct). Replaces today's stateless `run_bash`. |
| Prod data path | **Load app context at startup** — boot the kernel with the Flask app + `db` + models, using a **read-only** DB credential. Agent runs live `db.session` queries. |
| Browser (visual answers) | Headless browser in the sandbox logs into the **live production app** (least-privilege support account); screenshots go into the notebook + Slack reply. |
| Browser web scope | Scoped to the product + `*.atlassian.net` + Atlassian help/community. **No open web.** |
| Deploy target | **Kubernetes now.** |

## 3. Why the sandbox gets network (and how it stays safe)

Today Henry's Docker sandbox is hard-coded to `network="none"` (`henry/sandbox/docker.py`
raises if not). That was a conservative default for a threat model of "untrusted code might
leak secrets." It makes live prod-DB debugging awkward (data must be bridged in host-side).

The real risk of an open-network sandbox is **prompt injection** — a customer ticket, a Help
Scout thread, or a malicious MCP server telling the agent to exfiltrate data. The fix is not
"no network." It is two rules:

1. **Egress allowlist.** The sandbox may reach *only* named hosts — your prod DB, the
   Anthropic API, PyPI/npm. Everything else blocked. An injected agent cannot reach
   `evil.com`.
2. **No write credentials in the sandbox.** The sandbox reads and computes. The tokens that
   post to Slack, open a PR, or file a Jira ticket live only in the **host orchestrator**,
   behind the approval gate.

**Trust boundary:** *the sandbox investigates; the host acts.* This is the load-bearing
security decision of the whole roadmap.

Additional controls:
- **Read-only enforced at the database** (read replica or restricted role), not by convention.
- **Least-privilege, short-lived DB credential** injected at pod launch — never baked into
  the image or a plaintext env var the model can dump.

### Two sandbox profiles — never prod-data *and* broad reach in one box

Adding a browser widens the egress surface, so the two modes run as **separate sandbox
profiles**. Neither profile ever holds both a prod-data credential and a wide egress path:

| Profile | Has | Egress allowlist | Does NOT have |
|---|---|---|---|
| **Diagnose** | read-only prod DB cred, app context | DB + Anthropic + PyPI | browser, general web |
| **Demonstrate** | headless browser, prod support-account login | product + `*.atlassian.net` + Atlassian help | direct DB cred |

The browser points at the **live production app**, so screenshots may contain real customer
data. That is acceptable inside the internal Slack workspace (the team already sees it), but
is a **flag before any outward sharing**. The tight product-only egress means even the
browser profile can't exfiltrate to arbitrary hosts.

## 4. Target architecture

```
Slack (@mention)                          ── Henry orchestrator (host pod)
  │                                           • holds Slack + GitHub + Jira WRITE creds
  ▼                                           • approval gate; posts results; audit_log
Henry orchestrator ──spawns──► Sandbox Pod (per investigation)
                                 • gVisor RuntimeClass  (isolation boundary)
                                 • IPython kernel + Flask app context
                                 • READ-ONLY prod DB cred injected at launch
                                 • Cilium FQDN egress: DB + Anthropic + PyPI only
                                 • cell history = the notebook
                                 ▼
                        Hosted notebook page  ──link──►  Slack thread
```

### The self-hosted k8s sandbox stack (2026)

There is no single "k8s sandbox button," but there is a native stack:

- **Isolation runtime (RuntimeClass):** **gVisor** (`runsc`) — user-space kernel, software-only,
  ~10–30% overhead. The pragmatic default. **Kata Containers** (VM-per-pod) is stronger but
  needs bare-metal nodes; reserve for a hostile-code threat model, offered as a second
  RuntimeClass.
- **Kernel-pod orchestration:** **`kubernetes-sigs/agent-sandbox`** — official k8s SIG project
  (Apache-2.0), purpose-built for agents: `Sandbox` CRD = stateful pod + stable identity +
  persistent storage + `runtimeClassName` (gVisor first-class) + warm pools (sub-second cold
  start). **Pre-1.0** as of early 2026. Proven fallback: **Jupyter Enterprise Gateway**
  (pod-per-kernel, session affinity, cleanup).
- **Egress allowlist:** **Cilium** CNI with FQDN policies (`toFQDNs: api.anthropic.com`). Plain
  NetworkPolicy can't do hostnames. Gotcha: you must include an explicit DNS allow rule or
  every FQDN rule silently no-ops.

**Reference implementations already built (reuse, don't rebuild):**
- `support-console/support_console/kernel.py` — IPython kernel manager (jupyter_client).
- `support-console/support_console/startup_template.py` — loads Flask app + `db` + models.
- `support-console/support_console/static/` — working notebook web UI (chat + cells).

### Known gotchas (from research)
- gVisor's user-space **network stack is its weakest path** — fine for SQL queries, watch it on
  huge result sets.
- **Cold start** is seconds; CNI/network setup dominates under concurrency → use a warm pool.
- **gVisor + `kubectl port-forward` doesn't work** — connect kernels via a Service (agent-sandbox
  ships a "Sandbox Router"), not port-forward.
- A niche C-extension hitting an unimplemented syscall is your escape hatch to Kata for that job.

## 5. Current state vs. what's needed

| Capability | Today | Gap |
|---|---|---|
| Code execution | Docker sandbox, `run_bash` (stateless), `network=none` | Swap to stateful kernel; add allowlisted network |
| Prod data | None (must bridge host-side) | Read-only DB cred + app context at startup |
| Browser / visual answers | None | Headless browser (Playwright) in a separate sandbox profile; screenshots → notebook |
| Notebook / show-work | None (Slack text only) | Cell capture → hosted page + Slack link |
| Help Scout | MCP example only, no code | Read-only ingest tool/MCP |
| Jira | None (product *is* a Jira app) | Read + file-issue integration |
| GitHub | `search_code`, `get_file`, `open_pr`, `create_issue` ✓ | Reused for escalation packet + draft PR |
| Deploy | Docker-out-of-Docker + compose Postgres | k8s: Cilium, gVisor, per-session Pods, Helm, secrets |
| Memory | Per-channel, naive term-frequency recall | Semantic recall over past investigations (later) |
| Session replay / analytics | None (PostHog MCP available) | Ingest for "anchor to identity → session" (later) |

## 6. Roadmap phases

Ordered by the founder's stated priorities: sandbox + show-your-work + k8s come first.

### Phase 0 — Execution engine + k8s foundation *(the big infra swap)*
Replace the network-less Docker sandbox with a **per-investigation kernel pod**.
- Adopt support-console's IPython kernel as the execution engine.
- Run it as a k8s `Sandbox` (agent-sandbox CRD; JEG fallback) under **gVisor**.
- **Cilium FQDN egress allowlist**; **read-only DB cred** injected at launch; **app context**
  loaded at startup.
- Move write creds out of the sandbox into the host orchestrator.
- Helm chart; Henry runs as a Deployment (Slack Socket Mode = no ingress needed).
- **Done = ** `@Henry` runs a live `db.session` query against a read replica from inside a
  gVisor pod that can reach *only* the DB + Anthropic.

### Phase 1 — The notebook ("show your work")
- Capture each kernel step (code + output + narrative) as ordered cells.
- Render a hosted notebook page; post **link + short summary** in the Slack thread.
- Sessions persisted (rehydrate/replay later).
- **Done = ** an investigation produces a shareable page proving the root cause.

### Phase 2 — Visual answers (browser)
The "demonstrate" mode: answer undocumented how-to questions by driving the product.
- **Headless browser (Playwright)** in a **separate sandbox profile** — browser + prod
  support-account login, **no DB cred**, egress scoped to product + `*.atlassian.net` +
  Atlassian help/community.
- Screenshots captured as notebook image cells and posted in the Slack reply.
- Customer-data flag: prod screenshots stay in internal Slack; warn before outward sharing.
- **Done = ** `@Henry, how do I set up an asset schema?` → it logs into the product, clicks
  through, and replies with annotated screenshots.

### Phase 3 — Feed it & voice it
- **Help Scout read-only ingest**: pull a ticket by URL/id into a thread to work on.
- **Jira**: read related issues + file an issue (apt — the product is a Jira app).
- Register-aware **reply drafting** in Slack (plain for admins, precise for devs).

### Phase 4 — Close the bug loop
- **Escalation packet**: structured Jira/GitHub issue with repro + verifying query + notebook link.
- **Draft fix PR**: clone repo (already supported), reproduce, propose a fix, open a *draft* PR
  linked to the packet. (Uses existing `open_pr`.)

### Phase 5 — Get sharper over time
- **Session replay / PostHog** ingest: anchor to identity → session → correlate to backend.
- **Semantic memory** of past investigations: recognize repeat bugs ("we've seen this — here's
  the prior fix"), turning history into deflection.

## 7. Explicitly out of scope (YAGNI)

- Ambient / proactive mode — Henry stays mention-gated.
- Approval UI beyond a simple Slack confirm — internal-copilot means a human sends.
- Help Scout write access — read-only ingest only.
- Multi-tenant control plane — single-tenant, operator-configured.

## 8. Open risks / decisions to revisit

- **agent-sandbox is pre-1.0.** Going "k8s now" on it is bleeding-edge; JEG is the proven
  fallback for the kernel-pod layer. Cilium + gVisor themselves are mature.
- **App-context coupling.** Loading the full Flask app into the sandbox couples the sandbox
  image to prod app code + a read-only DB. Powerful, but a build/versioning dependency to manage.
- **Notebook hosting.** Where does the page live — served by Henry, or pushed to object storage?
  (Decide in Phase 1.)
- **gVisor network overhead** on large result sets — cap query result sizes.

## 9. Tracking (GitHub epics + issues)

Published to `abhishekray07/henry`. Each epic is an issue labelled `epic` with its children as
native sub-issues; children carry `needs-triage` + `AFK`/`HITL`.

| Phase | Epic | Child issues |
|---|---|---|
| 0 — Execution engine + k8s | [#8](https://github.com/abhishekray07/henry/issues/8) | #18 kernel swap · #19 gVisor/Cilium · #20 pod lifecycle · #21 Helm |
| 1 — Notebook | [#9](https://github.com/abhishekray07/henry/issues/9) | #22 cell contract · #23 hosted page + Slack link |
| 2 — Visual answers (browser) | [#11](https://github.com/abhishekray07/henry/issues/11) | #26 browser profile · #27 screenshots → notebook |
| 3 — Feed it & voice it | [#12](https://github.com/abhishekray07/henry/issues/12) | #15 Help Scout · #16 Jira · #17 reply drafting |
| 4 — Diagnose (prod data) | [#10](https://github.com/abhishekray07/henry/issues/10) | #24 read-only DB + profile · #25 app-context startup |
| 4 — Close the bug loop | [#13](https://github.com/abhishekray07/henry/issues/13) | #28 escalation packet · #29 draft fix PR |
| 5 — Get sharper | [#14](https://github.com/abhishekray07/henry/issues/14) | #30 session replay/PostHog · #31 semantic memory |

**Day-one parallel tracks:** platform (#18) and integrations (#15, #16, #17) — no shared blockers.

## 10. References

- Current Henry map: `henry/` (kernel-less Docker sandbox, MCP, per-channel memory).
- Origin bot: `~/Projects/opslane/support-engineer` — capability-starved, read-only APIs, no
  sandbox; the `investigate-ticket` skill encodes the 6-phase debug loop (its Flask-shell
  step is aspirational text the old harness can't run).
- Notebook reference: `github.com/abhishekray07/support-console` — live IPython kernel + Flask
  app context + notebook UI.
- Investigation loop: `~/.claude/skills/investigate-ticket/SKILL.md`.
