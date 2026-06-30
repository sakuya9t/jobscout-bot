"""Trigger a run on demand and fetch the ranked report."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import Company, JobListSnapshot, User
from ..schemas import EvaluationStatus, JobListOut, JobListRunOut, MatchOut, RunSummary
from ..services import evaluator, matcher, reporter

router = APIRouter(prefix="/api", tags=["run"])
_JOB_LIST_RESPONSE_LIMIT = 500


@router.post("/run", response_model=RunSummary)
def run_now(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> RunSummary:
    """Scrape this user's companies synchronously, then hand scoring to the
    background evaluator and return immediately. The dashboard polls
    ``/api/evaluation/status`` and watches ``pending`` drain to zero."""
    result = matcher.scrape_only(db, user)
    db.commit()
    result.finalize_errors()
    evaluator.ensure_running(user.id)
    # Show whatever's already scored from prior runs while the backlog evaluates.
    report = reporter.build_report(db, user, limit=10, min_results=5)
    return RunSummary(
        new_positions=result.new_positions,
        scored=result.scored,
        errors=result.errors,
        pending=matcher.count_pending(db, user),
        top_matches=[MatchOut(**m) for m in _strip(report)],
    )


@router.get("/evaluation/status", response_model=EvaluationStatus)
def evaluation_status(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> EvaluationStatus:
    """How many positions are still queued for background scoring, and whether a
    drain is actively running. Polled by the dashboard backlog indicator."""
    return EvaluationStatus(
        pending=matcher.count_pending(db, user),
        in_progress=user.id in evaluator.active_users(),
    )


@router.get("/report", response_model=list[MatchOut])
def get_report(
    min_score: int | None = None,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return [
        MatchOut(**m)
        for m in _strip(reporter.build_report(db, user, min_score=min_score, limit=limit, min_results=5))
    ]


@router.get("/job-lists/runs", response_model=list[JobListRunOut])
def get_job_list_runs(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    snapshots = list(
        db.scalars(
            select(JobListSnapshot)
            .where(JobListSnapshot.user_id == user.id)
            .order_by(JobListSnapshot.created_at.desc(), JobListSnapshot.id.desc())
        )
    )
    return [
        JobListRunOut(
            id=snapshot.id,
            created_at=snapshot.created_at,
            new_positions=snapshot.new_positions,
            scored=snapshot.scored,
            filtered=snapshot.filtered,
            total=len(reporter.job_list_items(snapshot)),
            has_errors=bool(reporter.job_list_errors(snapshot)),
        )
        for snapshot in snapshots
    ]


@router.get("/job-lists/latest", response_model=JobListOut)
def get_latest_job_list(
    limit: int = 10,
    offset: int = 0,
    category: str = "matching",
    min_score: int = 0,
    min_win: int = 0,
    posted_within_days: int | None = None,
    company_id: int | None = None,
    sort: str = "match",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The default 'latest' view is **live** — a fresh ranked page over current
    MatchResults — so matches appear progressively as the background evaluator
    scores them, with ``pending`` counting what's left. ``category`` selects
    matching-only or all (incl. non-matching) jobs; ``min_score``/``min_win`` keep
    only matches at or above those thresholds; ``posted_within_days`` keeps only
    recently-listed jobs (None = all); ``company_id`` narrows to one watch-list
    company (None = all); ``sort`` orders by best-match (default) or salary
    (``salary_desc``/``salary_asc``); ``limit``/``offset`` paginate. Run stats and
    warnings come from the most recent saved snapshot (the last completed drain);
    frozen versions are served by ``/job-lists/{id}``."""
    items, total = reporter.build_job_list(
        db, user, category=_category(category),
        min_score=max(0, min_score), min_win=max(0, min_win),
        posted_within_days=posted_within_days, company_id=company_id,
        sort=_sort(sort), limit=_safe_limit(limit), offset=max(0, offset),
    )
    snapshot = db.scalar(
        select(JobListSnapshot)
        .where(JobListSnapshot.user_id == user.id)
        .order_by(JobListSnapshot.created_at.desc(), JobListSnapshot.id.desc())
        .limit(1)
    )
    errors = reporter.job_list_errors(snapshot) if snapshot else []
    return JobListOut(
        id=snapshot.id if snapshot else None,
        created_at=snapshot.created_at if snapshot else None,
        new_positions=snapshot.new_positions if snapshot else 0,
        scored=snapshot.scored if snapshot else 0,
        filtered=snapshot.filtered if snapshot else 0,
        errors=errors,
        pending=matcher.count_pending(db, user),
        llm_error=reporter.llm_failed(errors),
        total=total,
        items=[MatchOut(**m) for m in items],
    )


