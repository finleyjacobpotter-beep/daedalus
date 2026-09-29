"""Model selection: ``provider:model`` strings mapped onto tau providers."""

from __future__ import annotations

import os
from dataclasses import replace

from tau_agent.provider import ModelProvider
from tau_ai import (
    AnthropicConfig,
    AnthropicProvider,
    OpenAICompatibleProvider,
    openai_compatible_config_from_env,
)

DEFAULT_MODEL = "anthropic:claude-opus-5-5"
PROVIDERS = ("anthropic", "openai")


class ProviderError(RuntimeError):
    pass


def split_model(spec: str) -> tuple[str, str]:
    provider, sep, model = spec.partition(":")
    if not sep:
        provider, model = "anthropic", spec
    if provider not in PROVIDERS or not model:
        raise ProviderError(
            f"bad model {spec!r}: use provider:model with provider one of {', '.join(PROVIDERS)}"
        )
    return provider, model


def build_provider(spec: str, *, max_tokens: int | None = None) -> tuple[ModelProvider, str]:
    """Return (provider, model name) for a ``provider:model`` spec.

    anthropic  ANTHROPIC_API_KEY, optional ANTHROPIC_BASE_URL
    openai     OPENAI_API_KEY, optional OPENAI_BASE_URL (any OpenAI-compatible endpoint)
    """
    provider, model = split_model(spec)
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        config = AnthropicConfig(api_key=key, base_url=base, max_tokens=max_tokens)
        return AnthropicProvider(config), model
    try:
        config = openai_compatible_config_from_env()
    except RuntimeError as exc:
        raise ProviderError(str(exc)) from None
    if max_tokens:
        config = replace(config, max_tokens=max_tokens)
    return OpenAICompatibleProvider(config), model
