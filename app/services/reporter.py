"""Turn stored MatchResults into a ranked, user-facing daily report."""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Application,
    ApplicationKit,
    Company,
    Interest,
    JobListSnapshot,
    MatchResult,
    Position,
    User,
)
from ..timeutil import utcnow
from .matcher import ERROR_MODEL


JOB_LIST_SNAPSHOT_LIMIT = 500
# Categories the dashboard job list can request (see build_job_list).
JOB_CATEGORIES = ("matching", "all")


def _loads(value: str | None) -> list[str]:
    try:
        return json.loads(value) if value else []
    except json.JSONDecodeError:
        return []


def _listed_at(pos: Position) -> datetime | None:
    """The job's effective 'listed' date: the ATS-reported post date when we have
    one, else when our crawler first saw it (so undated sources still get a date)."""
    return pos.posted_at or pos.first_seen_at


def _match_row(match: MatchResult, pos: Position, company: Company, *,
               below_threshold: bool, non_matching: bool = False) -> dict:
    """Build one dashboard/report row dict from a (match, position, company)."""
    listed = _listed_at(pos)
    return {
        "position_id": pos.id,
        "company": company.name,
        "title": pos.title,
        "location": pos.location,
        "url": pos.url,
        "match_score": match.match_score,
        "win_probability": match.win_probability,
        "reasoning": match.reasoning,
        "strengths": _loads(match.strengths),
        "gaps": _loads(match.gaps),
        "below_threshold": below_threshold,
        "non_matching": non_matching,
        "scored_at": match.created_at.isoformat() if match.created_at else None,
        "listed_at": listed.isoformat() if listed else None,
        "applied": False,  # overlaid per-user by tag_applied (live, not stored)
        # Application-kit status overlaid by tag_kit_status (live, not stored):
        # None (no kit yet) | "generating" | "ok" | "error".
        "kit_status": None,
    }


def tag_applied(db: Session, user: User, items: list[dict]) -> list[dict]:
    """Set each item's ``applied`` flag from the user's Application rows. Done at
    render time (not stored) so the toggle reflects the latest status even on a
    frozen saved list. Mutates and returns ``items``."""
    ids = [m["position_id"] for m in items if m.get("position_id") is not None]
    if not ids:
        return items
    applied = set(
        db.scalars(
            select(Application.position_id).where(
                Application.user_id == user.id, Application.position_id.in_(ids)
            )
        )
    )
    for m in items:
        m["applied"] = m.get("position_id") in applied
    return items


def tag_kit_status(db: Session, user: User, items: list[dict]) -> list[dict]:
    """Set each item's ``kit_status`` from the user's ApplicationKit rows (None when
    no kit has been requested for that position). Overlaid live at render time — like
    ``tag_applied`` — so the job list reflects the current status even on a frozen
    saved list. Mutates and returns ``items``."""
    ids = [m["position_id"] for m in items if m.get("position_id") is not None]
    if not ids:
        return items
    statuses = dict(
        db.execute(
            select(ApplicationKit.position_id, ApplicationKit.status).where(
                ApplicationKit.user_id == user.id, ApplicationKit.position_id.in_(ids)
            )
        ).all()
    )
    for m in items:
        m["kit_status"] = statuses.get(m.get("position_id"))
    return items


