"""Per-user Telegram settings: the user's own bot token + the chat their daily
report is delivered to. The dashboard's settings page saves the token, links the
chat (by reading the bot's ``/start <code>`` on demand), and sends a test message;
the scheduler pushes the daily report through the same per-user bot."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import get_current_user, new_link_code
from ..db import get_db
from ..models import User
from ..schemas import TelegramActionResult, TelegramConfigIn, TelegramConfigOut
from ..services import telegram_bot

router = APIRouter(prefix="/api/telegram-config", tags=["telegram-config"])

_TEST_MESSAGE = "✅ JobScout test message — your Telegram delivery is working."


def _current(db: Session, user: User) -> TelegramConfigOut:
    """The user's Telegram state. Mints a fresh link code when unlinked and none
    exists (e.g. a prior code was burned), so there's always a code to DM the bot."""
    if not user.telegram_chat_id and not user.telegram_link_code:
        user.telegram_link_code = new_link_code()
        db.commit()
    return TelegramConfigOut(
        has_token=bool(user.telegram_bot_token),
        linked=bool(user.telegram_chat_id),
        chat_id=user.telegram_chat_id,
        link_code=user.telegram_link_code,
    )


@router.get("", response_model=TelegramConfigOut)
def get_telegram_config(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Pre-fills the settings form: whether a bot token is saved and the chat link
    status (with the one-time link code to DM the bot when not yet linked)."""
    return _current(db, user)


@router.put("", response_model=TelegramConfigOut)
def update_telegram_config(
    payload: TelegramConfigIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the user's bot token. ``bot_token`` is optional: a non-empty value
    replaces the stored token; omitting/blanking it keeps the existing one."""
    if payload.bot_token is not None:  # blank -> keep the existing token
        user.telegram_bot_token = payload.bot_token
    db.commit()
    return _current(db, user)


@router.post("/link", response_model=TelegramActionResult)
def link_chat(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Bind the chat the user just DMed ``/start <code>`` from. Reads the bot's
    recent updates once (no background poller), and on a match stores the chat id
    and burns the one-time code so a leaked code can't re-bind the chat."""
    if not user.telegram_bot_token:
        return TelegramActionResult(ok=False, detail="Add and save your bot token first.")
    if not user.telegram_link_code:
        # Already linked, or the code was burned — mint one so the user can re-link.
        user.telegram_link_code = new_link_code()
        db.commit()
    try:
        chat_id = telegram_bot.find_start_chat(user.telegram_bot_token, user.telegram_link_code)
    except Exception as exc:  # noqa: BLE001 — surface any transport/API failure to the UI
        return TelegramActionResult(ok=False, detail=f"Couldn't read updates from your bot ({exc}).")
    if not chat_id:
        return TelegramActionResult(
            ok=False,
            detail=f"No “/start {user.telegram_link_code}” received yet — DM that to your bot, "
                   "then click Link chat.",
        )
    user.telegram_chat_id = chat_id
    user.telegram_link_code = None  # one-time: burn on successful link
    db.commit()
    return TelegramActionResult(ok=True, detail=f"Linked to chat {chat_id} — reports will arrive here.")


@router.post("/test", response_model=TelegramActionResult)
def test_telegram_config(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Confirm delivery works: validate the saved bot token, then (when a chat is
    linked) send a real test message. Never raises — it reports each outcome so the
    UI can show it. Uses the saved token/chat, so Save (and Link) come first."""
    token = user.telegram_bot_token
    if not token:
        return TelegramActionResult(ok=False, detail="Add and save your bot token first.")
    ok, info = telegram_bot.get_bot_username(token)
    if not ok:
        return TelegramActionResult(ok=False, detail=f"Bot token check failed — {info}.")
    if not user.telegram_chat_id:
        return TelegramActionResult(
            ok=False,
            detail=f"Bot @{info} works, but no chat is linked yet — DM “/start "
                   f"{user.telegram_link_code}” to your bot and click Link chat.",
        )
    if telegram_bot.send_message(token, user.telegram_chat_id, _TEST_MESSAGE):
        return TelegramActionResult(ok=True, detail=f"Sent a test message via @{info} — check Telegram.")
    return TelegramActionResult(
        ok=False,
        detail=f"Bot @{info} works, but the message was rejected — is your chat still open with the bot?",
    )
