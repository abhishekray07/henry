from __future__ import annotations

from typing import Any

import httpx
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


def build_model(model_str: str, settings: Any, http_client: httpx.AsyncClient | None = None) -> Model:
    if getattr(settings, "litellm_base_url", ""):
        provider = OpenAIProvider(
            base_url=settings.litellm_base_url,
            api_key=getattr(settings, "litellm_api_key", "unused") or "unused",
            http_client=http_client,
        )
        return OpenAIChatModel(model_str, provider=provider)

    return infer_model(model_str)
