"""Trigger a run on demand and fetch the ranked report."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import JobListSnapshot, User
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
    limit: int = 5,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The default 'latest' view is **live** — a fresh ranked report over current
    MatchResults — so matches appear progressively as the background evaluator
    scores them, with ``pending`` counting what's left. Run stats and warnings come
    from the most recent saved snapshot (the last completed drain); the browsable
    frozen versions are served by ``/job-lists/runs`` and ``/job-lists/{id}``."""
    items = _strip(
        reporter.build_report(
            db, user, limit=_JOB_LIST_RESPONSE_LIMIT, include_below_threshold=True
        )
    )
    safe_limit = _safe_limit(limit)
    snapshot = db.scalar(
        select(JobListSnapshot)
        .where(JobListSnapshot.user_id == user.id)
        .order_by(JobListSnapshot.created_at.desc(), JobListSnapshot.id.desc())
        .limit(1)
    )
    return JobListOut(
        id=snapshot.id if snapshot else None,
        created_at=snapshot.created_at if snapshot else None,
        new_positions=snapshot.new_positions if snapshot else 0,
        scored=snapshot.scored if snapshot else 0,
        filtered=snapshot.filtered if snapshot else 0,
        errors=reporter.job_list_errors(snapshot) if snapshot else [],
        pending=matcher.count_pending(db, user),
        total=len(items),
        items=[MatchOut(**m) for m in items[:safe_limit]],
    )


@router.get("/job-lists/{snapshot_id}", response_model=JobListOut)
def get_job_list(
    snapshot_id: int,
    limit: int = 5,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    snapshot = db.get(JobListSnapshot, snapshot_id)
    if not snapshot or snapshot.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job list not found")
    return _job_list_out(snapshot, limit)


def _strip(report: list[dict]) -> list[dict]:
    """Drop reporter-only keys that aren't part of the MatchOut schema."""
    keep = {"position_id", "company", "title", "location", "url", "match_score",
            "win_probability", "reasoning", "strengths", "gaps", "below_threshold"}
    return [{k: v for k, v in m.items() if k in keep} for m in report]


def _safe_limit(limit: int) -> int:
    return min(max(limit, 1), _JOB_LIST_RESPONSE_LIMIT)


def _job_list_out(snapshot: JobListSnapshot, limit: int) -> JobListOut:
    items = reporter.job_list_items(snapshot)
    safe_limit = _safe_limit(limit)
    return JobListOut(
        id=snapshot.id,
        created_at=snapshot.created_at,
        new_positions=snapshot.new_positions,
        scored=snapshot.scored,
        filtered=snapshot.filtered,
        errors=reporter.job_list_errors(snapshot),
        total=len(items),
        items=[MatchOut(**m) for m in items[:safe_limit]],
    )
