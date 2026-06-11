"""Thin client for the Ollama /api/chat endpoint.

Works against both Ollama Cloud (https://ollama.com, bearer key) and a local
server (http://localhost:11434, no key) — the only difference is the base URL
and whether we send an Authorization header. We use Ollama's *structured
outputs* feature (``format`` = a JSON schema) so the model returns parseable
JSON instead of prose."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from ..config import settings

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


def _is_transient(exc: BaseException) -> bool:
    """Retry timeouts/connection errors and 429/5xx; a 401/400 is permanent."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
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

    def chat_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Send a chat request constrained to ``schema`` and return parsed JSON."""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "options": {"temperature": temperature},
        }
        url = f"{self.base_url}/api/chat"
        try:
            resp = _post(url, body, self._headers, self.timeout)
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not reach Ollama at {url}: {exc}") from exc

        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Ollama returned a non-JSON body: {resp.text[:300]}") from exc
        content = payload.get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Model did not return valid JSON: {content[:300]}") from exc

    def chat_text(self, system: str, user: str, temperature: float = 0.4) -> str:
        """Free-form completion (used for P2 cover letters etc.)."""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        }
        url = f"{self.base_url}/api/chat"
        try:
            resp = _post(url, body, self._headers, self.timeout)
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not reach Ollama at {url}: {exc}") from exc
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Ollama returned a non-JSON body: {resp.text[:300]}") from exc
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
