"""The application-history view: ``GET /api/applications/history`` lists every
position the user marked applied — newest first — no matter whether it still
matches an active interest, was screened out, or has any stored match at all. We
seed Application rows (and optionally matches) directly, so there's no network or
scoring involved; the point is the application-centric query, not how a row got
applied."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.auth import create_access_token
from app.db import session_scope
from app.main import app


def _seed_user(db, email="u@x.com"):
    user = models.User(email=email, hashed_password="h")
    db.add(user); db.flush()
    db.add(models.Resume(user_id=user.id, filename="r.txt", content_text="x", is_active=True))
    company = models.Company(user_id=user.id, name="Acme", ats_type="greenhouse", ats_token="acme")
    db.add(company); db.flush()
    interest = models.Interest(user_id=user.id, label="be", title_keywords="backend",
                               locations="remote", min_score=70, is_active=True)
    db.add(interest); db.flush()
    return user.id, company.id, interest.id


def _add_position(db, company_id, ext, title, *, removed=False):
    pos = models.Position(
        company_id=company_id, external_id=ext, title=title, description="d",
        url=f"https://boards.greenhouse.io/acme/jobs/{ext}",
        removed_at=(models.utcnow() if removed else None),
    )
    db.add(pos); db.flush()
    return pos.id


def _match(db, uid, pid, interest_id, *, passed=True, score=80):
    rid = db.scalar(select(models.Resume.id).where(models.Resume.user_id == uid))
    db.add(models.MatchResult(
        user_id=uid, position_id=pid, resume_id=rid, interest_id=interest_id,
        passed_filter=passed, match_score=score, win_probability=score,
        reasoning="r", model="good"))
    db.flush()


def _apply(db, uid, pid, *, days_ago=0):
    db.add(models.Application(
        user_id=uid, position_id=pid,
        applied_at=models.utcnow() - timedelta(days=days_ago)))
    db.flush()


def _auth(uid):
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


def test_history_lists_applied_newest_first_regardless_of_match_state():
    """Newest application first, and an applied posting shows even when its only match
    was a filter-rejection or was deleted outright (no stored match) — the cases the
    match-centric job list can't surface. A non-applied position never appears."""
    with session_scope() as db:
        uid, cid, iid = _seed_user(db)
        # A: a strong, passing match, applied longest ago.
        a = _add_position(db, cid, "a", "Match A")
        _match(db, uid, a, iid, passed=True, score=90)
        _apply(db, uid, a, days_ago=2)
        # B: a non-matching (filter-rejected) match, applied more recently.
        b = _add_position(db, cid, "b", "Rejected B")
        _match(db, uid, b, iid, passed=False, score=0)
        _apply(db, uid, b, days_ago=1)
        # C: no stored match at all (e.g. its match was dropped by an interest edit),
        # applied most recently.
        c = _add_position(db, cid, "c", "Unscored C")
        _apply(db, uid, c, days_ago=0)
        # D: a matched position the user never applied to — must NOT appear.
        d = _add_position(db, cid, "d", "Not applied D")
        _match(db, uid, d, iid, passed=True, score=88)

    with TestClient(app) as client:
        body = client.get("/api/applications/history", headers=_auth(uid)).json()

    assert body["total"] == 3  # D, never applied, is not counted
    rows = body["items"]
    assert [r["title"] for r in rows] == ["Unscored C", "Rejected B", "Match A"]
    by_title = {r["title"]: r for r in rows}
    assert by_title["Match A"]["match_score"] == 90 and by_title["Match A"]["non_matching"] is False
    assert by_title["Rejected B"]["non_matching"] is True  # present despite not matching
    assert by_title["Unscored C"]["match_score"] is None   # no stored match
    assert by_title["Unscored C"]["non_matching"] is False
    assert all(r["status"] == "applied" and r["applied_at"] for r in rows)


def test_history_includes_closed_postings():
    """A posting the user applied to and that later left the board still appears,
    badged removed — the history shouldn't lose it just because it closed."""
    with session_scope() as db:
        uid, cid, iid = _seed_user(db)
        pid = _add_position(db, cid, "x", "Closed role", removed=True)
        _match(db, uid, pid, iid, passed=True, score=75)
        _apply(db, uid, pid)

    with TestClient(app) as client:
        body = client.get("/api/applications/history", headers=_auth(uid)).json()

    assert body["total"] == 1
    rows = body["items"]
    assert len(rows) == 1 and rows[0]["removed"] is True and rows[0]["position_id"] == pid


def test_history_is_isolated_per_user():
    with session_scope() as db:
        uid_a, cid_a, iid_a = _seed_user(db, email="a@x.com")
        pid = _add_position(db, cid_a, "a", "A's role")
        _match(db, uid_a, pid, iid_a)
        _apply(db, uid_a, pid)
        uid_b, _, _ = _seed_user(db, email="b@x.com")

    with TestClient(app) as client:
        a_body = client.get("/api/applications/history", headers=_auth(uid_a)).json()
        assert a_body["total"] == 1 and len(a_body["items"]) == 1
        b_body = client.get("/api/applications/history", headers=_auth(uid_b)).json()
        assert b_body["total"] == 0 and b_body["items"] == []


def test_history_is_paginated_newest_first():
    """``limit``/``offset`` page the history (newest application first); ``total`` is
    the full count regardless of the page window."""
    with session_scope() as db:
        uid, cid, iid = _seed_user(db)
        # Five applications, applied 4..0 days ago, so newest-first is e4, e3, e2, e1, e0.
        for n in range(5):
            pid = _add_position(db, cid, f"e{n}", f"Role {n}")
            _match(db, uid, pid, iid)
            _apply(db, uid, pid, days_ago=4 - n)

    with TestClient(app) as client:
        h = _auth(uid)
        p1 = client.get("/api/applications/history?limit=2&offset=0", headers=h).json()
        p2 = client.get("/api/applications/history?limit=2&offset=2", headers=h).json()
        p3 = client.get("/api/applications/history?limit=2&offset=4", headers=h).json()

    assert (p1["total"], p2["total"], p3["total"]) == (5, 5, 5)
    assert [r["title"] for r in p1["items"]] == ["Role 4", "Role 3"]
    assert [r["title"] for r in p2["items"]] == ["Role 2", "Role 1"]
    assert [r["title"] for r in p3["items"]] == ["Role 0"]  # last page, partial


def test_history_reapply_moves_to_top():
    """History is ordered by the most recent apply click: marking B after A puts B on
    top, and cancelling A then re-applying it bumps A back above B."""
    with session_scope() as db:
        uid, cid, iid = _seed_user(db)
        a = _add_position(db, cid, "a", "Role A"); _match(db, uid, a, iid)
        b = _add_position(db, cid, "b", "Role B"); _match(db, uid, b, iid)

    def order(client, h):
        return [r["position_id"] for r in client.get("/api/applications/history", headers=h).json()["items"]]

    with TestClient(app) as client:
        h = _auth(uid)
        assert client.post(f"/api/applications/{a}", headers=h).status_code == 201
        assert client.post(f"/api/applications/{b}", headers=h).status_code == 201
        assert order(client, h) == [b, a]  # B applied last → top

        # Cancel A, then re-apply it: its apply time is now the newest → back to the top.
        assert client.delete(f"/api/applications/{a}", headers=h).status_code == 204
        assert client.post(f"/api/applications/{a}", headers=h).status_code == 201
        assert order(client, h) == [a, b]


def test_history_requires_auth():
    with TestClient(app) as client:
        assert client.get("/api/applications/history").status_code == 401
