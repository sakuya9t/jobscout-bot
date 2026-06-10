"""Trigger a run on demand and fetch the ranked report."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..db import get_db
from ..models import User
from ..schemas import MatchOut, RunSummary
from ..services import matcher, reporter

router = APIRouter(prefix="/api", tags=["run"])


@router.post("/run", response_model=RunSummary)
def run_now(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> RunSummary:
    """Scrape this user's companies and score new positions immediately."""
    result = matcher.run_for_user(db, user)
    db.commit()
    report = reporter.build_report(db, user, limit=10)
    return RunSummary(
        new_positions=result.new_positions,
        scored=result.scored,
        errors=result.errors,
        top_matches=[MatchOut(**m) for m in _strip(report)],
    )


@router.get("/report", response_model=list[MatchOut])
def get_report(
    min_score: int | None = None,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return [MatchOut(**m) for m in _strip(reporter.build_report(db, user, min_score=min_score, limit=limit))]


def _strip(report: list[dict]) -> list[dict]:
    """Drop reporter-only keys that aren't part of the MatchOut schema."""
    keep = {"position_id", "company", "title", "location", "url", "match_score",
            "win_probability", "reasoning", "strengths", "gaps"}
    return [{k: v for k, v in m.items() if k in keep} for m in report]
