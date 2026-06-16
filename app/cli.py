"""Command-line entrypoint: jobscout <command>.

Commands:
  serve            Run the web app (uvicorn) with the daily scheduler.
  mcp              Run the MCP server over stdio (for openclaw/hermes/agents).
  run-daily        Run the scrape+score pipeline once for all users (cron-friendly).
  init-db          Create database tables.
  health           Check DB + Ollama connectivity.
  token <email>    Mint a bearer token for a user (for MCP / API clients).
  backfill-descriptions
                   Fetch missing job descriptions for eightfold boards (e.g. NVIDIA),
                   whose search API returns none, so older postings become scoreable.
  migrate-db       Copy schema + data to another database (e.g. SQLite -> Supabase
                   Postgres) to publish the app on a hosted DB.
"""
from __future__ import annotations

import argparse
import os
import re
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
    telegram_bot.send_daily_reports({uid: s.errors for uid, s in summaries.items() if s.errors})
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


def cmd_backfill_descriptions(args: argparse.Namespace) -> int:
    """Backfill descriptions for already-stored eightfold postings (NVIDIA et al.).

    The PCSX search API carries no description, so positions scraped before the
    detail-page enrichment landed are description-less and the matcher skips them.
    This fetches each one's JSON-LD description (batched, bounded concurrency) and
    updates it in place. Idempotent — re-running only touches still-empty rows.
    Targeted: only postings that have a URL and an empty description are fetched, so
    it never re-hits the search API or the other ATSes."""
    from sqlalchemy import select

    from .db import init_db, session_scope
    from .models import Company, Position
    from .services import scraper

    init_db()
    total = 0
    with session_scope() as db:
        companies = list(db.scalars(
            select(Company).where(Company.ats_type == "eightfold", Company.is_active == True)  # noqa: E712
        ))
        if args.company:
            key = args.company.strip().lower()
            companies = [c for c in companies
                         if (c.preset_key or "").lower() == key or c.name.lower() == key]
        if not companies:
            print("No matching active eightfold companies.", file=sys.stderr)
            return 1
        for company in companies:
            empties = [
                p for p in db.scalars(select(Position).where(Position.company_id == company.id))
                if p.url and not (p.description or "").strip()
            ]
            cap = args.limit if args.limit and args.limit > 0 else len(empties)
            urls = [p.url for p in empties][:cap]
            if not urls:
                print(f"{company.name}: nothing to backfill.")
                continue
            print(f"{company.name}: fetching {len(urls)} of {len(empties)} "
                  f"description-less posting(s)…")
            descs = scraper.fetch_eightfold_descriptions(urls, cap=len(urls))
            updated = 0
            for p in empties:
                text = descs.get(p.url)
                if text:
                    p.description = text
                    updated += 1
            db.commit()  # release the write lock between companies
            total += updated
            print(f"{company.name}: backfilled {updated} description(s).")
    print(f"Done — backfilled {total} posting(s). Run a scan (or `jobscout run-daily`) "
          "to score the newly-described jobs.")
    return 0


def _redact_url(url: str) -> str:
    """Mask the password in a DB URL for safe printing."""
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:***@", url)


def cmd_migrate_db(args: argparse.Namespace) -> int:
    """Copy schema + all rows from the current database (``settings.database_url``,
    e.g. the local SQLite file) into a target database (e.g. Supabase Postgres).

    Creates the schema on the target from the ORM models, copies every table in
    FK-safe dependency order, then fixes Postgres autoincrement sequences so future
    inserts don't collide with the copied ids. Encrypted columns (company_accounts)
    copy as-is — decryption keeps working only if the target deployment uses the
    SAME JOBSCOUT_SECRET_KEY. ``--drop`` recreates the target schema first (so a
    re-run is clean); without it the target must be empty."""
    from sqlalchemy import create_engine, func, select, text

    from . import models
    from .config import settings
    from .db import engine as source_engine

    target_url = args.target or os.environ.get("JOBSCOUT_TARGET_DATABASE_URL")
    if not target_url:
        print("No target. Pass --target <url> or set JOBSCOUT_TARGET_DATABASE_URL.",
              file=sys.stderr)
        return 2
    if target_url == settings.database_url:
        print("Target is the same as the source — nothing to do.", file=sys.stderr)
        return 2

    print(f"Source: {_redact_url(settings.database_url)}")
    print(f"Target: {_redact_url(target_url)}")
    target_engine = create_engine(target_url, future=True)
    md = models.Base.metadata

    try:
        if args.drop:
            print("Recreating target schema (--drop)…")
            md.drop_all(target_engine)
        md.create_all(target_engine)

        total = 0
        with source_engine.connect() as src, target_engine.begin() as dst:
            for table in md.sorted_tables:
                rows = [dict(r._mapping) for r in src.execute(select(table))]
                for i in range(0, len(rows), args.batch):
                    dst.execute(table.insert(), rows[i : i + args.batch])
                print(f"  {table.name:24} {len(rows):>6} rows")
                total += len(rows)

            # Postgres: copied rows carry explicit ids, so the SERIAL sequences are
            # still at 1 and the next insert would collide. Advance each to MAX(id).
            if dst.dialect.name == "postgresql":
                for table in md.sorted_tables:
                    if "id" not in table.c or not table.c.id.primary_key:
                        continue
                    max_id = dst.execute(select(func.max(table.c.id))).scalar()
                    if max_id is None:
                        continue
                    seq = dst.execute(
                        text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table.name}
                    ).scalar()
                    if seq:
                        dst.execute(text("SELECT setval(:s, :v, true)"), {"s": seq, "v": max_id})
                print("  (reset Postgres id sequences)")
    except Exception as exc:  # noqa: BLE001 — surface the failure, leave target as-is
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        target_engine.dispose()

    print(f"Done — copied {total} rows across {len(md.sorted_tables)} tables.")
    print("Next: set JOBSCOUT_DATABASE_URL to the target (same JOBSCOUT_SECRET_KEY) "
          "and restart the app.")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from .auth import create_access_token
    from .db import init_db, session_scope
    from .models import User

    init_db()
    with session_scope() as db:
        # Emails are stored lowercased (see the register route) — normalize the
        # lookup the same way so `jobscout token Foo@Bar.com` still finds the user.
        user = db.scalar(select(User).where(User.email == args.email.strip().lower()))
        if not user:
            print(f"No user with email {args.email}", file=sys.stderr)
            return 1
        print(create_access_token(user.id))
    return 0


def main() -> None:
    from .logging_config import configure_logging

    configure_logging()

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

    p = sub.add_parser("backfill-descriptions",
                       help="Fetch missing descriptions for eightfold boards (e.g. NVIDIA)")
    p.add_argument("--company", help="Limit to one company by preset key or name (e.g. nvidia)")
    p.add_argument("--limit", type=int, default=0,
                   help="Max postings to fetch per company (0 = all description-less rows)")
    p.set_defaults(func=cmd_backfill_descriptions)

    p = sub.add_parser("migrate-db",
                       help="Copy schema + data to another DB (e.g. SQLite -> Supabase Postgres)")
    p.add_argument("--target", help="Target SQLAlchemy URL (or set JOBSCOUT_TARGET_DATABASE_URL)")
    p.add_argument("--drop", action="store_true", help="Recreate the target schema first")
    p.add_argument("--batch", type=int, default=1000, help="Rows per insert batch")
    p.set_defaults(func=cmd_migrate_db)

    p = sub.add_parser("token", help="Mint a bearer token for a user")
    p.add_argument("email")
    p.set_defaults(func=cmd_token)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
