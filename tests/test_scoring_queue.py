"""Tests for the periodic scoring queue (services/scoring_queue.py) and the bounded
drain (evaluator.drain_queue) that the `jobscout run-scoring` cron runs. No network:
the LLM clients are faked via llm.clients_for_user and the scraper is never touched
(the drain only scores an already-seeded backlog)."""
from __future__ import annotations

import time
from datetime import timedelta

from fastapi.testclient import TestClient

from app import models
from app.config import settings
from app.db import session_scope
from app.main import app
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


class IncompleteScore:
    """Stage-2 client that returns an empty batch — every posting is 'missing', so
    each pair gets an error-marker (the incomplete-batch failure mode)."""
    model = "fake-incomplete"

    def chat_json(self, system, user, schema, temperature=0.2):
        return {"results": []}


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


def test_enqueue_force_rearms_a_running_job():
    """A user-initiated re-run (force=True) re-arms even a 'running' row — recovering a
    job orphaned by a dead worker — and resets attempts, unlike the default enqueue."""
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow(), attempts=4))
        db.commit()
        scoring_queue.enqueue(db, uid, force=True)
        job = _job(db, uid)
        assert job.state == "pending" and job.attempts == 0


def test_ensure_running_recovers_an_orphaned_running_job(monkeypatch):
    """The dashboard 'Recalculate' path (ensure_running) re-arms an orphaned 'running'
    row so the click actually restarts scoring — regression: a stale running row left
    enqueue a no-op and nothing drained."""
    monkeypatch.setattr(evaluator, "ensure_draining", lambda: None)
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow(), attempts=4))
        db.commit()
    evaluator.ensure_running(uid)
    with session_scope() as db:
        assert _job(db, uid).state == "pending"


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


def test_reconcile_leaves_a_fresh_running_job_mid_run():
    """Mid-run (cron/dispatch) a recently-claimed 'running' row may belong to a live
    in-process worker, so the default reconcile must NOT yank it — only stale ones."""
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow()))
        db.commit()
        scoring_queue.reconcile(db)
        assert _job(db, uid).state == "running"


def test_reconcile_reclaims_fresh_running_job_at_startup():
    """Regression: after a restart, a job claimed seconds before the crash sits 'running'
    but its worker is gone. At startup no worker exists yet, so reclaim_all_running pulls
    it back to 'pending' to be re-drained — otherwise the dashboard shows
    'Evaluating — N still to score' that never progresses until the stale window elapses."""
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow()))
        db.commit()
        scoring_queue.reconcile(db, reclaim_all_running=True)
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


# ── per-user run budget (stop cleanly instead of being killed mid-drain) ───────
def test_score_to_completion_stops_at_deadline_and_rearms(monkeypatch):
    """A run past its wall-clock budget stops before scoring, flags time_exhausted, and
    leaves the backlog intact — so finalize re-arms the queue row to pending (continue
    next run) rather than the worker overrunning the job timeout and stranding it."""
    monkeypatch.setattr("app.services.llm.clients_for_user", lambda db, u: (GoodClient(), FilterPass()))
    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)
        res = matcher.score_to_completion(db, user, deadline=time.monotonic() - 1)  # already past
        assert res.time_exhausted is True and res.scored == 0
        assert matcher.count_pending(db, user) == 1  # nothing drained

        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow()))
        db.commit()
        scoring_queue.finalize(db, user, res)
        assert _job(db, uid).state == "pending"  # re-armed, not done/error


def test_score_to_completion_drains_within_budget(monkeypatch):
    """A generous deadline drains the whole backlog (time_exhausted stays False)."""
    monkeypatch.setattr("app.services.llm.clients_for_user", lambda db, u: (GoodClient(), FilterPass()))
    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)
        res = matcher.score_to_completion(db, user, deadline=time.monotonic() + 60)
        assert res.time_exhausted is False and res.scored == 1
        assert matcher.count_pending(db, user) == 0


