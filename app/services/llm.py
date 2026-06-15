"""Resolve a user's effective LLM configuration and build clients from it.

A user's ``LlmConfig`` (provider + API key + model names) overrides the
deployment-wide ``settings`` defaults. Anything the user left unset falls back to
those defaults, so a single-tenant deployment that only configured the global
``JOBSCOUT_OLLAMA_*`` env vars keeps working unchanged. The provider key maps to
a base URL via ``llm_providers``; the user never types a URL."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..llm_providers import DEFAULT_PROVIDER, PROVIDERS_BY_KEY
from ..models import LlmConfig, User
from .ollama_client import OllamaClient


@dataclass(frozen=True)
class EffectiveLlmConfig:
    provider: str
    base_url: str
    api_key: str | None
    main_model: str
    light_model: str


def _provider(key: str | None):
    return PROVIDERS_BY_KEY.get(key or "", PROVIDERS_BY_KEY[DEFAULT_PROVIDER])


def effective_config(db: Session, user: User) -> EffectiveLlmConfig:
    """The fully-resolved config used to talk to the LLM for ``user``: their saved
    LlmConfig where set, else the chosen provider's defaults. With no saved config
    the user has no API key, so scoring surfaces a clear "configure your LLM
    provider" error rather than silently using someone else's credentials."""
    cfg = db.scalar(select(LlmConfig).where(LlmConfig.user_id == user.id))
    provider = _provider(cfg.provider if cfg else None)
    return EffectiveLlmConfig(
        provider=provider.key,
        base_url=provider.base_url,
        api_key=(cfg.api_key if cfg else None) or None,
        main_model=(cfg.main_model if cfg else None) or provider.default_main_model,
        light_model=(cfg.light_model if cfg else None) or provider.default_light_model,
    )


def clients_for_user(db: Session, user: User) -> tuple[OllamaClient, OllamaClient]:
    """Build the (scoring, relevance-filter) clients for ``user`` from their
    effective config."""
    eff = effective_config(db, user)
    score = OllamaClient(base_url=eff.base_url, api_key=eff.api_key, model=eff.main_model)
    light = OllamaClient(base_url=eff.base_url, api_key=eff.api_key, model=eff.light_model)
    return score, light
