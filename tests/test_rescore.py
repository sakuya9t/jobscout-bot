"""Unit tests for the per-position on-demand re-score (matcher.rescore_position) that
backs the detail page's "Re-evaluate" button. No network: the scoring client is faked
and a MatchResult is seeded directly so we can assert the row is overwritten in place
(on success) or left untouched (on failure)."""
from __future__ import annotations

import json

from sqlalchemy import select

from app import models
from app.db import session_scope
from app.services import matcher
from app.services.ollama_client import OllamaBudgetError, OllamaError


# ── LLM fakes (single-position batch scoring) ─────────────────────────────────
class GoodClient:
    """Returns one fresh single-verdict object (the re-score uses the non-batched
    scoring path, so there's no ``results`` wrapper / per-posting id)."""

    model = "fake-rescored"

    def __init__(self, score: int = 99):
        self.score = score

    def chat_json(self, system, user, schema, temperature=0.2, seed=None):
        return {
            "matches_requirements": True, "match_score": self.score,
            "win_probability": 70, "reasoning": "Re-scored fit.",
            "strengths": ["Python"], "gaps": ["No Go"],
            "score_breakdown": [
                {"label": "Skills overlap", "score": self.score, "rationale": "Strong."}
            ],
        }


class FailClient:
    model = "fake-fail"

    def chat_json(self, *a, **k):
        raise OllamaError("Ollama returned 500: server error")


class BudgetClient:
    model = "fake-budget"

    def chat_json(self, *a, **k):
        raise OllamaBudgetError("Ollama budget/quota exhausted (HTTP 402)")


class BoomClient:
    """Fails the test if scoring is attempted at all."""

    model = "fake-boom"

    def chat_json(self, *a, **k):
        raise AssertionError("the scoring model should not have been called")


class DriftClient:
    """Returns a verdict that drifts from the schema (no ``matches_requirements``, a
    malformed ``score_breakdown``) — the re-score should tolerate it and keep the core
    score rather than failing, since these models routinely drift even under `format`."""

    model = "fake-drift"

    def chat_json(self, system, user, schema, temperature=0.2, seed=None):
        return {
            "match_score": 91, "win_probability": 60, "reasoning": "Strong overlap.",
            "strengths": ["Python"], "gaps": [],
            "score_breakdown": [{"label": "Skills", "score": 999}],  # out of range → dropped
        }


class EmptyClient:
    """Returns JSON with no usable score — the re-score must NOT persist a hollow zero
    over the prior good score."""

    model = "fake-empty"

    def chat_json(self, system, user, schema, temperature=0.2, seed=None):
        return {"reasoning": "hmm"}


def test_score_one_schema_forces_supplementary_arrays():
    """The re-score constrains the model to emit strengths/gaps/score_breakdown (the
    plain schema leaves them optional, so the model skips them and Winning/Risks come
    back empty), plus each subscore's rationale (else the per-aspect description is blank)."""
    req = set(matcher.SCORE_ONE_SCHEMA.get("required", []))
    assert {"strengths", "gaps", "score_breakdown"} <= req
    assert {"match_score", "win_probability", "matches_requirements", "reasoning"} <= req
    subscore_req = set(matcher.SCORE_ONE_SCHEMA["$defs"]["MatchSubScore"]["required"])
    assert {"label", "score", "rationale"} <= subscore_req


def test_loose_breakdown_tolerates_key_and_shape_drift():
    """The breakdown must survive synonym keys (aspect/name/reason) and the object-keyed
    shape some models emit — otherwise it's dropped and the detail page falls back to the
    synthetic 'Estimated…' note."""
    # Synonym keys instead of label/score/rationale.
    bd = matcher._loose_breakdown([
        {"aspect": "Vertical experience", "value": 80, "reason": "You've done ads ranking."},
        {"name": "Skills overlap", "rating": "90%", "explanation": "Strong Python."},
    ])
    assert [(b.label, b.score) for b in bd] == [("Vertical experience", 80), ("Skills overlap", 90)]
    assert bd[0].rationale == "You've done ads ranking."

    # Object keyed by aspect name, with a nested dict value.
    bd2 = matcher._loose_breakdown({
        "Seniority fit": {"score": 70, "rationale": "Right level."},
        "Location fit": 100,  # bare scalar value
    })
    got = {b.label: (b.score, b.rationale) for b in bd2}
    assert got["Seniority fit"] == (70, "Right level.")
    assert got["Location fit"][0] == 100

    # Genuinely empty / unusable stays empty.
    assert matcher._loose_breakdown([]) == []
    assert matcher._loose_breakdown("nope") == []


