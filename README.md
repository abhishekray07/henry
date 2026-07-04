# Henry

> **"Henry" is a placeholder name** — it lives in one place (`henry/branding.py`) so it can be renamed in a single edit.

Open-source, self-hosted, **model-agnostic** AI teammate for Slack. You `@mention` it in a channel and it does real work in the thread where the whole team can watch; it remembers each channel over time; and (later phases) it acts proactively and runs long jobs — all on infrastructure **you** control, with **any** LLM.

## Why it's different
- **Self-hosted** — your Slack data never leaves machines you control.
- **Any model** — bring your own LLM (Claude, GPT, Gemini, local…), swap anytime.
- **Open & auditable** — AGPL-3.0; you can read and scope everything.

## Status
Henry V1 is in integration. The runtime wiring, Postgres-backed memory, Slack
orchestrator, built-in integrations, and Docker sandbox are present; the
remaining release gate is the real Slack smoke test described in the
implementation plan.
- [`docs/plans/2026-06-23-henry-design.md`](docs/plans/2026-06-23-henry-design.md) — design spec
- [`docs/plans/2026-06-23-henry-v1-implementation.md`](docs/plans/2026-06-23-henry-v1-implementation.md) — implementation plan (parallel worktrees)
- [`design-overview.html`](design-overview.html) — plain-English overview

## Quickstart

Prerequisites:
- Python 3.12
- Docker Desktop or another Docker daemon
- `uv`
- Slack app credentials with Socket Mode enabled

Install dependencies:

```bash
uv sync --extra dev
```

Create a local env file:

```bash
cp .env.example .env
```

Fill in at least:
- `HENRY_SLACK_BOT_TOKEN` (`xoxb-...`)
- `HENRY_SLACK_APP_TOKEN` (`xapp-...`)
- a model provider credential for `HENRY_DEFAULT_MODEL`
- `HENRY_GITHUB_TOKEN` if the GitHub integration should be available
- `HENRY_WEB_SEARCH_API_KEY` if web search should be available

Start Postgres and apply migrations:

```bash
docker compose up -d db
uv run alembic upgrade head
```

If host port `5432` is already in use, choose another host port and set both
`HENRY_DB_PORT` and `HENRY_DATABASE_URL` in your `.env` so every command —
compose, migrations, and the bot — agrees on the port:

```bash
# in .env
HENRY_DB_PORT=55432
HENRY_DATABASE_URL=postgresql+asyncpg://henry:henry@localhost:55432/henry
```

Then bring up Postgres and apply migrations as above:

```bash
docker compose up -d db
uv run alembic upgrade head
```

Run the Slack bot:

```bash
uv run python -m henry.app
```

The process stays in the foreground while the Socket Mode connection is active.
Stop it with `Ctrl-C`.

Useful checks:

```bash
uv run ruff check .
uv run pytest -q
```

## License
AGPL-3.0-or-later. Contributions require signing the CLA ([`CLA.md`](CLA.md)).
