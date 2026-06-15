"""Internal/admin endpoints, gated by an ``X-Admin-Token`` header
(``settings.admin_token``). Not linked from the user UI — an admin UI button can
call ``POST /api/admin/crawl`` later. Disabled (503) when no admin token is set, so
this is never an open trigger."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, status

from ..config import settings
from ..services import crawler

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(token: str | None) -> None:
    if not settings.admin_token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Admin endpoints are disabled. Set JOBSCOUT_ADMIN_TOKEN to enable them.",
        )
    if not token or not secrets.compare_digest(token, settings.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid admin token.")


@router.post("/crawl", status_code=status.HTTP_202_ACCEPTED)
def trigger_crawl(x_admin_token: str | None = Header(default=None)):
    """Manually trigger a shared preset crawl. Runs in the background and returns
    202 immediately; overlapping crawls are de-duplicated by the crawler's lock."""
    _require_admin(x_admin_token)
    crawler.crawl_presets_async()
    return {"status": "crawl started"}