def test_loose_verdict_tolerates_drift_and_rejects_empty():
    # Missing matches_requirements is inferred from the score.
    v = matcher._loose_verdict({"match_score": 80, "win_probability": 40})
    assert v is not None and v.matches_requirements is True and v.match_score == 80
    # A zero score with no flag is treated as not-a-match.
    v0 = matcher._loose_verdict({"match_score": 0, "reasoning": "nope"})
    assert v0 is not None and v0.matches_requirements is False
    # A malformed score_breakdown item must NOT wipe the good strengths/gaps: the bad
    # item is clamped/kept by label, and strengths survive (the bug that left
    # Winning/Risks empty was dropping all three arrays on any validation hiccup).
    vb = matcher._loose_verdict({
        "matches_requirements": True, "match_score": 90, "win_probability": 55,
        "reasoning": "ok", "strengths": ["You ship Python"], "gaps": ["No Kubernetes"],
        "score_breakdown": [
            {"label": "Skills", "score": 999},          # out-of-range score → clamped to 100
            {"score": 50},                               # no label → dropped
            {"label": "Seniority", "score": "n/a"},      # non-numeric score → 0
        ],
    })
    assert vb is not None and vb.match_score == 90
    assert vb.strengths == ["You ship Python"] and vb.gaps == ["No Kubernetes"]
    labels = {b.label: b.score for b in vb.score_breakdown}
    assert labels == {"Skills": 100, "Seniority": 0}  # bad item dropped, others kept
    # No usable score (or not even a dict) → None, so the caller keeps the prior result.
    assert matcher._loose_verdict({}) is None
    assert matcher._loose_verdict({"reasoning": "no score"}) is None
    assert matcher._loose_verdict("not a dict") is None


def _full_breakdown(vertical, skills, seniority, location, preferences):
    return [
        {"label": "Vertical experience", "score": vertical},
        {"label": "Skills overlap", "score": skills},
        {"label": "Seniority fit", "score": seniority},
        {"label": "Location fit", "score": location},
        {"label": "Preferences", "score": preferences},
    ]


def test_match_score_derived_from_full_breakdown_ignores_volatile_gestalt():
    """With a complete rubric breakdown, the headline is the fixed weighted average of the
    aspects — NOT the model's free-form match_score — so the same posting stops swinging
    between identical calls. Here every aspect is 80, so the headline is 80 regardless of
    the stray 35 the model emitted as its holistic number."""
    v = matcher._loose_verdict({
        "matches_requirements": True, "match_score": 35, "win_probability": 50,
        "reasoning": "ok", "score_breakdown": _full_breakdown(80, 80, 80, 80, 80),
    })
    assert v is not None and v.match_score == 80


def test_match_score_weights_vertical_experience_most():
    """The weighted average leans on vertical experience (0.35): vertical=100 with every
    other aspect 0 yields 35, confirming the rubric ordering is honored."""
    v = matcher._loose_verdict({
        "matches_requirements": True, "match_score": 99, "win_probability": 50,
        "reasoning": "ok", "score_breakdown": _full_breakdown(100, 0, 0, 0, 0),
    })
    assert v is not None and v.match_score == 35


def test_match_score_falls_back_when_breakdown_too_sparse():
    """One or two aspects can't carry a trustworthy weighted score, so the model's own
    number is kept rather than extrapolated from a fragment of the rubric."""
    v = matcher._loose_verdict({
        "matches_requirements": True, "match_score": 72, "win_probability": 40,
        "reasoning": "ok",
        "score_breakdown": [
            {"label": "Skills overlap", "score": 90},
            {"label": "Seniority fit", "score": 90},
        ],
    })
    assert v is not None and v.match_score == 72


def test_hard_requirement_violation_caps_derived_score():
    """A wrong-location role whose skill/experience aspects rate high must not surface a
    high green headline — matches_requirements=False caps it at _HARD_FAIL_CAP."""
    v = matcher._loose_verdict({
        "matches_requirements": False, "match_score": 88, "win_probability": 10,
        "reasoning": "Wrong location.", "score_breakdown": _full_breakdown(90, 90, 90, 0, 90),
    })
    assert v is not None and v.matches_requirements is False
    assert v.match_score <= matcher._HARD_FAIL_CAP


def test_scoring_call_uses_deterministic_options():
    """The scoring call runs at temperature 0 with the configured fixed seed so a single
    sample is reproducible — the other half of the headline-swing fix."""
    captured: dict = {}

    class CaptureClient:
        model = "fake-capture"

        def chat_json(self, system, user, schema, temperature=0.2, seed=None):
            captured["temperature"] = temperature
            captured["seed"] = seed
            return {
                "matches_requirements": True, "match_score": 80, "win_probability": 50,
                "reasoning": "ok", "strengths": ["Py"], "gaps": [],
            }

    with session_scope() as db:
        uid, pid = _seed(db, score=40)
    with session_scope() as db:
        matcher.rescore_position(
            db, db.get(models.User, uid), db.get(models.Position, pid), client=CaptureClient()
        )
    assert captured["temperature"] == matcher.settings.score_temperature == 0.0
    assert captured["seed"] == matcher.settings.score_seed


