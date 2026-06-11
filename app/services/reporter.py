"""Turn stored MatchResults into a ranked, user-facing daily report."""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Company, Interest, JobListSnapshot, MatchResult, Position, User


JOB_LIST_SNAPSHOT_LIMIT = 500


def _loads(value: str | None) -> list[str]:
    try:
        return json.loads(value) if value else []
    except json.JSONDecodeError:
        return []


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

    def _row(match, pos, company, below: bool) -> dict:
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
            "below_threshold": below,
            "scored_at": match.created_at.isoformat() if match.created_at else None,
        }

    all_ranked: list[dict] = []
    above: list[dict] = []
    below: list[dict] = []
    for match, pos, company in rows:
        threshold = min_score if min_score is not None else thresholds.get(match.interest_id, 70)
        is_below = match.match_score < threshold
        row = _row(match, pos, company, is_below)
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


def _match_out_payload(match: dict) -> dict:
    keep = {
        "position_id", "company", "title", "location", "url", "match_score",
        "win_probability", "reasoning", "strengths", "gaps", "below_threshold",
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
