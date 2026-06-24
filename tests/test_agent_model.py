from __future__ import annotations

import httpx
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel

from henry.agent.model import build_model
from henry.settings import Settings


async def test_build_model_infers_native_model_without_network() -> None:
    settings = Settings(default_model="test")

    model = build_model("test", settings)

    assert isinstance(model, TestModel)


async def test_build_model_uses_litellm_openai_compatible_provider() -> None:
    settings = Settings(default_model="openai:gpt-4o-mini", litellm_base_url="http://localhost:4000/v1")

    async with httpx.AsyncClient() as http:
        model = build_model("gpt-4o-mini", settings, http)

    assert isinstance(model, OpenAIChatModel)