def _seed(db, *, score: int = 88, passed: bool = True, model: str = "fake-old") -> tuple[int, int]:
    """A user with an active resume + interest and ONE position already carrying a
    MatchResult, so a re-score has an existing row to overwrite."""
    user = models.User(email="r@x.com", hashed_password="h")
    db.add(user)
    db.flush()
    resume = models.Resume(
        user_id=user.id, filename="r.txt", content_text="Senior Python engineer", is_active=True
    )
    db.add(resume)
    company = models.Company(user_id=user.id, name="Acme", ats_type="greenhouse", ats_token="acme")
    db.add(company)
    db.flush()
    pos = models.Position(
        company_id=company.id, external_id="1", title="Backend Engineer",
        location="Remote", description="Build Python APIs",
    )
    db.add(pos)
    interest = models.Interest(
        user_id=user.id, label="be", title_keywords="backend", locations="remote",
        min_score=70, is_active=True,
    )
    db.add(interest)
    db.flush()
    db.add(models.MatchResult(
        user_id=user.id, position_id=pos.id, resume_id=resume.id, interest_id=interest.id,
        passed_filter=passed, match_score=score, win_probability=40, reasoning="old reasoning",
        strengths=json.dumps(["old strength"]), gaps=json.dumps(["old gap"]),
        score_breakdown=json.dumps([]), model=model, attempts=0,
    ))
    db.flush()
    return user.id, pos.id


def _matches(db, position_id: int) -> list[models.MatchResult]:
    return list(db.scalars(select(models.MatchResult).where(models.MatchResult.position_id == position_id)))


def test_rescore_overwrites_match_in_place():
    with session_scope() as db:
        uid, pid = _seed(db, score=40)
    with session_scope() as db:
        res = matcher.rescore_position(db, db.get(models.User, uid), db.get(models.Position, pid),
                                       client=GoodClient(99))
        assert res.scored == 1 and not res.errors
    with session_scope() as db:
        rows = _matches(db, pid)
        assert len(rows) == 1  # updated in place — no duplicate row
        m = rows[0]
        assert m.match_score == 99 and m.win_probability == 70 and m.passed_filter is True
        assert json.loads(m.strengths) == ["Python"] and json.loads(m.gaps) == ["No Go"]
        assert json.loads(m.score_breakdown)[0]["label"] == "Skills overlap"
        assert m.model == "fake-rescored"


def test_rescore_preserves_existing_score_when_llm_fails():
    """A transient LLM error must NOT clobber the good row with an error-marker — the
    user keeps their previous score and just retries."""
    with session_scope() as db:
        uid, pid = _seed(db, score=88, model="fake-old")
    with session_scope() as db:
        res = matcher.rescore_position(db, db.get(models.User, uid), db.get(models.Position, pid),
                                       client=FailClient())
        assert res.scored == 0 and res.errors
    with session_scope() as db:
        rows = _matches(db, pid)
        assert len(rows) == 1
        m = rows[0]
        assert m.match_score == 88 and m.passed_filter is True and m.model == "fake-old"
        assert json.loads(m.strengths) == ["old strength"]
        assert m.model != matcher.ERROR_MODEL  # no error-marker written


def test_rescore_preserves_existing_score_on_budget_exhaustion():
    with session_scope() as db:
        uid, pid = _seed(db, score=75)
    with session_scope() as db:
        res = matcher.rescore_position(db, db.get(models.User, uid), db.get(models.Position, pid),
                                       client=BudgetClient())
        assert res.budget_exhausted and res.scored == 0 and res.errors
    with session_scope() as db:
        m = _matches(db, pid)[0]
        assert m.match_score == 75 and m.model == "fake-old"


def test_rescore_tolerates_schema_drift():
    """A drifting-but-usable verdict (missing flag, out-of-range subscore) still re-scores
    the row in place — and a malformed subscore must NOT discard the good strengths."""
    with session_scope() as db:
        uid, pid = _seed(db, score=40)
    with session_scope() as db:
        res = matcher.rescore_position(db, db.get(models.User, uid), db.get(models.Position, pid),
                                       client=DriftClient())
        assert res.scored == 1 and not res.errors
    with session_scope() as db:
        m = _matches(db, pid)[0]
        assert m.match_score == 91 and m.passed_filter is True  # flag inferred from score
        assert json.loads(m.strengths) == ["Python"]            # strengths survived
        assert json.loads(m.score_breakdown) == [{"label": "Skills", "score": 100, "rationale": ""}]


def test_rescore_keeps_prior_score_on_empty_response():
    with session_scope() as db:
        uid, pid = _seed(db, score=88)
    with session_scope() as db:
        res = matcher.rescore_position(db, db.get(models.User, uid), db.get(models.Position, pid),
                                       client=EmptyClient())
        assert res.scored == 0 and res.errors
    with session_scope() as db:
        m = _matches(db, pid)[0]
        assert m.match_score == 88 and m.model == "fake-old"  # untouched


def test_rescore_requires_active_resume():
    with session_scope() as db:
        uid, pid = _seed(db)
        for r in db.scalars(select(models.Resume).where(models.Resume.user_id == uid)):
            r.is_active = False
    with session_scope() as db:
        res = matcher.rescore_position(db, db.get(models.User, uid), db.get(models.Position, pid),
                                       client=BoomClient())
        assert res.scored == 0 and res.errors
    with session_scope() as db:  # existing row untouched
        assert _matches(db, pid)[0].match_score == 88
