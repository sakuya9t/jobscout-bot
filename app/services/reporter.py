"""Turn stored MatchResults into a ranked, user-facing daily report."""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Company, Interest, MatchResult, Position, User


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
) -> list[dict]:
    """Ranked list of strong matches for a user. ``min_score`` defaults to each
    interest's own threshold; pass a number to override globally. ``on_date``
    restricts to matches scored that UTC day (used by the daily Telegram push)."""
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

    report: list[dict] = []
    for match, pos, company in rows:
        threshold = min_score if min_score is not None else thresholds.get(match.interest_id, 70)
        if match.match_score < threshold:
            continue
        report.append(
            {
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
                "scored_at": match.created_at.isoformat() if match.created_at else None,
            }
        )
        if len(report) >= limit:
            break
    return report


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


def report_to_telegram(report: list[dict]) -> str:
    """Compact HTML for Telegram sendMessage (parse_mode=HTML)."""
    if not report:
        return "<b>JobScout</b>\nNo strong new matches today."
    out = [f"<b>JobScout — {len(report)} strong match(es) today</b>"]
    for m in report[:10]:
        # Escape everything sourced from scraped pages / LLM output so it can't
        # break the HTML message or inject Telegram markup.
        loc = f" · {html.escape(m['location'])}" if m["location"] else ""
        head = f"\n<b>{html.escape(m['title'])}</b> @ {html.escape(m['company'])}{loc}"
        score = f"\nMatch {m['match_score']}/100 · Win {m['win_probability']}%"
        why = f"\n{html.escape(m['reasoning'])}" if m["reasoning"] else ""
        link = f'\n<a href="{html.escape(m["url"], quote=True)}">View posting</a>' if m["url"] else ""
        out.append(head + score + why + link)
    return "".join(out)