@router.get("/job-lists/{snapshot_id}", response_model=JobListOut)
def get_job_list(
    snapshot_id: int,
    limit: int = 10,
    offset: int = 0,
    category: str = "matching",
    min_score: int = 0,
    min_win: int = 0,
    posted_within_days: int | None = None,
    company_id: int | None = None,
    sort: str = "match",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A frozen saved version. Snapshots only store matching jobs, so the
    'all'/non-matching category has nothing extra to show here — pagination just
    pages over the stored matches, after the score/win, post-date and company
    filters are applied. ``sort`` re-orders the stored rows (best-match or salary)."""
    snapshot = db.get(JobListSnapshot, snapshot_id)
    if not snapshot or snapshot.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job list not found")
    all_items = reporter.job_list_items(snapshot)
    all_items = reporter.filter_items_posted_within(all_items, posted_within_days)
    if company_id is not None:
        # Stored items carry the company name, not its id — resolve id -> name. An
        # unknown id matches nothing (empty page), never another company's rows.
        company = db.get(Company, company_id)
        all_items = reporter.filter_items_by_company(all_items, company.name) if company else []
    if min_score > 0 or min_win > 0:
        all_items = [
            m for m in all_items
            if m.get("match_score", 0) >= min_score and m.get("win_probability", 0) >= min_win
        ]
    # Overlay live applied + removed state onto the frozen rows, then drop applied
    # postings (they move to the Application History view) and any that have since left
    # the board — the same rules the live list enforces in SQL — before paging so
    # counts stay consistent.
    reporter.tag_applied(db, user, all_items)
    reporter.tag_removed(db, user, all_items)
    all_items = [m for m in all_items if not m.get("applied") and not m.get("removed")]
    all_items = reporter.sort_items_by_salary(all_items, _sort(sort))
    start = max(0, offset)
    page = all_items[start : start + _safe_limit(limit)]
    reporter.tag_kit_status(db, user, page)
    errors = reporter.job_list_errors(snapshot)
    return JobListOut(
        id=snapshot.id,
        created_at=snapshot.created_at,
        new_positions=snapshot.new_positions,
        scored=snapshot.scored,
        filtered=snapshot.filtered,
        errors=errors,
        llm_error=reporter.llm_failed(errors),
        total=len(all_items),
        items=[MatchOut(**m) for m in page],
    )


def _strip(report: list[dict]) -> list[dict]:
    """Drop reporter-only keys that aren't part of the MatchOut schema."""
    keep = {"position_id", "company", "title", "location", "url", "match_score",
            "win_probability", "reasoning", "strengths", "gaps", "below_threshold",
            "non_matching", "listed_at"}
    return [{k: v for k, v in m.items() if k in keep} for m in report]


def _safe_limit(limit: int) -> int:
    return min(max(limit, 1), _JOB_LIST_RESPONSE_LIMIT)


def _category(value: str) -> str:
    """Clamp the requested job-list category to a known one (default matching)."""
    return value if value in reporter.JOB_CATEGORIES else "matching"


_SORTS = {"match", "salary_desc", "salary_asc"}


def _sort(value: str) -> str:
    """Clamp the requested job-list sort to a known one (default best-match)."""
    return value if value in _SORTS else "match"
