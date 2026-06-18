"""MCP server exposing JobScout to external agents (openclaw / hermes / etc).

Runs over stdio. The acting user is resolved from a bearer token in the
``JOBSCOUT_MCP_TOKEN`` env var (mint one by logging in via the HTTP API and
copying the access_token, or with ``jobscout token <email>``). Every tool is
scoped to that user, mirroring the HTTP API's per-user isolation.

Run:  JOBSCOUT_MCP_TOKEN=<token> python -m app.mcp_server
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .auth import authenticate_token
from .db import init_db, session_scope
from .models import Application, Company, Interest, Position, User
from .schemas import CompanyIn, InterestIn
from .services import matcher, reporter

mcp = FastMCP("jobscout")


def _current_user(db) -> User:
    token = os.environ.get("JOBSCOUT_MCP_TOKEN", "")
    user = authenticate_token(token, db) if token else None
    if user is None:
        raise ValueError(
            "Unauthorized: set JOBSCOUT_MCP_TOKEN to a valid user access token."
        )
    return user


@mcp.tool()
def list_companies() -> list[dict]:
    """List the companies on the current user's watch-list."""
    with session_scope() as db:
        user = _current_user(db)
        return [
            {"id": c.id, "name": c.name, "careers_url": c.careers_url,
             "ats_type": c.ats_type, "is_active": c.is_active}
            for c in user.companies
        ]


@mcp.tool()
def add_company(
    name: str,
    careers_url: str | None = None,
    ats_type: str = "auto",
    ats_token: str | None = None,
    location_hint: str | None = None,
) -> dict:
    """Add a company to watch. ATS (greenhouse/lever/ashby) is auto-detected from
    the careers_url unless you specify ats_type/ats_token explicitly."""
    payload = CompanyIn(
        name=name, careers_url=careers_url, ats_type=ats_type,
        ats_token=ats_token, location_hint=location_hint,
    )
    with session_scope() as db:
        user = _current_user(db)
        company = Company(user_id=user.id, **payload.model_dump())
        db.add(company)
        try:
            db.flush()
        except IntegrityError as exc:
            # Same unique (user_id, name) constraint the HTTP router returns 409 for.
            db.rollback()
            raise ValueError(f"Company {name!r} is already on your list") from exc
        return {"id": company.id, "name": company.name}


@mcp.tool()
def remove_company(company_id: int) -> dict:
    """Remove a company (and its scraped positions) from the watch-list."""
    with session_scope() as db:
        user = _current_user(db)
        company = db.get(Company, company_id)
        if not company or company.user_id != user.id:
            raise ValueError("Company not found")
        db.delete(company)
        return {"removed": company_id}


@mcp.tool()
def list_interests() -> list[dict]:
    """List the current user's role/requirement profiles."""
    with session_scope() as db:
        user = _current_user(db)
        return [
            {"id": i.id, "label": i.label, "title_keywords": i.title_keywords,
             "locations": i.locations, "min_score": i.min_score, "notes": i.notes}
            for i in user.interests
        ]


@mcp.tool()
def add_interest(
    label: str,
    title_keywords: str | None = None,
    locations: str | None = None,
    seniority: str | None = None,
    employment_type: str | None = None,
    exclude_keywords: str | None = None,
    notes: str | None = None,
    min_score: int = 70,
) -> dict:
    """Add a role/requirement profile. title_keywords/locations/exclude_keywords
    are comma-separated; notes is free text used to steer the LLM match decision."""
    payload = InterestIn(
        label=label, title_keywords=title_keywords, locations=locations,
        seniority=seniority, employment_type=employment_type,
        exclude_keywords=exclude_keywords, notes=notes, min_score=min_score,
    )
    with session_scope() as db:
        user = _current_user(db)
        interest = Interest(user_id=user.id, **payload.model_dump())
        db.add(interest)
        db.flush()
        return {"id": interest.id, "label": interest.label}


@mcp.tool()
def list_resumes() -> list[dict]:
    """List the current user's resumes and which one is active for scoring."""
    with session_scope() as db:
        user = _current_user(db)
        return [{"id": r.id, "filename": r.filename, "is_active": r.is_active} for r in user.resumes]


@mcp.tool()
def run_daily_scan() -> dict:
    """Scrape the user's companies, detect new positions, and score them with the
    Ollama model against the active resume. Returns counts, warnings, and the top matches."""
    from .services import crawler

    crawler.crawl_presets()  # refresh the shared preset catalog before scoring
    with session_scope() as db:
        user = _current_user(db)
        result = matcher.run_for_user(db, user)
        reporter.record_job_list_snapshot(db, user, result)
        db.flush()
        top = reporter.build_report(db, user, limit=10)
        return {
            "new_positions": result.new_positions,
            "scored": result.scored,
            "errors": result.errors,
            "top_matches": top,
        }


@mcp.tool()
def get_report(min_score: int | None = None, limit: int = 25) -> list[dict]:
    """Return the current user's ranked matches with scores, win probability, and
    the reasoning for why each is a strong fit."""
    with session_scope() as db:
        user = _current_user(db)
        return reporter.build_report(db, user, min_score=min_score, limit=limit)


@mcp.tool()
def get_position(position_id: int) -> dict:
    """Fetch a single scraped position's full details (title, location, description, url)."""
    with session_scope() as db:
        user = _current_user(db)
        pos = db.get(Position, position_id)
        if not pos or pos.company.user_id != user.id:
            raise ValueError("Position not found")
        # A posting that left the board is hidden unless this user applied to it.
        if pos.removed_at is not None and not db.scalar(
            select(Application.id).where(
                Application.user_id == user.id, Application.position_id == pos.id
            ).limit(1)
        ):
            raise ValueError("Position not found")
        return {
            "id": pos.id, "company": pos.company.name, "title": pos.title,
            "location": pos.location, "department": pos.department, "url": pos.url,
            "employment_type": pos.employment_type, "description": pos.description,
        }


def main() -> None:
    init_db()
    mcp.run()


if __name__ == "__main__":
    main()
