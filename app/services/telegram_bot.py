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


# Telegram rejects any sendMessage body over 4096 chars with a 400, which would
# drop the whole report. Split on line boundaries to stay under the limit.
_TELEGRAM_LIMIT = 4096


def _split_for_telegram(text: str, limit: int = _TELEGRAM_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # A single line longer than the limit gets hard-split (rare; only the
        # reasoning text, which carries no HTML tags to break).
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def _response_ok(resp: httpx.Response) -> bool:
    try:
        return resp.status_code == 200 and bool(resp.json().get("ok"))
    except Exception:  # noqa: BLE001 — a non-JSON/garbage body is "not ok"
        return False


def send_message(chat_id: str, text: str) -> None:
    if not _enabled():
        return
    for chunk in _split_for_telegram(text):
        try:
            resp = httpx.post(
                _api("sendMessage"),
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=20,
            )
        except httpx.HTTPError as exc:
            log.warning("telegram send failed: %s", exc)
            return
        if not _response_ok(resp):
            # e.g. 400 "message is too long", 403 bot blocked — previously these
            # were silently dropped, so a user just never got their report.
            log.warning("telegram sendMessage rejected (HTTP %s): %s",
                        resp.status_code, resp.text[:200])
            return


def _link_account(link_code: str, chat_id: str) -> str:
    with session_scope() as db:
        user = db.scalar(select(User).where(User.telegram_link_code == link_code))
        if not user:
            return "That link code isn't valid. Copy it from your JobScout dashboard."
        user.telegram_chat_id = str(chat_id)
        # One-time: burn the code on use so a leaked code can't be replayed to
        # re-bind someone else's chat. The dashboard can mint a fresh one.
        user.telegram_link_code = None
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


def send_daily_reports(error_by_user: dict[int, list[str]] | None = None) -> None:
    """Push today's report to every user who has linked Telegram. ``error_by_user``
    maps user id → that run's warnings so they're surfaced alongside the matches
    instead of leaving a broken account staring at an empty report."""
    if not _enabled():
        return
    from ..timeutil import utcnow

    error_by_user = error_by_user or {}
    today_utc = utcnow().date()
    with session_scope() as db:
        users = list(db.scalars(select(User).where(User.telegram_chat_id.isnot(None))))
        for user in users:
            report = build_report(db, user, on_date=today_utc)
            send_message(
                user.telegram_chat_id,
                report_to_telegram(report, error_by_user.get(user.id)),
            )