def test_ensure_running_enqueues_without_draining_when_workers_off(monkeypatch):
    """With in-process workers off, ensure_running only enqueues (durably) and does not
    spawn a drain — the `jobscout run-scoring` cron is the backstop."""
    assert settings.background_workers_enabled is False  # the test env (conftest)
    drained = []
    monkeypatch.setattr(evaluator, "ensure_draining", lambda: drained.append(1))
    with session_scope() as db:
        uid = _seed_user(db)
    evaluator.ensure_running(uid)
    assert drained == []  # did not spawn an in-process drain
    with session_scope() as db:
        assert _job(db, uid).state == "pending"


# ── consumer endpoint (local-runnable / testable) ─────────────────────────────
def test_run_scoring_endpoint_requires_cron_secret(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    with TestClient(app) as c:
        assert c.post("/api/cron/run-scoring").status_code == 503  # disabled until set
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    with TestClient(app) as c:
        assert c.post("/api/cron/run-scoring", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_run_scoring_endpoint_drains_the_queue(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr("app.services.llm.clients_for_user", lambda db, u: (GoodClient(), FilterPass()))
    with session_scope() as db:
        uid = _seed_user(db)  # one scoreable pair, not yet enqueued
    with TestClient(app) as c:
        r = c.post("/api/cron/run-scoring", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    body = r.json()
    assert body["enqueued"] == 1 and body["scored"] == 1 and body["more_pending"] is False
    with session_scope() as db:
        assert matcher.count_pending(db, db.get(models.User, uid)) == 0


def test_run_scoring_endpoint_reports_backlog_remaining(monkeypatch):
    """If the budget leaves work pending, the endpoint reports more_pending=True so the
    next run continues the drain."""
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    # Drain is a no-op, so the reconciled backlog stays pending after the call.
    monkeypatch.setattr(evaluator, "drain_queue", lambda **k: evaluator.DrainSummary())
    with session_scope() as db:
        _seed_user(db)
    with TestClient(app) as c:
        r = c.post("/api/cron/run-scoring", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json()["more_pending"] is True


def test_run_daily_endpoint_publishes_backlog(monkeypatch):
    """After the scrape, run-daily enqueues users with a backlog (scoring is deferred)."""
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.setattr(matcher, "scrape_for_all_users", lambda: {})  # skip real scraping
    with session_scope() as db:
        _seed_user(db)  # has a backlog → should be enqueued
    with TestClient(app) as c:
        r = c.get("/api/cron/run-daily", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json()["enqueued"] == 1


# ── bounded retry for failed scoring pairs ────────────────────────────────────
def test_failed_pair_retries_then_goes_terminal(monkeypatch):
    """A failed (posting, interest) pair is retried up to score_max_attempts, staying
    in the backlog meanwhile, then goes terminal (drops out) instead of looping forever."""
    monkeypatch.setattr(settings, "score_max_attempts", 2)
    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)

        # Attempt 1 fails → marker attempts=1, still retryable (1 < 2) → stays pending.
        matcher.score_to_completion(db, user, client=IncompleteScore(), filter_client=FilterPass())
        m = db.scalar(matcher.select(models.MatchResult))
        assert m.model == matcher.ERROR_MODEL and m.attempts == 1
        assert matcher.count_pending(db, user) == 1

        # Attempt 2 fails → attempts=2 → terminal (2 >= 2) → leaves the backlog.
        matcher.score_to_completion(db, user, client=IncompleteScore(), filter_client=FilterPass())
        m = db.scalar(matcher.select(models.MatchResult))
        assert m.attempts == 2
        assert matcher.count_pending(db, user) == 0

        # A further run does not touch it (terminal): attempts stays 2.
        matcher.score_to_completion(db, user, client=IncompleteScore(), filter_client=FilterPass())
        assert db.scalar(matcher.select(models.MatchResult)).attempts == 2


def test_failed_pair_recovers_on_retry(monkeypatch):
    """A retry that succeeds converts the pair's error-marker into a real result
    in place (no duplicate row), and the retry counter resets."""
    monkeypatch.setattr(settings, "score_max_attempts", 3)
    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)

        matcher.score_to_completion(db, user, client=IncompleteScore(), filter_client=FilterPass())
        assert db.scalar(matcher.select(models.MatchResult)).model == matcher.ERROR_MODEL

        matcher.score_to_completion(db, user, client=GoodClient(), filter_client=FilterPass())
        rows = list(db.scalars(matcher.select(models.MatchResult)))
        assert len(rows) == 1                       # same row upserted, not duplicated
        assert rows[0].model == "fake-good" and rows[0].passed_filter is True
        assert rows[0].attempts == 0
        assert matcher.count_pending(db, user) == 0


def test_clear_failed_markers_reenqueues(monkeypatch):
    """The escape hatch (jobscout retry-failed → clear_failed_markers) puts a terminal
    pair back in the backlog for another round."""
    monkeypatch.setattr(settings, "score_max_attempts", 1)  # fail once → terminal
    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)
        matcher.score_to_completion(db, user, client=IncompleteScore(), filter_client=FilterPass())
        assert matcher.count_pending(db, user) == 0     # terminal (attempts 1 >= 1)
        assert matcher.clear_failed_markers(db, uid) == 1
        assert matcher.count_pending(db, user) == 1     # back in the backlog


# ── queue-level retry/backoff + auto-resolve (orphaned/parked jobs) ────────────
def test_reconcile_retries_orphan_under_cap(monkeypatch):
    monkeypatch.setattr(settings, "scoring_job_max_attempts", 3)
    stale = utcnow() - timedelta(minutes=10_000)
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=stale, attempts=1))
        db.commit()
        scoring_queue.reconcile(db)
        assert _job(db, uid).state == "pending"  # under the cap → reclaimed to retry


def test_reconcile_parks_orphan_at_attempt_cap(monkeypatch):
    monkeypatch.setattr(settings, "scoring_job_max_attempts", 3)
    monkeypatch.setattr(settings, "scoring_job_retry_cooldown_minutes", 60)
    stale = utcnow() - timedelta(minutes=10_000)
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=stale, attempts=3))
        db.commit()
        scoring_queue.reconcile(db)
        job = _job(db, uid)
        # used up its retries → parked (kicked out of the active queue), not requeued now
        assert job.state == "error" and job.finished_at is not None


