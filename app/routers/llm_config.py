"""Per-user LLM provider settings: which provider/key/models to score with.

The dashboard's settings page reads the effective config + provider list here and
writes the user's choices back. The matcher resolves the same rows at scan time
(see services/llm.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..llm_providers import PROVIDERS, PROVIDERS_BY_KEY
from ..models import LlmConfig, User
from ..schemas import LlmConfigIn, LlmConfigOut, LlmModelTest, LlmProviderOut, LlmTestResult
from ..services import llm
from ..services.ollama_client import OllamaBudgetError, OllamaClient, OllamaError

router = APIRouter(prefix="/api/llm-config", tags=["llm-config"])


def _provider_options() -> list[LlmProviderOut]:
    return [LlmProviderOut(key=p.key, label=p.label, base_url=p.base_url) for p in PROVIDERS]


def _current(db: Session, user: User) -> LlmConfigOut:
    eff = llm.effective_config(db, user)
    return LlmConfigOut(
        provider=eff.provider,
        base_url=eff.base_url,
        main_model=eff.main_model,
        light_model=eff.light_model,
        has_api_key=bool(eff.api_key),
        providers=_provider_options(),
    )


@router.get("", response_model=LlmConfigOut)
def get_llm_config(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The user's effective config (their settings, else deployment defaults) plus
    the providers to choose from. Pre-fills the settings form."""
    return _current(db, user)


@router.put("", response_model=LlmConfigOut)
def update_llm_config(
    payload: LlmConfigIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the user's provider/models (upsert). ``api_key`` is optional: a
    non-empty value replaces the stored key; omitting it keeps the existing one."""
    if payload.provider not in PROVIDERS_BY_KEY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown provider: {payload.provider}")

    cfg = db.scalar(select(LlmConfig).where(LlmConfig.user_id == user.id))
    if cfg is None:
        cfg = LlmConfig(user_id=user.id)
        db.add(cfg)
    cfg.provider = payload.provider
    cfg.main_model = payload.main_model
    cfg.light_model = payload.light_model
    if payload.api_key is not None:  # absent/blank -> keep the existing key
        cfg.api_key = payload.api_key
    db.commit()
    return _current(db, user)


def _probe_model(provider, api_key: str, role: str, model: str) -> LlmModelTest:
    """One tiny real request that exercises the provider/key for a single model."""
    client = OllamaClient(base_url=provider.base_url, api_key=api_key, model=model)
    try:
        reply = client.chat_text("You are a connectivity check.", "Reply with the word OK.")
    except OllamaBudgetError as exc:
        return LlmModelTest(role=role, model=model, ok=False,
                            detail=f"quota/budget exhausted ({exc})")
    except OllamaError as exc:
        return LlmModelTest(role=role, model=model, ok=False, detail=str(exc))
    if reply and reply.strip():
        return LlmModelTest(role=role, model=model, ok=True, detail="responded")
    return LlmModelTest(role=role, model=model, ok=False, detail="empty response")


def _summarize(provider_label: str, results: list[LlmModelTest]) -> str:
    if all(r.ok for r in results):
        models = " and ".join(f"{r.role} model “{r.model}”" for r in results)
        return f"Success — {provider_label} responded for the {models}."
    return "; ".join(
        f"{r.role} model “{r.model}”: " + ("ok" if r.ok else f"failed — {r.detail}")
        for r in results
    )


@router.post("/test", response_model=LlmTestResult)
def test_llm_config(
    payload: LlmConfigIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Confirm the user's LLM setup works by making one tiny real request per model
    — both the main (scoring) and light (relevance-filter) models, deduped when
    they're the same. The API key falls back to the saved one when the field is
    blank, so the user can test without re-typing it. Never raises on an LLM
    failure — it reports each model's outcome so the UI can show it."""
    provider = PROVIDERS_BY_KEY.get(payload.provider)
    if provider is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown provider: {payload.provider}")

    saved = db.scalar(select(LlmConfig).where(LlmConfig.user_id == user.id))
    api_key = payload.api_key or (saved.api_key if saved else None)
    if not api_key:
        return LlmTestResult(ok=False, detail="No API key set — enter your provider API key and test again.")

    # Probe the main and light models; skip the duplicate when they're identical.
    to_probe = [("main", payload.main_model)]
    if payload.light_model != payload.main_model:
        to_probe.append(("light", payload.light_model))

    results = [_probe_model(provider, api_key, role, model) for role, model in to_probe]
    ok = all(r.ok for r in results)
    return LlmTestResult(ok=ok, detail=_summarize(provider.label, results), results=results)
