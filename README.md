# Henry

> **"Henry" is a placeholder name** — it lives in one place (`henry/branding.py`) so it can be renamed in a single edit.

Open-source, self-hosted, **model-agnostic** AI teammate for Slack. You `@mention` it in a channel and it does real work in the thread where the whole team can watch; it remembers each channel over time; and (later phases) it acts proactively and runs long jobs — all on infrastructure **you** control, with **any** LLM.

## Why it's different
- **Self-hosted** — your Slack data never leaves machines you control.
- **Any model** — bring your own LLM (Claude, GPT, Gemini, local…), swap anytime.
- **Open & auditable** — AGPL-3.0; you can read and scope everything.

## Status
Design + plan complete; implementation starting.
- [`docs/plans/2026-06-23-henry-design.md`](docs/plans/2026-06-23-henry-design.md) — design spec
- [`docs/plans/2026-06-23-henry-v1-implementation.md`](docs/plans/2026-06-23-henry-v1-implementation.md) — implementation plan (parallel worktrees)
- [`design-overview.html`](design-overview.html) — plain-English overview

## Quickstart (once built)
```bash
docker compose up -d db
alembic upgrade head
python -m henry.app
```

## License
AGPL-3.0-or-later. Contributions require signing the CLA ([`CLA.md`](CLA.md)).
