"""Live smoke check: verify the running Henry bot handles a real Slack mention.

The one step automation can't do is send the mention — Slack never delivers a
bot its own messages, so a human (or a second app) has to trigger the event.
This script does everything else: it watches audit_log for the next run and
reports its outcome.

Usage:
  1. Start Henry (`uv run henry` or `uv run python -m henry.app`) and keep it running.
  2. Run: uv run python scripts/smoke.py
  3. When prompted, mention @Henry in a test channel.

Exit codes: 0 = run succeeded, 1 = run failed (error printed), 2 = timed out.
"""

from __future__ import annotations

import asyncio
import sys
import time

from dotenv import load_dotenv
from sqlalchemy import text

from henry.db.session import make_engine
from henry.settings import get_settings

TIMEOUT_SECONDS = 180.0
POLL_SECONDS = 2.0


async def main() -> int:
    load_dotenv(".env")
    engine = make_engine(get_settings())
    try:
        async with engine.connect() as conn:
            last_seen = (await conn.execute(text("SELECT coalesce(max(id), 0) FROM audit_log"))).scalar()

        print(f"Watching audit_log for runs newer than id={last_seen}.")
        print("Now mention @Henry in your test Slack channel (waiting up to 3 minutes)...")

        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT id, channel_id, status, error, latency_ms, cost_usd "
                            "FROM audit_log WHERE id > :last ORDER BY id"
                        ),
                        {"last": last_seen},
                    )
                ).all()
            for run_id, channel_id, status, error, latency_ms, cost_usd in rows:
                print(f"run id={run_id} channel={channel_id} status={status} latency={latency_ms}ms cost={cost_usd}")
                if status == "ok":
                    print("SMOKE PASS")
                    return 0
                print(f"SMOKE FAIL — error: {error}")
                return 1
            await asyncio.sleep(POLL_SECONDS)

        print(f"SMOKE TIMEOUT — no run observed within {TIMEOUT_SECONDS:.0f}s. Is the bot running?")
        return 2
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
