from __future__ import annotations

import asyncio

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from henry.settings import get_settings
from henry.slack.app import create_slack_app
from henry.wiring import build_runtime


async def amain() -> None:
    settings = get_settings()
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
