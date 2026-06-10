"""Minimal Telegram integration via the HTTP Bot API (no extra deps).

Two jobs:
1. ``poll_updates`` — long-poll for ``/start <link-code>`` messages so users can
   bind their Telegram chat to their JobScout account.
2. ``send_daily_reports`` — push each user's ranked report to their linked chat.

The bot is entirely optional: if no token is configured everything no-ops."""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import User
from .reporter import build_report, report_to_telegram

log = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(settings.telegram_bot_token)


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def send_message(chat_id: str, text: str) -> None:
    if not _enabled():
        return
    try:
        httpx.post(
            _api("sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        log.warning("telegram send failed: %s", exc)


def _link_account(link_code: str, chat_id: str) -> str:
    with session_scope() as db:
        user = db.scalar(select(User).where(User.telegram_link_code == link_code))
        if not user:
            return "That link code isn't valid. Copy it from your JobScout dashboard."
        user.telegram_chat_id = str(chat_id)
        return f"✅ Linked to {user.email}. You'll get your daily JobScout report here."


def poll_updates(offset: int | None = None) -> int | None:
    """One long-poll cycle. Returns the next update offset. Handles /start <code>.

    Raises on any transport/API failure (bad token → 401, non-JSON body, or an
    ``ok: false`` envelope) so the caller can back off instead of hot-looping —
    a 401 from a bad token returns *instantly*, with no long-poll delay."""
    if not _enabled():
        return offset
    resp = httpx.get(
        _api("getUpdates"),
        params={"timeout": 30, "offset": offset} if offset else {"timeout": 30},
        timeout=40,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"telegram getUpdates not ok: {str(payload)[:200]}")

    next_offset = offset
    for update in payload.get("result", []):
        next_offset = update["update_id"] + 1
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not chat_id:
            continue
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                send_message(str(chat_id), _link_account(parts[1].strip(), str(chat_id)))
            else:
                send_message(str(chat_id), "Send <code>/start &lt;your-link-code&gt;</code> "
                                           "(find the code on your JobScout dashboard).")
    return next_offset


def send_daily_reports() -> None:
    """Push today's report to every user who has linked Telegram."""
    if not _enabled():
        return
    from ..timeutil import utcnow

    today_utc = utcnow().date()
    with session_scope() as db:
        users = list(db.scalars(select(User).where(User.telegram_chat_id.isnot(None))))
        for user in users:
            report = build_report(db, user, on_date=today_utc)
            send_message(user.telegram_chat_id, report_to_telegram(report))
