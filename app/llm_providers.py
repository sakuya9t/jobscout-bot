"""Built-in LLM providers a user can pick in settings.

Each provider is just a label + the base URL its API lives at; the user supplies
their own API key and model names. Today only Ollama Cloud is offered, but the
client speaks the Ollama ``/api/chat`` protocol, so any Ollama-compatible host
(e.g. a self-hosted server) is a one-line addition here — no other code changes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LlmProvider:
    key: str  # stable slug stored on LlmConfig / sent by the API
    label: str  # shown in the settings dropdown
    base_url: str  # API root the OllamaClient talks to
    # Sensible model defaults to pre-fill the settings form and to fall back to
    # when a user hasn't picked their own (a strong cheap "light" model triages
    # relevance; the "main" model does the expensive resume<->role scoring).
    default_main_model: str
    default_light_model: str


PROVIDERS: list[LlmProvider] = [
    LlmProvider(
        key="ollama_cloud",
        label="Ollama Cloud",
        base_url="https://ollama.com",
        default_main_model="gpt-oss:120b-cloud",
        default_light_model="deepseek-v4-flash",
    ),
]

PROVIDERS_BY_KEY: dict[str, LlmProvider] = {p.key: p for p in PROVIDERS}

DEFAULT_PROVIDER = PROVIDERS[0].key
DEFAULT_PROVIDER_OBJ = PROVIDERS_BY_KEY[DEFAULT_PROVIDER]
