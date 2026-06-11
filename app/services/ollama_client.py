"""Thin client for the Ollama /api/chat endpoint.

Works against both Ollama Cloud (https://ollama.com, bearer key) and a local
server (http://localhost:11434, no key) — the only difference is the base URL
and whether we send an Authorization header. We use Ollama's *structured
outputs* feature (``format`` = a JSON schema) so the model returns parseable
JSON instead of prose."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from ..config import settings
from ..logging_config import OLLAMA_LOGGER, get_logger

log = get_logger(__name__)
# Every Ollama request/response is logged here (one correlated pair per call),
# gated by settings.log_ollama. See app/logging_config.py.
wire = get_logger(OLLAMA_LOGGER)


class OllamaError(RuntimeError):
    pass


class OllamaBudgetError(OllamaError):
    """Ollama rejected the call because the account's budget / quota / credit is
    exhausted (HTTP 402, or a 403/429 whose body names a usage cap rather than a
    short-lived rate limit).

    Distinct from a generic OllamaError so the matcher can (a) tell the user their
    Ollama account is out of quota in plain language and (b) NOT write permanent
    error-markers — an exhausted budget is recoverable, so those postings must
    re-score automatically once quota returns, not be skipped until a manual
    --retry-failed."""

    pass


# Body substrings that point to a hard usage/credit cap (vs a transient rate
# limit). Matched case-insensitively against the provider's error body.
_BUDGET_HINTS = (
    "quota", "credit", "billing", "insufficient", "payment required",
    "exceeded", "usage limit", "out of", "upgrade your plan", "spending limit",
)


def _looks_like_budget(status_code: int, body: str | None) -> bool:
    """Heuristic: is this HTTP error an exhausted-budget rejection? 402 always is;
    a 403/429 only when its body names a cap (a bare 429 is a transient rate limit
    that should still be retried). Ollama Cloud's exact codes aren't contractual,
    so we match defensively on both status and body."""
    if status_code == 402:  # Payment Required — unambiguously a budget rejection
        return True
    if status_code in (403, 429):
        low = (body or "").lower()
        return any(hint in low for hint in _BUDGET_HINTS)
    return False


def _clip(text: str) -> str:
    """Truncate logged prompt/response content to keep the log bounded; 0 = full."""
    limit = settings.log_ollama_max_chars
    if limit and len(text) > limit:
        return f"{text[:limit]}… [+{len(text) - limit} chars]"
    return text


def _log_request(url: str, body: dict) -> str:
    """Log an outgoing chat request and return a short correlation id that ties it
    to its response/failure line. Never logs the Authorization header/API key."""
    xid = uuid.uuid4().hex[:8]
    if not settings.log_ollama:
        return xid
    msgs = body.get("messages", [])
    wire.info(
        "[%s] → POST %s model=%s temp=%s format=%s messages=%d prompt_chars=%d",
        xid, url, body.get("model"),
        (body.get("options") or {}).get("temperature"),
        "json-schema" if body.get("format") else "text",
        len(msgs), sum(len(m.get("content") or "") for m in msgs),
    )
    for m in msgs:
        wire.info("[%s]   %s: %s", xid, m.get("role"), _clip(m.get("content") or ""))
    return xid


def _log_response(xid: str, started: float, payload: dict) -> None:
    if not settings.log_ollama:
        return
    elapsed_ms = (time.perf_counter() - started) * 1000
    content = (payload.get("message") or {}).get("content") or ""
    wire.info(
        "[%s] ← %.0fms done_reason=%s prompt_tokens=%s eval_tokens=%s resp_chars=%d",
        xid, elapsed_ms, payload.get("done_reason"),
        payload.get("prompt_eval_count"), payload.get("eval_count"), len(content),
    )
    wire.info("[%s]   assistant: %s", xid, _clip(content))


def _log_failure(xid: str, started: float, detail: str) -> None:
    if not settings.log_ollama:
        return
    elapsed_ms = (time.perf_counter() - started) * 1000
    wire.warning("[%s] ✗ %.0fms %s", xid, elapsed_ms, detail)


def _is_transient(exc: BaseException) -> bool:
    """Retry timeouts/connection errors and 429/5xx; a 401/400 is permanent.
    An exhausted-budget rejection is permanent too (retrying just burns time and
    won't clear), so it's excluded even when it arrives as a 429."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if _looks_like_budget(status, exc.response.text):
            return False
        return status == 429 or status >= 500
    return False


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=8),
    reraise=True,
)
def _post(url: str, body: dict, headers: dict, timeout: int) -> httpx.Response:
    resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


class OllamaClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.ollama_api_key
        self.model = model or settings.ollama_model
        self.timeout = timeout or settings.ollama_timeout

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _send(self, body: dict) -> dict[str, Any]:
        """POST one /api/chat ``body``, log the full exchange, and return the
        parsed response payload. Shared by chat_json/chat_text so every Ollama
        communication is logged and error-handled the same way."""
        url = f"{self.base_url}/api/chat"
        xid = _log_request(url, body)
        started = time.perf_counter()
        try:
            resp = _post(url, body, self._headers, self.timeout)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = f"Ollama returned {status}: {exc.response.text[:300]}"
            _log_failure(xid, started, detail)
            if _looks_like_budget(status, exc.response.text):
                raise OllamaBudgetError(
                    f"Ollama budget/quota exhausted (HTTP {status}): {exc.response.text[:300]}"
                ) from exc
            raise OllamaError(detail) from exc
        except httpx.HTTPError as exc:
            _log_failure(xid, started, f"transport error: {exc}")
            raise OllamaError(f"Could not reach Ollama at {url}: {exc}") from exc

        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            _log_failure(xid, started, f"non-JSON body: {resp.text[:300]}")
            raise OllamaError(f"Ollama returned a non-JSON body: {resp.text[:300]}") from exc
        _log_response(xid, started, payload)
        return payload

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Send a chat request constrained to ``schema`` and return parsed JSON."""
        payload = self._send({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "options": {"temperature": temperature},
        })
        content = payload.get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Model did not return valid JSON: {content[:300]}") from exc

    def chat_text(self, system: str, user: str, temperature: float = 0.4) -> str:
        """Free-form completion (used for P2 cover letters etc.)."""
        payload = self._send({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        })
        return payload.get("message", {}).get("content", "")

    def health(self) -> str:
        """Connectivity check used by /health and the CLI. Returns one of
        ``"ok"`` | ``"unauthorized"`` | ``"unreachable"`` so a bad API key is not
        mistaken for a healthy server (Ollama Cloud answers 401 with a bad key)."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", headers=self._headers, timeout=10)
        except httpx.HTTPError:
            return "unreachable"
        if resp.status_code in (401, 403):
            return "unauthorized"
        if resp.status_code >= 500:
            return "unreachable"
        return "ok"


def get_client() -> OllamaClient:
    return OllamaClient()
