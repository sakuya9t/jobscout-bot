"""Per-user Telegram integration via the HTTP Bot API (no extra deps).

Telegram is per-user: each user creates their own bot (via @BotFather), saves its
token in settings, and links the chat reports go to by DMing ``/start <code>`` to
that bot. There is no global bot or background long-poll loop — linking is done on
demand from the settings page (``find_start_chat`` reads the bot's recent updates
once), and the daily scheduler pushes each user's report through that user's own
bot. Everything no-ops gracefully for a user who hasn't configured a bot/chat."""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from ..db import session_scope
from ..models import User
from .reporter import build_report, report_to_telegram

log = logging.getLogger(__name__)


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


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


def send_message(token: str, chat_id: str, text: str) -> bool:
    """Send (chunked) text to a chat via the given bot. Returns True iff every
    chunk was accepted. Never raises — transport/API failures are logged and
    reported as False so callers (the daily push and the settings "Test" button)
    can surface the problem instead of silently dropping the report."""
    if not token or not chat_id:
        return False
    for chunk in _split_for_telegram(text):
        try:
            resp = httpx.post(
                _api(token, "sendMessage"),
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=20,
            )
        except httpx.HTTPError as exc:
            log.warning("telegram send failed: %s", exc)
            return False
        if not _response_ok(resp):
            # e.g. 400 "message is too long", 403 bot blocked — previously these
            # were silently dropped, so a user just never got their report.
            log.warning("telegram sendMessage rejected (HTTP %s): %s",
                        resp.status_code, resp.text[:200])
            return False
    return True


def get_bot_username(token: str) -> tuple[bool, str]:
    """Validate a bot token via getMe. Returns ``(ok, username)`` on success or
    ``(False, reason)`` when the token is bad / Telegram is unreachable — used by
    the settings "Test" button to check the token before trying to send."""
    if not token:
        return False, "no bot token"
    try:
        resp = httpx.get(_api(token, "getMe"), timeout=15)
    except httpx.HTTPError as exc:
        return False, f"could not reach Telegram ({exc})"
    if not _response_ok(resp):
        return False, f"Telegram rejected the token (HTTP {resp.status_code})"
    username = (resp.json().get("result") or {}).get("username") or "your bot"
    return True, username


def get_updates(token: str, offset: int | None = None, timeout: int = 0) -> list[dict]:
    """One getUpdates call for a single bot. ``timeout=0`` returns immediately with
    whatever is buffered (we poll on demand, not in a long-poll loop). Raises on a
    transport/API failure (bad token → 401, non-JSON body, ``ok: false``)."""
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = httpx.get(_api(token, "getUpdates"), params=params, timeout=timeout + 15)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"telegram getUpdates not ok: {str(payload)[:200]}")
    return payload.get("result", [])


def find_start_chat(token: str, code: str) -> str | None:
    """Scan the bot's recent updates for ``/start <code>`` and return the chat id
    that sent it (newest wins), or None if the user hasn't DMed it yet. Requiring
    the one-time code means only the chat that knows it gets linked, so a stranger
    who happens to message the bot can't bind themselves to the account."""
    chat_id: str | None = None
    for update in get_updates(token):
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        cid = (msg.get("chat") or {}).get("id")
        if not cid:
            continue
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[0] == "/start" and parts[1].strip() == code:
            chat_id = str(cid)  # keep scanning so the most recent match wins
    return chat_id


def send_daily_reports(error_by_user: dict[int, list[str]] | None = None) -> None:
    """Push today's report to every user who has both a bot token and a linked
    chat, each through their own bot. ``error_by_user`` maps user id → that run's
    warnings so a broken account sees *why* there are no matches instead of a
    silent empty report."""
    from ..timeutil import utcnow

    error_by_user = error_by_user or {}
    today_utc = utcnow().date()
    with session_scope() as db:
        users = list(db.scalars(select(User).where(
            User.telegram_bot_token.isnot(None), User.telegram_chat_id.isnot(None)
        )))
        for user in users:
            report = build_report(db, user, on_date=today_utc)
            send_message(
                user.telegram_bot_token,
                user.telegram_chat_id,
                report_to_telegram(report, error_by_user.get(user.id)),
            )
