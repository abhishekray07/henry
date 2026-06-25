from __future__ import annotations

import asyncio

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from henry.settings import Settings, get_settings
from henry.slack.app import create_slack_app
from henry.wiring import build_runtime


def validate_startup_settings(settings: Settings) -> None:
    missing = [
        name
        for name in ("slack_bot_token", "slack_app_token")
        if not str(getattr(settings, name)).strip()
    ]
    if missing:
        env_names = ", ".join(f"HENRY_{name.upper()}" for name in missing)
        raise RuntimeError(f"Missing required Slack setting(s): {env_names}")


async def amain() -> None:
    settings = get_settings()
    validate_startup_settings(settings)
    runtime = build_runtime(settings)
    app = create_slack_app(
        bot_token=settings.slack_bot_token,
        orchestrator=runtime.handle_event,
        deduper=runtime.deduper,
    )
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    try:
        await handler.start_async()
    finally:
        await runtime.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
