from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HENRY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://henry:henry@localhost:5432/henry"
    slack_bot_token: str = ""
    slack_app_token: str = ""
    default_model: str = "anthropic:claude-sonnet-4-6"
    github_token: str = ""
    web_search_provider: str = "tavily"
    web_search_api_key: str = ""
    litellm_base_url: str = ""
    max_run_usd: float = Field(default=1.00, ge=0)
    sandbox_image: str = "henry-sandbox:base"


@lru_cache
def get_settings() -> Settings:
    return Settings()