def build_report(
    db: Session,
    user: User,
    min_score: int | None = None,
    limit: int = 50,
    on_date: date | None = None,
    min_results: int = 0,
    include_below_threshold: bool = False,
) -> list[dict]:
    """Ranked list of matches for a user, scored highest-first. Only postings the
    LLM judged a match (``passed_filter``) are included, gated by ``min_score``
    (defaults to each interest's own threshold; pass a number to override).

    ``min_results`` guarantees a minimum number of rows even when fewer clear the
    threshold, by backfilling with the next-highest-scoring matches (each tagged
    ``below_threshold``). The web dashboard uses this so the user always sees
    something; the Telegram push leaves it 0 so it stays threshold-only.
    ``include_below_threshold`` returns the whole ranked list, preserving the
    below-threshold tag; this powers the dashboard job-list history.
    ``on_date`` restricts to matches scored that UTC day (the daily push)."""
    stmt = (
        select(MatchResult, Position, Company)
        .join(Position, MatchResult.position_id == Position.id)
        .join(Company, Position.company_id == Company.id)
        .where(MatchResult.user_id == user.id, MatchResult.passed_filter == True)  # noqa: E712
        .order_by(MatchResult.match_score.desc(), MatchResult.win_probability.desc())
    )
    if on_date is not None:
        # created_at is stored naive-UTC; filter by the [midnight, +1 day) UTC
        # range in SQL rather than a Python-side local .date() comparison, which
        # silently drops/duplicates rows whenever the server isn't on UTC.
        day_start = datetime(on_date.year, on_date.month, on_date.day)
        stmt = stmt.where(
            MatchResult.created_at >= day_start,
            MatchResult.created_at < day_start + timedelta(days=1),
        )
    rows = db.execute(stmt).all()

    # Per-interest thresholds, for when min_score isn't overridden.
    thresholds = {i.id: i.min_score for i in db.scalars(select(Interest).where(Interest.user_id == user.id))}

    all_ranked: list[dict] = []
    above: list[dict] = []
    below: list[dict] = []
    for match, pos, company in rows:
        threshold = min_score if min_score is not None else thresholds.get(match.interest_id, 70)
        is_below = match.match_score < threshold
        row = _match_row(match, pos, company, below_threshold=is_below)
        all_ranked.append(row)
        (below if is_below else above).append(row)

    if include_below_threshold:
        return all_ranked[:limit]

    report = above[:limit]
    if len(report) < min_results:
        # Backfill with the highest-scoring below-threshold matches (rows are
        # already score-ordered) so the dashboard always shows something.
        report = report + below[: min_results - len(report)]
    return report


