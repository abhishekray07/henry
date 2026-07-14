from __future__ import annotations

import asyncio

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from henry.settings import Settings, get_settings
from henry.slack.app import create_slack_app
from henry.slack.context import make_transcript_fetcher
from henry.wiring import build_runtime


def validate_startup_settings(settings: Settings) -> None:
    missing = [
        name for name in ("slack_bot_token", "slack_app_token") if not str(getattr(settings, name)).strip()
    ]
    if missing:
        env_names = ", ".join(f"HENRY_{name.upper()}" for name in missing)
        raise RuntimeError(f"Missing required Slack setting(s): {env_names}")


async def amain() -> None:
    settings = get_settings()
    validate_startup_settings(settings)
    runtime = build_runtime(settings)
    try:
        app = create_slack_app(
            bot_token=settings.slack_bot_token,
            orchestrator=runtime.handle_event,
            deduper=runtime.deduper,
        )
        auth = await app.client.auth_test()
        runtime.transcript_fetcher = make_transcript_fetcher(
            app.client,
            bot_user_id=str(auth.get("user_id") or "") or None,
        )
        handler = AsyncSocketModeHandler(app, settings.slack_app_token)
        await handler.start_async()
    finally:
        await runtime.close()


def main() -> None:
    # Provider SDKs (e.g. pydantic-ai's Anthropic provider) read API keys from
    # os.environ, which pydantic-settings does not populate from .env.
    # cwd-relative, matching Settings' env_file=".env".
    load_dotenv(".env")
    asyncio.run(amain())


if __name__ == "__main__":
    main()
