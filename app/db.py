"""Database engine and session management."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .config import settings

log = logging.getLogger(__name__)

_is_sqlite = settings.database_url.startswith("sqlite")
if _is_sqlite:
    engine = create_engine(
        settings.database_url, connect_args={"check_same_thread": False}, future=True
    )
else:
    # Postgres is reached through Supabase's connection pooler (Supavisor). Pooling
    # *again* in-process means every serverless instance and batch run hoards idle
    # connections and exhausts the pooler's per-client cap ("max clients reached in
    # session mode - ... pool_size: 15"). NullPool keeps no idle connections: one is
    # opened per checkout and closed on release, so we only ever hold what we're
    # actively using. pool_pre_ping drops a connection the pooler closed under us.
    engine = create_engine(
        settings.database_url, poolclass=NullPool, pool_pre_ping=True, future=True
    )
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - exercised at runtime
        """WAL lets readers and one writer proceed concurrently; busy_timeout
        makes writers wait for a held lock instead of failing instantly. Without
        these, the daily scan's writes collide with dashboard/Telegram writes as
        'database is locked'."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        # SQLite ignores FK constraints unless this is set per-connection. ORM
        # cascades cover today's deletes, but this stops any raw-SQL delete from
        # silently orphaning child rows.
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def _literal_default(column) -> str | None:
    """A SQL literal for a column's scalar Python default, or None when it has
    none / isn't a simple constant we can safely render into DDL."""
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _reconcile_schema(bind: Engine) -> None:
    """Additive, idempotent micro-migration for SQLite dev/prod DBs.

    ``Base.metadata.create_all`` creates absent *tables* but never adds *columns*
    or indexes to a table that already exists, so adding a model field (e.g.
    ``Company.preset_key``) leaves an older on-disk DB unable to start. This brings
    such a DB up to the current models by ADDing any missing columns and creating
    any missing indexes. It is deliberately limited to *safe, additive* changes —
    new nullable columns (or ones with a literal default) and new indexes; column
    drops, renames and type changes are out of scope and still need a real
    migration.

    The additive passes (1: ADD COLUMN, 3: CREATE INDEX) run on SQLite **and**
    Postgres — both support ``ALTER TABLE ADD COLUMN`` for a nullable/defaulted
    column instantly, so prod (Supabase Postgres) picks up a new model field without a
    hand-written migration. The table-rebuild pass (2) stays SQLite-only: it exists to
    work around SQLite's inability to ALTER a column constraint, which Postgres can do
    natively and which is out of scope here anyway."""
    if bind.dialect.name not in ("sqlite", "postgresql"):
        return
    is_sqlite = bind.dialect.name == "sqlite"
    from . import models

    insp = inspect(bind)
    existing_tables = set(insp.get_table_names())
    # 1) Add missing columns (ALTER TABLE ADD COLUMN).
    with bind.begin() as conn:
        for table in models.Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all builds the whole (new) table itself
            have = {c["name"] for c in insp.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                default = _literal_default(column)
                if not column.nullable and default is None:
                    # Can't backfill a NOT NULL column with no default on existing
                    # rows; surface it rather than silently corrupt the table.
                    log.warning(
                        "schema reconcile: skipping %s.%s (NOT NULL, no default)",
                        table.name, column.name,
                    )
                    continue
                coltype = column.type.compile(dialect=bind.dialect)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {coltype}'
                if not column.nullable:
                    ddl += " NOT NULL"
                if default is not None:
                    ddl += f" DEFAULT {default}"
                log.info("schema reconcile: adding column %s.%s", table.name, column.name)
                conn.execute(text(ddl))
    # 2) Rebuild any table whose column constraints (e.g. a NOT NULL that the model
    # relaxed to nullable) drifted — ALTER can't change those in SQLite. SQLite only;
    # Postgres can ALTER constraints natively and this rebuild is unsafe there.
    if is_sqlite:
        insp = inspect(bind)  # refresh: the additive pass above changed the schema
        for table in models.Base.metadata.sorted_tables:
            if table.name in existing_tables and _nullability_drift(insp, table):
                _rebuild_table(bind, table)
    # 3) create_all skips existing tables entirely, so an index added alongside a
    # column won't exist yet; checkfirst makes this a no-op for indexes present.
    for table in models.Base.metadata.sorted_tables:
        if table.name in existing_tables:
            for index in table.indexes:
                index.create(bind=bind, checkfirst=True)


def _nullability_drift(insp, table) -> bool:
    """True when the model relaxed a column from NOT NULL to nullable but the disk
    table still enforces it — the one column-definition change we apply by rebuild.
    Tightening (nullable -> NOT NULL) is deliberately left to a real migration; the
    rebuild only ever relaxes, so it can never fail or drop rows."""
    disk = {c["name"]: c for c in insp.get_columns(table.name)}
    for column in table.columns:
        d = disk.get(column.name)
        if d is None:
            continue
        disk_notnull = not d["nullable"]
        model_nullable = column.nullable and not column.primary_key
        if disk_notnull and model_nullable:
            return True
    return False


def _rebuild_table(bind: Engine, table) -> None:
    """SQLite-safe table rebuild: create a fresh table with the model's schema,
    copy the shared columns across, then swap it in and recreate its indexes. This
    is how SQLite applies column-definition changes ALTER can't (e.g. relaxing a
    NOT NULL constraint). FK enforcement is disabled across the swap so dropping a
    referenced table is allowed; it must toggle outside a transaction.

    The rebuilt table never *tightens* nullability: a column is NOT NULL only where
    model and disk already agree, so copying existing rows can't violate a freshly
    added constraint (tightening stays a real-migration concern)."""
    from sqlalchemy import MetaData
    from sqlalchemy.schema import CreateTable

    insp = inspect(bind)
    disk = {c["name"]: c for c in insp.get_columns(table.name)}
    shared = [c.name for c in table.columns if c.name in disk]
    cols_sql = ", ".join(f'"{c}"' for c in shared)
    tmp = f"_recon_{table.name}"
    # Copy into a fresh MetaData that also holds the tables this one references, so
    # its foreign keys resolve when we compile CreateTable (only the temp table is
    # compiled; the copied siblings are just there to satisfy FK targets).
    tmp_meta = MetaData()
    for sibling in table.metadata.sorted_tables:
        sibling.to_metadata(tmp_meta, name=tmp if sibling.name == table.name else sibling.name)
    tmp_table = tmp_meta.tables[tmp]
    # Don't tighten: keep a column nullable wherever the disk already allows NULLs.
    for col in tmp_table.columns:
        d = disk.get(col.name)
        if d is not None and d["nullable"]:
            col.nullable = True
    # CreateTable emits only the table (inline constraints), not the separate
    # CREATE INDEX statements — so the temp table's creation can't collide with the
    # still-present old indexes; we recreate those by name after the swap.
    create_sql = str(CreateTable(tmp_table).compile(dialect=bind.dialect))

    log.info("schema reconcile: rebuilding table %s to match model", table.name)
    raw = bind.raw_connection()
    try:
        prev_iso = raw.isolation_level
        raw.isolation_level = None  # take manual control of BEGIN/COMMIT
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN")
        try:
            cur.execute(create_sql)
            cur.execute(f'INSERT INTO "{tmp}" ({cols_sql}) SELECT {cols_sql} FROM "{table.name}"')
            cur.execute(f'DROP TABLE "{table.name}"')
            cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table.name}"')
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.execute("PRAGMA foreign_keys=ON")
            raw.isolation_level = prev_iso
            cur.close()
    finally:
        raw.close()

    for index in table.indexes:
        index.create(bind=bind, checkfirst=True)


