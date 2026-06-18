"""Tests for the periodic scoring queue (services/scoring_queue.py) and the bounded
drain (evaluator.drain_queue) that the `jobscout run-scoring` cron runs. No network:
the LLM clients are faked via llm.clients_for_user and the scraper is never touched
(the drain only scores an already-seeded backlog)."""
from __future__ import annotations

from datetime import timedelta

from app import models
from app.db import session_scope
from app.services import evaluator, matcher, scoring_queue
from app.timeutil import utcnow


# ── LLM fakes (single-position batch) ─────────────────────────────────────────
class GoodClient:
    model = "fake-good"

    def chat_json(self, system, user, schema, temperature=0.2):
        n = user.count("### Posting ")
        return {
            "results": [
                {
                    "id": i + 1, "matches_requirements": True, "match_score": 88,
                    "win_probability": 50, "reasoning": "Fit.", "strengths": ["Py"], "gaps": [],
                }
                for i in range(n)
            ]
        }


class FilterPass:
    model = "fake-filter"

    def chat_text(self, system, user, temperature=0.4):
        return "YES"


def _seed_user(db, *, email="u@x.com") -> int:
    """A user with one described, scoreable position and one active interest → a
    backlog of exactly one (position × interest) pair."""
    user = models.User(email=email, hashed_password="h")
    db.add(user)
    db.flush()
    db.add(models.Resume(user_id=user.id, filename="r.txt",
                         content_text="Senior Python engineer", is_active=True))
    company = models.Company(user_id=user.id, name="Acme", ats_type="greenhouse", ats_token="acme")
    db.add(company)
    db.flush()
    db.add(models.Position(company_id=company.id, external_id="1", title="Backend Engineer",
                           location="Remote", description="Build Python APIs"))
    db.add(models.Interest(user_id=user.id, label="be", title_keywords="backend",
                           locations="remote", min_score=70, is_active=True))
    db.flush()
    return user.id


def _job(db, user_id) -> models.ScoringJob | None:
    return db.scalar(matcher.select(models.ScoringJob).where(models.ScoringJob.user_id == user_id))


# ── enqueue ───────────────────────────────────────────────────────────────────
def test_enqueue_is_idempotent():
    with session_scope() as db:
        uid = _seed_user(db)
        scoring_queue.enqueue(db, uid)
        scoring_queue.enqueue(db, uid)  # second call must not create a 2nd row
        rows = list(db.scalars(matcher.select(models.ScoringJob).where(models.ScoringJob.user_id == uid)))
        assert len(rows) == 1
        assert rows[0].state == "pending"


def test_enqueue_does_not_disturb_a_running_job():
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow()))
        db.commit()
        scoring_queue.enqueue(db, uid)  # a worker owns it — leave it alone
        assert _job(db, uid).state == "running"


# ── reconcile ─────────────────────────────────────────────────────────────────
def test_reconcile_enqueues_only_users_with_backlog():
    with session_scope() as db:
        busy = _seed_user(db, email="busy@x.com")          # has a pending pair
        idle = models.User(email="idle@x.com", hashed_password="h")  # nothing to score
        db.add(idle)
        db.flush()
        idle_id = idle.id
        n = scoring_queue.reconcile(db)
        assert n == 1
        assert _job(db, busy).state == "pending"
        assert _job(db, idle_id) is None


def test_reconcile_reclaims_stale_running_jobs():
    with session_scope() as db:
        uid = _seed_user(db)
        stale = utcnow() - timedelta(minutes=10_000)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=stale))
        db.commit()
        scoring_queue.reconcile(db)
        # The crashed worker's row is reclaimed to pending (then left pending since the
        # user still has a backlog).
        assert _job(db, uid).state == "pending"


# ── claim ─────────────────────────────────────────────────────────────────────
def test_claim_one_flips_pending_to_running():
    with session_scope() as db:
        uid = _seed_user(db)
        scoring_queue.enqueue(db, uid)
    with session_scope() as db:
        claimed = scoring_queue.claim_one(db)
        assert claimed == uid
        assert _job(db, uid).state == "running"
        assert _job(db, uid).claimed_at is not None
        assert scoring_queue.claim_one(db) is None  # nothing left to claim


# ── finalize ──────────────────────────────────────────────────────────────────
def test_finalize_marks_done_when_backlog_empty():
    with session_scope() as db:
        # No interests/positions → count_pending is 0 → finalize settles to done.
        user = models.User(email="done@x.com", hashed_password="h")
        db.add(user)
        db.flush()
        uid = user.id
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow()))
        db.commit()
        scoring_queue.finalize(db, user, matcher.RunResult())
        assert _job(db, uid).state == "done"
        assert _job(db, uid).finished_at is not None


