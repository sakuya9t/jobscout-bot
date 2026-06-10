"""Command-line entrypoint: jobscout <command>.

Commands:
  serve            Run the web app (uvicorn) with scheduler + Telegram poller.
  mcp              Run the MCP server over stdio (for openclaw/hermes/agents).
  run-daily        Run the scrape+score pipeline once for all users (cron-friendly).
  init-db          Create database tables.
  health           Check DB + Ollama connectivity.
  token <email>    Mint a bearer token for a user (for MCP / API clients).
"""
from __future__ import annotations

import argparse
import sys


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import settings

    # Refuse to expose a forgeable-JWT server beyond localhost.
    if settings.secret_is_default and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Refusing to bind {args.host} with the default JOBSCOUT_SECRET_KEY "
            "(JWTs would be forgeable). Set a long random JOBSCOUT_SECRET_KEY.",
            file=sys.stderr,
        )
        return 2
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def cmd_run_daily(args: argparse.Namespace) -> int:
    from .db import init_db
    from .services import matcher, telegram_bot

    init_db()
    summaries = matcher.run_for_all_users(retry_failed=args.retry_failed)
    new = sum(s.new_positions for s in summaries.values())
    scored = sum(s.scored for s in summaries.values())
    errors = [e for s in summaries.values() for e in s.errors]
    telegram_bot.send_daily_reports()
    print(f"Users: {len(summaries)} | new positions: {new} | scored: {scored} | warnings: {len(errors)}")
    for e in errors:
        print(f"  - {e}")
    return 0


def cmd_init_db(_: argparse.Namespace) -> int:
    from .db import init_db

    init_db()
    print("Database initialized.")
    return 0


def cmd_health(_: argparse.Namespace) -> int:
    from .db import init_db, session_scope
    from sqlalchemy import text

    from .services.ollama_client import get_client

    init_db()
    db_ok = True
    try:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        print(f"db error: {exc}")
    ollama = get_client().health()
    print(f"db: {'ok' if db_ok else 'FAIL'} | ollama: {ollama}")
    return 0 if db_ok and ollama != "unreachable" else 1


def cmd_token(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from .auth import create_access_token
    from .db import init_db, session_scope
    from .models import User

    init_db()
    with session_scope() as db:
        user = db.scalar(select(User).where(User.email == args.email))
        if not user:
            print(f"No user with email {args.email}", file=sys.stderr)
            return 1
        print(create_access_token(user.id))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobscout")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="Run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("mcp", help="Run the MCP server (stdio)").set_defaults(func=cmd_mcp)
    p = sub.add_parser("run-daily", help="Run the pipeline once for all users")
    p.add_argument("--retry-failed", action="store_true",
                   help="Clear prior scoring-error markers so failed pairs are re-scored")
    p.set_defaults(func=cmd_run_daily)
    sub.add_parser("init-db", help="Create database tables").set_defaults(func=cmd_init_db)
    sub.add_parser("health", help="Check DB + Ollama").set_defaults(func=cmd_health)

    p = sub.add_parser("token", help="Mint a bearer token for a user")
    p.add_argument("email")
    p.set_defaults(func=cmd_token)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