def seed_presets(db: Session) -> None:
    """Ensure a global Company row exists for each built-in preset (idempotent),
    keyed by ``preset_key``. These are the shared, crawled-once companies."""
    from .company_presets import PRESETS
    from .models import Company

    have = {c.preset_key for c in db.scalars(select(Company).where(Company.preset_key.is_not(None)))}
    for p in PRESETS:
        if p.key in have:
            continue
        db.add(Company(
            preset_key=p.key, user_id=None, name=p.name, careers_url=p.careers_url,
            ats_type=p.ats_type, ats_token=p.ats_token, location_hint=p.location_hint,
        ))
    db.flush()


def _migrate_user_presets(db: Session) -> None:
    """One-time: a legacy per-user company that is really a preset becomes a
    subscription to the shared preset company, and the duplicate per-user row is
    deleted (its positions/match-results cascade; preset matches re-score next
    run). Idempotent — after the first pass no user company matches a preset."""
    from .company_presets import PRESETS
    from .models import Company, Subscription

    by_token = {(p.ats_type, p.ats_token): p.key for p in PRESETS if p.ats_token}
    by_name = {p.name.lower(): p.key for p in PRESETS}
    global_by_key = {
        c.preset_key: c for c in db.scalars(select(Company).where(Company.preset_key.is_not(None)))
    }
    legacy = db.scalars(
        select(Company).where(Company.user_id.is_not(None), Company.preset_key.is_(None))
    )
    for c in legacy:
        key = by_token.get((c.ats_type, c.ats_token)) or by_name.get((c.name or "").lower())
        shared = global_by_key.get(key) if key else None
        if shared is None:
            continue  # genuine custom company — leave as-is
        if not db.scalar(select(Subscription).where(
            Subscription.user_id == c.user_id, Subscription.company_id == shared.id
        )):
            db.add(Subscription(user_id=c.user_id, company_id=shared.id))
        db.delete(c)  # cascades its now-duplicate positions + match results
    db.flush()


def init_db() -> None:
    """Create all tables, bring an existing DB up to the current schema, seed
    preset companies, and run the one-time preset migration. Import models first
    so they register with the metadata."""
    from . import models  # noqa: F401

    models.Base.metadata.create_all(bind=engine)
    _reconcile_schema(engine)  # add columns/indexes create_all can't add to existing tables
    with session_scope() as db:
        seed_presets(db)
        _migrate_user_presets(db)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for use outside request handlers (scheduler, MCP, CLI)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