def test_reconcile_requeues_parked_job_after_cooldown(monkeypatch):
    monkeypatch.setattr(settings, "scoring_job_max_attempts", 3)
    monkeypatch.setattr(settings, "scoring_job_retry_cooldown_minutes", 60)
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="error", attempts=3,
                                 finished_at=utcnow() - timedelta(minutes=10_000)))
        db.commit()
        scoring_queue.reconcile(db)
        job = _job(db, uid)
        # cooldown elapsed → auto-resolved: requeued with a fresh retry budget
        assert job.state == "pending" and job.attempts == 0


def test_reconcile_keeps_parked_job_during_cooldown(monkeypatch):
    monkeypatch.setattr(settings, "scoring_job_max_attempts", 3)
    monkeypatch.setattr(settings, "scoring_job_retry_cooldown_minutes", 60)
    with session_scope() as db:
        uid = _seed_user(db)
        db.add(models.ScoringJob(user_id=uid, state="error", attempts=3, finished_at=utcnow()))
        db.commit()
        scoring_queue.reconcile(db)
        assert _job(db, uid).state == "error"  # still cooling down → stays parked


def test_finalize_resets_attempts_on_clean_end():
    with session_scope() as db:
        # No interests/positions → count_pending 0 → finalize settles to done + reset.
        user = models.User(email="fin@x.com", hashed_password="h")
        db.add(user); db.flush()
        uid = user.id
        db.add(models.ScoringJob(user_id=uid, state="running", claimed_at=utcnow(), attempts=4))
        db.commit()
        scoring_queue.finalize(db, db.get(models.User, uid), matcher.RunResult())
        job = _job(db, uid)
        assert job.state == "done" and job.attempts == 0


def _add_error_marker(db, uid, *, attempts, created_at):
    resume = db.scalar(matcher.select(models.Resume).where(models.Resume.user_id == uid))
    interest = db.scalar(matcher.select(models.Interest).where(models.Interest.user_id == uid))
    pos = db.scalar(matcher.select(models.Position))
    db.add(models.MatchResult(
        user_id=uid, position_id=pos.id, resume_id=resume.id, interest_id=interest.id,
        passed_filter=False, match_score=0, win_probability=0,
        model=matcher.ERROR_MODEL, attempts=attempts, created_at=created_at))
    db.commit()


