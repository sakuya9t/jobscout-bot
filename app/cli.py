"""Command-line entrypoint: jobscout <command>.

Commands:
  serve            Run the web app (uvicorn) with the daily scheduler.
  mcp              Run the MCP server over stdio (for openclaw/hermes/agents).
  run-daily        Scrape all users' companies and save new positions (cron-friendly;
                   no scoring — matching runs on-demand from the job-list view).
  run-scoring      Drain every user's matching backlog via the scoring queue, with a
                   bounded worker pool so concurrent DB connections stay capped. A
                   one-shot out-of-process drain (the long-lived server drains in-process).
  queue-log        Tail the scoring-queue trace (scoring_events) for debugging a flaky
                   drain: e.g. `jobscout queue-log --user 1` or `--event error`.
  init-db          Create database tables.
  encrypt-secrets  Encrypt the telegram-token / LLM-key columns at rest (one-time,
                   idempotent migration; widens the columns on Postgres first).
  health           Check DB + Ollama connectivity.
  token <email>    Mint a bearer token for a user (for MCP / API clients).
  invite           Mint / list / revoke registration invite codes
                   (e.g. `jobscout invite mint --max-uses 5 --expires-days 30`).
  backfill-descriptions
                   Fetch missing job descriptions for eightfold boards (e.g. NVIDIA),
                   whose search API returns none, so older postings become scoreable.
  migrate-db       Copy schema + data to another database (e.g. SQLite -> DigitalOcean
                   Managed Postgres) to publish the app on a hosted DB.
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


def cmd_run_daily(_: argparse.Namespace) -> int:
    from .db import init_db, session_scope
    from .services import matcher, scoring_queue

    init_db()
    summaries = matcher.scrape_for_all_users()
    new = sum(s.new_positions for s in summaries.values())
    errors = [e for s in summaries.values() for e in s.errors]
    # Publish the scraped work: enqueue every user with a backlog. Scrape and scoring
    # remain separate jobs — this only marks work ready; the `jobscout run-scoring`
    # drain (or the long-lived server's in-process workers) does the matching.
    with session_scope() as db:
        enqueued = scoring_queue.reconcile(db)
    print(
        f"Users: {len(summaries)} | new positions: {new} | warnings: {len(errors)} | "
        f"enqueued: {enqueued}"
    )
    for e in errors:
        print(f"  - {e}")
    return 0


def cmd_run_scoring(_: argparse.Namespace) -> int:
    """One-shot scoring drain (out-of-process; the long-lived server drains in-process).
    Enqueue every user with a non-empty matching backlog, then drain the queue with a
    bounded worker pool — capping concurrent DB connections regardless of how many users
    have work. Separate from ``run-daily`` (scrape-only) on purpose."""
    from .config import settings
    from .db import init_db, session_scope
    from .services import evaluator, scoring_queue

    init_db()
    with session_scope() as db:
        enqueued = scoring_queue.reconcile(db)
    summary = evaluator.drain_queue(
        max_workers=settings.scoring_max_concurrency,
        budget_seconds=settings.scoring_run_budget_seconds,
    )
    with session_scope() as db:
        states = scoring_queue.counts_by_state(db)
        more = scoring_queue.has_pending(db)
    print(
        f"Enqueued: {enqueued} | users drained: {summary.users} | "
        f"scored: {summary.scored} | failed: {summary.failed}"
    )
    print(f"Queue: {states or '{}'}")  # e.g. {'done': 3, 'error': 1, 'pending': 2}
    if more:
        # The per-run budget stopped a big backlog mid-drain (rows re-armed to pending);
        # the next scheduled run continues it.
        print("Backlog remains -> will continue on the next run")
    for e in summary.errors:
        print(f"  - {e}")
    return 0


def cmd_retry_failed(args: argparse.Namespace) -> int:
    """Clear failed-scoring markers (incl. ones that hit the retry limit and went
    terminal) so those (posting, interest) pairs re-enter the backlog and re-score on
    the next run. Optionally scoped to one user."""
    from .db import init_db, session_scope
    from .services import matcher

    init_db()
    with session_scope() as db:
        cleared = matcher.clear_failed_markers(db, args.user)
    scope = f"user {args.user}" if args.user else "all users"
    print(f"Cleared {cleared} failed-scoring marker(s) for {scope}; "
          "they re-score on the next scoring run.")
    return 0


def cmd_queue_log(args: argparse.Namespace) -> int:
    """Tail the scoring-queue trace (the scoring_events table). Shows the ordered
    lifecycle trail — enqueue/claim/finalize/drain/error/reconcile/worker — so a flaky
    drain is reconstructable: `jobscout queue-log --user 1` to follow one user, or
    `--event error` to see only failures. Newest rows last (chronological)."""
    from sqlalchemy import select

    from .db import init_db, session_scope
    from .models import ScoringEvent

    init_db()
    with session_scope() as db:
        q = select(ScoringEvent)
        if args.user is not None:
            q = q.where(ScoringEvent.user_id == args.user)
        if args.event:
            q = q.where(ScoringEvent.event == args.event)
        # Take the most recent N, then show oldest-first so the trail reads top-to-bottom.
        rows = list(db.scalars(
            q.order_by(ScoringEvent.created_at.desc(), ScoringEvent.id.desc()).limit(args.limit)
        ))[::-1]
        if not rows:
            print("No scoring events recorded yet.")
            return 0
        print(f"{'time':<19}  {'user':>4}  {'event':<9}  {'transition':<18}  {'att':>3}  {'worker':<16}  detail")
        for r in rows:
            ts = r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "?"
            trans = f"{r.state_from or '-'}->{r.state_to or '-'}"
            att = "" if r.attempts is None else str(r.attempts)
            user = "" if r.user_id is None else str(r.user_id)
            print(f"{ts:<19}  {user:>4}  {r.event:<9}  {trans:<18}  {att:>3}  "
                  f"{(r.worker or ''):<16}  {r.detail or ''}")
    return 0


def cmd_init_db(_: argparse.Namespace) -> int:
    from .db import init_db

    init_db()
    print("Database initialized.")
    return 0


def cmd_encrypt_secrets(_: argparse.Namespace) -> int:
    """Encrypt the credential columns (users.telegram_bot_token, llm_configs.api_key)
    at rest, in place. One-time migration for a DB that predates ``EncryptedString``;
    idempotent, so it's safe to re-run (and is run on every deploy). On Postgres it
    first widens each column to TEXT — Fernet ciphertext outgrows the old VARCHAR caps.
    Reads/writes raw values via Core SQL so it bypasses the EncryptedString type and
    can tell an already-encrypted value from a plaintext one.

    Must run with the SAME JOBSCOUT_SECRET_KEY the app uses, or the app can't decrypt
    what this writes."""
    from sqlalchemy import text

    from .crypto import decrypt, encrypt
    from .db import engine, init_db

    init_db()
    targets = [("users", "telegram_bot_token"), ("llm_configs", "api_key")]
    converted = 0
    with engine.begin() as conn:
        is_pg = conn.dialect.name == "postgresql"
        for table, col in targets:
            if is_pg:
                dtype = conn.execute(
                    text("SELECT data_type FROM information_schema.columns "
                         "WHERE table_name = :t AND column_name = :c"),
                    {"t": table, "c": col},
                ).scalar()
                if dtype and dtype != "text":
                    conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE text'))
                    print(f"  {table}.{col}: widened {dtype} -> text")
            rows = conn.execute(
                text(f'SELECT id, "{col}" AS v FROM "{table}" WHERE "{col}" IS NOT NULL')
            ).all()
            n = 0
            for row_id, value in rows:
                if decrypt(value) is not None:
                    continue  # already valid ciphertext for the current key
                conn.execute(
                    text(f'UPDATE "{table}" SET "{col}" = :v WHERE id = :id'),
                    {"v": encrypt(value), "id": row_id},
                )
                n += 1
            converted += n
            print(f"  {table}.{col}: {len(rows)} value(s) present, {n} newly encrypted")
    print(f"Done — encrypted {converted} previously-plaintext secret(s). Re-running is a no-op.")
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
    print(f"Done — backfilled {total} posting(s). Run a scan from the job-list view "
          "to score the newly-described jobs.")
    return 0


def _redact_url(url: str) -> str:
    """Mask the password in a DB URL for safe printing."""
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:***@", url)


def cmd_migrate_db(args: argparse.Namespace) -> int:
    """Copy schema + all rows from the current database (``settings.database_url``,
    e.g. the local SQLite file) into a target database (e.g. DigitalOcean Managed Postgres).

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


