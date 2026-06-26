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
from ..llm_providers import DEFAULT_PROVIDER_OBJ
from ..logging_config import get_logger
from . import llm_log

log = get_logger(__name__)


class OllamaError(RuntimeError):
    pass


class OllamaBudgetError(OllamaError):
    """Ollama rejected the call because the account's budget / quota / credit is
    exhausted (HTTP 402, or a 403/429 whose body names a usage cap rather than a
    short-lived rate limit).

    Distinct from a generic OllamaError so the matcher can (a) tell the user their
    Ollama account is out of quota in plain language and (b) NOT write permanent
    error-markers — an exhausted budget is recoverable, so those postings must
    re-score automatically once quota returns, not be skipped until the markers are
    manually cleared."""

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
    """Truncate stored prompt/response content to keep the log table bounded;
    ``log_ollama_max_chars`` = 0 stores the full text."""
    limit = settings.log_ollama_max_chars
    if limit and len(text) > limit:
        return f"{text[:limit]}… [+{len(text) - limit} chars]"
    return text


def _record_exchange(
    xid: str,
    started: float,
    url: str,
    body: dict,
    *,
    status: str,
    payload: dict | None = None,
    error: str | None = None,
) -> None:
    """Persist one Ollama exchange to the ``llm_logs`` table (off the hot path) and
    emit a single terse stdout line — never the full prompt/response, which used to
    spam the console. Failures log at WARNING (so they stay visible); successes log
    at DEBUG (quiet under the default INFO level). Gated by ``settings.log_ollama``;
    the API key is never part of the body we record."""
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if status == "ok":
        log.debug(
            "ollama ✓ %s %dms tokens=%s/%s [%s]", body.get("model"), elapsed_ms,
            (payload or {}).get("prompt_eval_count"), (payload or {}).get("eval_count"), xid,
        )
    else:
        log.warning("ollama ✗ %s %dms %s [%s]", body.get("model"), elapsed_ms, error, xid)

    if not settings.log_ollama:
        return
    msgs = body.get("messages", [])
    content = (payload or {}).get("message", {}).get("content") or "" if payload else ""
    llm_log.enqueue(llm_log.LlmLogRecord(
        correlation_id=xid,
        model=body.get("model"),
        url=url,
        temperature=(body.get("options") or {}).get("temperature"),
        response_format="json-schema" if body.get("format") else "text",
        prompt_chars=sum(len(m.get("content") or "") for m in msgs),
        request_messages=json.dumps(
            [{"role": m.get("role"), "content": _clip(m.get("content") or "")} for m in msgs]
        ),
        status=status,
        elapsed_ms=elapsed_ms,
        done_reason=(payload or {}).get("done_reason"),
        prompt_tokens=(payload or {}).get("prompt_eval_count"),
        eval_tokens=(payload or {}).get("eval_count"),
        response_content=_clip(content) if content else None,
        error_detail=error,
    ))


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
        # Callers normally pass these explicitly (resolved per-user in services/llm
        # .py); the defaults just keep the bare OllamaClient() used by the health
        # probe working (default provider, no key).
        self.base_url = (base_url or DEFAULT_PROVIDER_OBJ.base_url).rstrip("/")
        self.api_key = api_key
        self.model = model or DEFAULT_PROVIDER_OBJ.default_main_model
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
        xid = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        try:
            resp = _post(url, body, self._headers, self.timeout)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = f"Ollama returned {status}: {exc.response.text[:300]}"
            _record_exchange(xid, started, url, body, status="error", error=detail)
            if _looks_like_budget(status, exc.response.text):
                raise OllamaBudgetError(
                    f"Ollama budget/quota exhausted (HTTP {status}): {exc.response.text[:300]}"
                ) from exc
            raise OllamaError(detail) from exc
        except httpx.HTTPError as exc:
            _record_exchange(xid, started, url, body, status="error", error=f"transport error: {exc}")
            raise OllamaError(f"Could not reach Ollama at {url}: {exc}") from exc

        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            _record_exchange(
                xid, started, url, body, status="error", error=f"non-JSON body: {resp.text[:300]}"
            )
            raise OllamaError(f"Ollama returned a non-JSON body: {resp.text[:300]}") from exc
        _record_exchange(xid, started, url, body, status="ok", payload=payload)
        return payload

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        temperature: float = 0.2,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Send a chat request constrained to ``schema`` and return parsed JSON. Pass
        ``seed`` (with ``temperature=0``) to make the sample reproducible — the scoring
        path does, so the same posting scores the same way run to run."""
        options: dict[str, Any] = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        payload = self._send({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "options": options,
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