def test_finalize_rearms_when_drain_did_not_run():
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow()))
        db.commit()
        res = matcher.RunResult()
        res.did_run = False  # another in-process drain holds the score lock
        scoring_queue.finalize(db, db.get(models.User, uid), res)
        assert _job(db, uid).state == "pending"


# ── drain_queue (end-to-end) ──────────────────────────────────────────────────
def test_drain_queue_scores_backlog_and_marks_done(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm.clients_for_user", lambda db, user: (GoodClient(), FilterPass())
    )
    with session_scope() as db:
        uid = _seed_user(db)
        scoring_queue.reconcile(db)

    summary = evaluator.drain_queue(max_workers=1, budget_seconds=0)

    assert summary.users == 1
    assert summary.scored == 1
    assert summary.failed == 0
    with session_scope() as db:
        user = db.get(models.User, uid)
        assert matcher.count_pending(db, user) == 0
        assert _job(db, uid).state == "done"
        # A passing MatchResult and a job-list snapshot were written.
        assert db.scalar(matcher.select(models.MatchResult).where(models.MatchResult.passed_filter == True)) is not None  # noqa: E712
        assert db.scalar(matcher.select(models.JobListSnapshot)) is not None


def test_drain_queue_marks_error_on_failure(monkeypatch):
    class Boom:
        model = "boom"

        def chat_text(self, *a, **k):
            raise RuntimeError("kaboom")

        def chat_json(self, *a, **k):
            raise RuntimeError("kaboom")

    # Make score_to_completion itself raise (not just an LLM warning) by blowing up
    # client construction, so the drain hits its except path and marks the job errored.
    def _explode(db, user):
        raise RuntimeError("no clients")

    monkeypatch.setattr("app.services.matcher.llm.clients_for_user", _explode)
    with session_scope() as db:
        uid = _seed_user(db)
        scoring_queue.reconcile(db)

    summary = evaluator.drain_queue(max_workers=1, budget_seconds=0)

    assert summary.failed == 1
    with session_scope() as db:
        job = _job(db, uid)
        assert job.state == "error"
        assert job.last_error


# ── manual refresh routes through the queue ───────────────────────────────────
def test_ensure_running_enqueues_the_user(monkeypatch):
    """The on-demand entry point (`/api/run` -> ensure_running) now ENQUEUES rather
    than draining inline, so the bounded pool — not the request — controls how many
    users score at once. Drive workers off so we observe just the enqueue."""
    monkeypatch.setattr(evaluator, "ensure_draining", lambda: None)
    with session_scope() as db:
        uid = _seed_user(db)

    evaluator.ensure_running(uid)

    with session_scope() as db:
        job = _job(db, uid)
        assert job is not None and job.state == "pending"
        # Nothing was scored inline — the queue, not the request, owns the work.
        assert matcher.count_pending(db, db.get(models.User, uid)) == 1


def test_active_users_reflects_queue_state():
    with session_scope() as db:
        a = models.User(email="a@x.com", hashed_password="h")
        b = models.User(email="b@x.com", hashed_password="h")
        c = models.User(email="c@x.com", hashed_password="h")
        db.add_all([a, b, c])
        db.flush()
        db.add(models.ScoringJob(user_id=a.id, state="pending"))
        db.add(models.ScoringJob(user_id=b.id, state="running", claimed_at=utcnow()))
        db.add(models.ScoringJob(user_id=c.id, state="done", finished_at=utcnow()))
        db.commit()
        ids = {a.id, b.id}
    assert evaluator.active_users() == ids  # pending + running, not done


# ── cross-process race tolerance (_flush_match) ───────────────────────────────
def test_flush_match_tolerates_duplicate_pair():
    """A second MatchResult for the same (user, position, resume, interest) — what a
    concurrent drain would write — is swallowed, leaving the first row intact and the
    session still usable."""
    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)
        resume = db.scalar(matcher.select(models.Resume).where(models.Resume.user_id == uid))
        interest = db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid))
        pos = db.scalar(matcher.select(models.Position))

        def _row():
            return models.MatchResult(
                user_id=uid, position_id=pos.id, resume_id=resume.id, interest_id=interest.id,
                passed_filter=True, match_score=80, win_probability=40, model="m",
            )

        assert matcher._flush_match(db, _row()) is True
        assert matcher._flush_match(db, _row()) is False  # duplicate pair → skipped
        db.commit()
        rows = list(db.scalars(matcher.select(models.MatchResult).where(models.MatchResult.position_id == pos.id)))
        assert len(rows) == 1