def cmd_invite(args: argparse.Namespace) -> int:
    """Mint / list / revoke registration invite codes (see app/invites.py)."""
    from sqlalchemy import select

    from . import invites
    from .db import init_db, session_scope
    from .models import InviteCode

    init_db()
    with session_scope() as db:
        if args.invite_command == "mint":
            codes = invites.mint(
                db,
                max_uses=args.max_uses,
                expires_in_days=args.expires_days,
                note=args.note,
                count=args.count,
            )
            # Printed once — only the HMAC is stored, so a code can't be recovered later.
            print(f"Minted {len(codes)} code(s) "
                  f"(max_uses={max(1, args.max_uses)}, "
                  f"expires={'in ' + str(args.expires_days) + 'd' if args.expires_days else 'never'}):")
            for code in codes:
                print(f"  {code}")
            return 0

        if args.invite_command == "list":
            rows = list(db.scalars(select(InviteCode).order_by(InviteCode.id)))
            if not rows:
                print("No invite codes.")
                return 0
            print(f"{'id':>4}  {'uses':>9}  {'expires':<19}  {'state':<8}  note")
            for r in rows:
                expires = r.expires_at.strftime("%Y-%m-%d %H:%M") if r.expires_at else "never"
                if r.revoked:
                    state = "revoked"
                elif invites.is_expired(r):
                    state = "expired"
                elif r.uses >= r.max_uses:
                    state = "used-up"
                else:
                    state = "active"
                print(f"{r.id:>4}  {r.uses:>4}/{r.max_uses:<4}  {expires:<19}  {state:<8}  {r.note or ''}")
            return 0

        if args.invite_command == "revoke":
            target = args.code.strip()
            ok = (
                invites.revoke(db, code_id=int(target)) if target.isdigit()
                else invites.revoke(db, code=target)
            )
            print(f"Revoked {target}." if ok else f"No matching code: {target}")
            return 0 if ok else 1

    return 2


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
    sub.add_parser(
        "run-daily",
        help="Scrape all users' companies and save new positions (no scoring)",
    ).set_defaults(func=cmd_run_daily)
    sub.add_parser(
        "run-scoring",
        help="Drain the matching backlog for all users (bounded; cron-friendly)",
    ).set_defaults(func=cmd_run_scoring)
    sub.add_parser("init-db", help="Create database tables").set_defaults(func=cmd_init_db)
    sub.add_parser(
        "encrypt-secrets",
        help="Encrypt telegram_bot_token + llm api_key at rest (idempotent one-time migration)",
    ).set_defaults(func=cmd_encrypt_secrets)
    sub.add_parser("health", help="Check DB + Ollama").set_defaults(func=cmd_health)

    p = sub.add_parser(
        "retry-failed",
        help="Clear failed-scoring markers so those postings re-score on the next run",
    )
    p.add_argument("--user", type=int, default=None, help="Limit to one user id (default: all)")
    p.set_defaults(func=cmd_retry_failed)

    p = sub.add_parser(
        "queue-log",
        help="Tail the scoring-queue trace (enqueue/claim/finalize/error/...) for debugging",
    )
    p.add_argument("--user", type=int, default=None, help="Limit to one user id (default: all)")
    p.add_argument("--event", default=None,
                   help="Limit to one event type (enqueue|claim|finalize|drain|error|done|reconcile|worker)")
    p.add_argument("--limit", type=int, default=50, help="How many recent events to show")
    p.set_defaults(func=cmd_queue_log)

    p = sub.add_parser("backfill-descriptions",
                       help="Fetch missing descriptions for eightfold boards (e.g. NVIDIA)")
    p.add_argument("--company", help="Limit to one company by preset key or name (e.g. nvidia)")
    p.add_argument("--limit", type=int, default=0,
                   help="Max postings to fetch per company (0 = all description-less rows)")
    p.set_defaults(func=cmd_backfill_descriptions)

    p = sub.add_parser("migrate-db",
                       help="Copy schema + data to another DB (e.g. SQLite -> DigitalOcean Managed Postgres)")
    p.add_argument("--target", help="Target SQLAlchemy URL (or set JOBSCOUT_TARGET_DATABASE_URL)")
    p.add_argument("--drop", action="store_true", help="Recreate the target schema first")
    p.add_argument("--batch", type=int, default=1000, help="Rows per insert batch")
    p.set_defaults(func=cmd_migrate_db)

    p = sub.add_parser("token", help="Mint a bearer token for a user")
    p.add_argument("email")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("invite", help="Mint / list / revoke registration invite codes")
    isub = p.add_subparsers(dest="invite_command", required=True)
    m = isub.add_parser("mint", help="Mint new invite code(s); prints them once")
    m.add_argument("--max-uses", type=int, default=1, help="How many times each code can be used")
    m.add_argument("--expires-days", type=int, default=None,
                   help="Days until each code expires (omit = never expires)")
    m.add_argument("--count", type=int, default=1, help="How many codes to mint")
    m.add_argument("--note", default=None, help="Optional label stored with the code(s)")
    isub.add_parser("list", help="List all invite codes and their state")
    rv = isub.add_parser("revoke", help="Revoke a code by id or by the code itself")
    rv.add_argument("code", help="Invite code id (number) or the plaintext code")
    p.set_defaults(func=cmd_invite)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