def build_job_list(
    db: Session,
    user: User,
    *,
    category: str = "matching",
    min_score: int = 0,
    min_win: int = 0,
    posted_within_days: int | None = None,
    company_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """One page of the dashboard job list, plus the total count in that category.

    ``category="matching"`` returns only jobs the AI judged a fit (passed_filter),
    below-threshold ones tagged. ``category="all"`` additionally includes
    non-matching jobs — filter-rejected and keyword-excluded — but never transient
    scoring-error markers. Matches rank first (by score), then non-matching by
    recency. Pagination is server-side via ``limit``/``offset``.

    ``min_score`` / ``min_win`` keep only matches scoring at least that (0 = off).
    They gate real matches; non-matching rows have no meaningful score, so in the
    "all" category they're shown regardless of the thresholds.

    ``posted_within_days`` keeps only postings listed within that many days
    (None/<=0 = off), by the job's effective listed date — the ATS post date, or
    our first-seen date when the source carries none. The filter is applied to the
    query before counting/paging, so ``total`` and the top-N (e.g. top-5) selection
    both reflect only the date-filtered pool.

    ``company_id`` (None = all) narrows to one company's postings, applied — like
    the date filter — before counting/paging so the total reflects the scope."""
    base = (
        select(MatchResult, Position, Company)
        .join(Position, MatchResult.position_id == Position.id)
        .join(Company, Position.company_id == Company.id)
        .where(MatchResult.user_id == user.id)
    )
    if company_id is not None:
        base = base.where(Position.company_id == company_id)
    if posted_within_days and posted_within_days > 0:
        cutoff = utcnow() - timedelta(days=posted_within_days)
        base = base.where(
            func.coalesce(Position.posted_at, Position.first_seen_at) >= cutoff
        )
    if category == "all":
        # Everything except error markers (those are failures, not "non-matching").
        base = base.where(MatchResult.model.is_distinct_from(ERROR_MODEL))
        if min_score > 0 or min_win > 0:
            base = base.where(
                (MatchResult.passed_filter == False)  # noqa: E712 — non-matching: exempt
                | ((MatchResult.match_score >= min_score) & (MatchResult.win_probability >= min_win))
            )
    else:
        base = base.where(MatchResult.passed_filter == True)  # noqa: E712
        if min_score > 0:
            base = base.where(MatchResult.match_score >= min_score)
        if min_win > 0:
            base = base.where(MatchResult.win_probability >= min_win)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    rows = db.execute(
        base.order_by(
            MatchResult.passed_filter.desc(),
            MatchResult.match_score.desc(),
            MatchResult.win_probability.desc(),
            MatchResult.created_at.desc(),
        )
        .offset(max(0, offset))
        .limit(limit)
    ).all()

    thresholds = {i.id: i.min_score for i in db.scalars(select(Interest).where(Interest.user_id == user.id))}
    items = []
    for match, pos, company in rows:
        non_matching = not match.passed_filter
        below = (not non_matching) and match.match_score < thresholds.get(match.interest_id, 70)
        items.append(_match_row(match, pos, company, below_threshold=below, non_matching=non_matching))
    tag_applied(db, user, items)
    tag_kit_status(db, user, items)
    return items, total


def position_visible(db: Session, user: User, position_id: int) -> bool:
    """Whether ``position_id`` is in the user's job list — i.e. it has been scored
    for them (a MatchResult exists). The same gate the 'Mark applied' action uses,
    so the detail page and kit generation only ever touch positions the user can see."""
    return db.scalar(
        select(MatchResult.id)
        .where(MatchResult.user_id == user.id, MatchResult.position_id == position_id)
        .limit(1)
    ) is not None


def build_position_detail(db: Session, user: User, position_id: int) -> dict | None:
    """The detail-page payload for one position: its posting fields plus the user's
    best stored match (a passing match, highest score, else the highest-scoring
    row). Returns None when the position isn't in the user's job list. ``applied``
    is overlaid live like the job list."""
    row = db.execute(
        select(MatchResult, Position, Company)
        .join(Position, MatchResult.position_id == Position.id)
        .join(Company, Position.company_id == Company.id)
        .where(MatchResult.user_id == user.id, MatchResult.position_id == position_id)
        .order_by(
            MatchResult.passed_filter.desc(),
            MatchResult.match_score.desc(),
            MatchResult.win_probability.desc(),
        )
        .limit(1)
    ).first()
    if row is None:
        return None
    match, pos, company = row
    listed = _listed_at(pos)
    detail = {
        "position_id": pos.id,
        "company": company.name,
        "title": pos.title,
        "location": pos.location,
        "department": pos.department,
        "employment_type": pos.employment_type,
        "url": pos.url,
        "description": pos.description,
        "listed_at": listed.isoformat() if listed else None,
        "match_score": match.match_score,
        "win_probability": match.win_probability,
        "reasoning": match.reasoning,
        "strengths": _loads(match.strengths),
        "gaps": _loads(match.gaps),
        "non_matching": not match.passed_filter,
        "applied": False,
    }
    tag_applied(db, user, [detail])
    return detail


def _match_out_payload(match: dict) -> dict:
    keep = {
        "position_id", "company", "title", "location", "url", "match_score",
        "win_probability", "reasoning", "strengths", "gaps", "below_threshold",
        "non_matching", "listed_at",
    }
    return {k: v for k, v in match.items() if k in keep}


def record_job_list_snapshot(db: Session, user: User, result) -> JobListSnapshot:
    """Persist the dashboard's ranked job-list after a scan completes."""
    report = build_report(
        db,
        user,
        limit=JOB_LIST_SNAPSHOT_LIMIT,
        include_below_threshold=True,
    )
    snapshot = JobListSnapshot(
        user_id=user.id,
        new_positions=result.new_positions,
        scored=result.scored,
        filtered=getattr(result, "filtered", 0),
        errors=json.dumps(result.errors),
        items_json=json.dumps([_match_out_payload(m) for m in report]),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def job_list_items(snapshot: JobListSnapshot) -> list[dict]:
    try:
        data = json.loads(snapshot.items_json or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def filter_items_posted_within(items: list[dict], posted_within_days: int | None) -> list[dict]:
    """Keep stored job-list items whose ``listed_at`` falls within the window.
    No-op when the window is off (None/<=0). Items missing/with an unparseable
    ``listed_at`` (e.g. snapshots saved before this field existed) are kept rather
    than silently dropped."""
    if not posted_within_days or posted_within_days <= 0:
        return items
    cutoff = utcnow() - timedelta(days=posted_within_days)
    kept = []
    for item in items:
        raw = item.get("listed_at")
        try:
            listed = datetime.fromisoformat(raw) if raw else None
        except (TypeError, ValueError):
            listed = None
        if listed is None or listed >= cutoff:
            kept.append(item)
    return kept


def filter_items_by_company(items: list[dict], company_name: str | None) -> list[dict]:
    """Keep stored job-list items for one company (exact ``company`` name match).
    No-op when ``company_name`` is falsy. Used on the frozen-snapshot path, whose
    items carry the company name (not its id) — the caller resolves id -> name."""
    if not company_name:
        return items
    return [m for m in items if m.get("company") == company_name]


# Substrings that mark a run warning as an LLM-communication failure (vs a scrape
# or config warning) — the matcher tags these with "Filtering failed"/"Scoring
# failed", and budget/quota exhaustion names Ollama. Used to raise the dashboard's
# "LLM requests failed" banner.
_LLM_FAILURE_HINTS = ("filtering failed", "scoring failed", "ollama", "quota", "budget")


def is_llm_failure(message: str) -> bool:
    low = (message or "").lower()
    return any(hint in low for hint in _LLM_FAILURE_HINTS)


def llm_failed(errors: list[str]) -> bool:
    """Whether any run warning indicates an LLM request failed (bad key/model,
    unreachable provider, or exhausted quota)."""
    return any(is_llm_failure(e) for e in errors)


def job_list_errors(snapshot: JobListSnapshot) -> list[str]:
    try:
        data = json.loads(snapshot.errors or "[]")
    except json.JSONDecodeError:
        return []
    return [_normalize_snapshot_error(str(item)) for item in data] if isinstance(data, list) else []


def _normalize_snapshot_error(message: str) -> str:
    """Older snapshots used 'scoring cap' for the candidate-screening budget.
    Normalize on display so historical runs match the current wording."""
    if message.startswith("Reached this run's scoring cap "):
        return (
            message
            .replace("scoring cap", "candidate evaluation cap", 1)
            .replace("remain unscored", "remain unevaluated", 1)
        )
    return message


def report_to_markdown(user_email: str, report: list[dict]) -> str:
    if not report:
        return f"# JobScout — {datetime.now():%Y-%m-%d}\n\nNo strong new matches today."
    lines = [f"# JobScout daily report — {datetime.now():%Y-%m-%d}", f"_for {user_email}_", ""]
    for i, m in enumerate(report, 1):
        lines.append(f"## {i}. {m['title']} — {m['company']}")
        loc = f" · {m['location']}" if m["location"] else ""
        lines.append(f"**Match {m['match_score']}/100 · Win chance {m['win_probability']}%**{loc}")
        if m["url"]:
            lines.append(f"<{m['url']}>")
        if m["reasoning"]:
            lines.append(f"\n{m['reasoning']}")
        if m["strengths"]:
            lines.append("\n**Why you're a strong fit:**")
            lines += [f"- {s}" for s in m["strengths"]]
        if m["gaps"]:
            lines.append("\n**Watch-outs:**")
            lines += [f"- {g}" for g in m["gaps"]]
        lines.append("")
    return "\n".join(lines)


def report_to_telegram(report: list[dict], errors: list[str] | None = None) -> str:
    """Compact HTML for Telegram sendMessage (parse_mode=HTML). ``errors`` are
    the run's warnings, appended so a user whose scrape/API key is broken sees
    *why* there are no matches instead of a silent empty report."""
    out: list[str] = []
    if report:
        out.append(f"<b>JobScout — {len(report)} strong match(es) today</b>")
        for m in report[:10]:
            # Escape everything sourced from scraped pages / LLM output so it
            # can't break the HTML message or inject Telegram markup.
            loc = f" · {html.escape(m['location'])}" if m["location"] else ""
            head = f"\n<b>{html.escape(m['title'])}</b> @ {html.escape(m['company'])}{loc}"
            score = f"\nMatch {m['match_score']}/100 · Win {m['win_probability']}%"
            why = f"\n{html.escape(m['reasoning'])}" if m["reasoning"] else ""
            link = f'\n<a href="{html.escape(m["url"], quote=True)}">View posting</a>' if m["url"] else ""
            out.append(head + score + why + link)
    else:
        out.append("<b>JobScout</b>\nNo strong new matches today.")
    if errors:
        out.append("\n\n<b>⚠️ Run warnings</b>")
        # No re-slice here: RunResult already caps at 5 unique messages plus an
        # "… and N more" tail — slicing to 5 would cut exactly that tail line.
        for e in errors:
            out.append(f"\n• {html.escape(e)}")
    return "".join(out)