def test_reconcile_clears_expired_failed_markers(monkeypatch):
    """Per-posting auto-resolve: a terminal marker older than the TTL is cleared so the
    pair re-enters the backlog (never stuck forever)."""
    monkeypatch.setattr(settings, "score_marker_retry_after_hours", 24)
    monkeypatch.setattr(settings, "score_max_attempts", 3)
    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)
        _add_error_marker(db, uid, attempts=3, created_at=utcnow() - timedelta(hours=48))
        assert matcher.count_pending(db, user) == 0  # terminal marker settles the pair
        scoring_queue.reconcile(db)
        assert matcher.count_pending(db, user) == 1  # stale marker swept → back in backlog


def test_reconcile_keeps_recent_failed_markers(monkeypatch):
    monkeypatch.setattr(settings, "score_marker_retry_after_hours", 24)
    monkeypatch.setattr(settings, "score_max_attempts", 3)
    with session_scope() as db:
        uid = _seed_user(db)
        user = db.get(models.User, uid)
        _add_error_marker(db, uid, attempts=3, created_at=utcnow())  # fresh terminal marker
        scoring_queue.reconcile(db)
        assert matcher.count_pending(db, user) == 0  # within TTL → kept (not retried yet)


# ── Scoring-queue trace table (services/scoring_log.py + models.ScoringEvent) ──
def _events(db, uid=None):
    from app.services import scoring_log

    scoring_log.flush()  # the writer is a background thread — drain before reading
    q = matcher.select(models.ScoringEvent)
    if uid is not None:
        q = q.where(models.ScoringEvent.user_id == uid)
    return list(db.scalars(q.order_by(models.ScoringEvent.id)))


def test_trace_records_full_drain_lifecycle(monkeypatch):
    """A clean drain leaves an ordered, queryable trail: enqueue -> claim -> drain ->
    finalize(done). This is the trace that makes a flaky stop diagnosable after the fact."""
    monkeypatch.setattr(settings, "log_scoring_events", True)
    monkeypatch.setattr(
        "app.services.llm.clients_for_user", lambda db, user: (GoodClient(), FilterPass())
    )
    with session_scope() as db:
        uid = _seed_user(db)
        scoring_queue.reconcile(db)

    evaluator.drain_queue(max_workers=1, budget_seconds=0)

    with session_scope() as db:
        events = _events(db, uid)
        kinds = [e.event for e in events]
        assert "enqueue" in kinds and "claim" in kinds
        assert "drain" in kinds and "finalize" in kinds
        # The trail ends on a clean finish.
        finalize = [e for e in events if e.event == "finalize"][-1]
        assert finalize.state_to == "done"
        # Every event is attributed to a thread (for following one worker's trail).
        assert all(e.worker for e in events)


def test_trace_records_error_event(monkeypatch):
    """A drain that raises records an `error` transition — the stop point you grep for."""
    monkeypatch.setattr(settings, "log_scoring_events", True)

    def _explode(db, user):
        raise RuntimeError("no clients")

    monkeypatch.setattr("app.services.matcher.llm.clients_for_user", _explode)
    with session_scope() as db:
        uid = _seed_user(db)
        scoring_queue.reconcile(db)

    evaluator.drain_queue(max_workers=1, budget_seconds=0)

    with session_scope() as db:
        errors = [e for e in _events(db, uid) if e.event == "error"]
        assert errors and errors[-1].state_to == "error"
        assert "no clients" in (errors[-1].detail or "")


def test_trace_can_be_disabled(monkeypatch):
    """JOBSCOUT_LOG_SCORING_EVENTS=0 turns the table off entirely (no rows written)."""
    monkeypatch.setattr(settings, "log_scoring_events", False)
    with session_scope() as db:
        uid = _seed_user(db)
        scoring_queue.enqueue(db, uid, force=True)
        assert _events(db, uid) == []
