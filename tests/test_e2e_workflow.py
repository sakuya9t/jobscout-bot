"""End-to-end workflow tests for the whole candidate journey:

    register/login -> upload resume -> add company + interests -> run a scan ->
    see ranked matches in the job list -> open a position's detail -> generate
    an application kit.

These drive the *real HTTP API* through a Starlette ``TestClient`` (cookie session,
the same one the dashboard uses — ``cookie_secure`` is False in tests), so each test
is a true slice of the live request path, not a unit call. The suite is deliberately
split: one test per key step (each re-onboarding via the shared helpers so it stands
alone) plus one ``test_full_workflow_end_to_end`` that walks the entire journey.

Cost control — nothing hits the network or a real LLM:
  * ``app.services.scraper.scrape_company`` is replaced with a fixed list of postings.
  * ``app.services.llm.clients_for_user`` (the single point where both the matcher and
    the kit generator resolve their model clients) returns one ``FakeLLM`` that branches
    on the request to serve cheap-filter, scoring, and kit calls.

Determinism — scoring and kit generation are normally backgrounded (a thread pool /
the queue drain). ``conftest`` disables the in-process pool, so after the request
enqueues the work we drive it synchronously: ``evaluator.drain_queue`` for scoring and
``kit_worker._run`` for the kit. That's the exact code the background worker runs, just
on the test thread so results are ready to assert.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import evaluator, kit_worker, matcher
from app.services import scraper as scraper_mod


# ── One fake that models "the LLM" for the whole workflow ─────────────────────
class FakeLLM:
    """Stands in for both model clients ``clients_for_user`` returns. It serves every
    LLM call the journey makes by branching on the request:

      * ``chat_text`` — the cheap free-text yes/no filter (returns a global YES so
        every posting survives to scoring), except the cover-letter call (system
        prompt mentions "cover"), which returns letter text.
      * ``chat_json`` — keyed on the requested JSON schema: batched scoring
        (``results``), the kit's tailored-resume call (``resume_markdown``), the kit's
        role analysis (``looking_for``), or a single scoring verdict otherwise.
    """

    model = "fake-e2e"

    def __init__(self, score: int = 88):
        self.score = score

    def chat_text(self, system, user, temperature=0.4):
        if "cover" in system.lower():
            return "Dear hiring team, I'm excited to apply for this role. …"
        return "YES — plausible fit"

    def _verdict(self, i: int) -> dict:
        return {
            "id": i + 1,
            "matches_requirements": True,
            "match_score": self.score,
            "win_probability": 50,
            "reasoning": "Strong overlap with the candidate's Python background.",
            "strengths": ["Python"],
            "gaps": [],
        }

    def chat_json(self, system, user, schema, temperature=0.2):
        props = schema.get("properties", {})
        if "results" in props:  # batched scoring: one verdict per "### Posting N" block
            n = user.count("### Posting ")
            return {"results": [self._verdict(i) for i in range(n)]}
        if "resume_markdown" in props:  # kit step 3: tailored resume
            return {
                "resume_markdown": "# Jane Doe\n\n## Experience\n- Built Python APIs",
                "optimization_summary": "Reordered to lead with Python and distributed systems.",
            }
        if "looking_for" in props:  # kit step 1: role analysis + open questions
            return {
                "looking_for": ["Strong Python", "Distributed systems"],
                "open_questions": [
                    {
                        "question": "Why do you want to work here?",
                        "advice": "Tie your background to the mission.",
                        "suggested_answer": "I admire the team's work on…",
                    }
                ],
            }
        return self._verdict(0)  # single (non-batched) scoring verdict


# Two canned postings the mocked scraper returns for every company.
SCRAPED = [
    scraper_mod.ScrapedPosition(
        external_id="gh-1", title="Senior Backend Engineer", location="Remote",
        url="https://example.test/jobs/1",
        description="Build Python APIs and distributed systems at scale.",
    ),
    scraper_mod.ScrapedPosition(
        external_id="gh-2", title="Platform Engineer", location="Remote",
        url="https://example.test/jobs/2",
        description="Own CI/CD and cloud infrastructure; strong Python a plus.",
    ),
]


@pytest.fixture(autouse=True)
def e2e_env(monkeypatch):
    """Stub the network + LLM boundaries for every test, and neutralize the kit
    worker's thread pool so the POST returns without spawning a thread (the test then
    drives generation synchronously). Yields the list of (user_id, position_id) pairs
    the kit endpoint kicked, for assertions."""
    monkeypatch.setattr(matcher.scraper, "scrape_company", lambda company: list(SCRAPED))
    monkeypatch.setattr(
        "app.services.llm.clients_for_user", lambda db, user: (FakeLLM(), FakeLLM())
    )
    kit_kicks: list[tuple[int, int]] = []
    monkeypatch.setattr(
        kit_worker, "ensure_generating", lambda uid, pid: kit_kicks.append((uid, pid))
    )
    return kit_kicks


# ── HTTP helpers (each step of the journey, via the real API) ─────────────────
PASSWORD = "secret123"


def _register(c, email):
    return c.post("/api/auth/register", json={"email": email, "password": PASSWORD})


def _login(c, email):
    return c.post("/api/auth/login", json={"email": email, "password": PASSWORD})


def _signup(c, email):
    """The 'user login' stage: create the account, then log in so the session cookie
    is what authenticates the rest of the journey (register also sets it, but logging
    in explicitly mirrors a returning user)."""
    assert _register(c, email).status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    assert _login(c, email).status_code == 200


def _upload_resume(c, text=b"Jane Doe\nSenior Python Engineer\nBuilt Python APIs and distributed systems.\nSkills: Python, Go, PostgreSQL"):
    return c.post("/api/resumes", files={"file": ("resume.txt", text, "text/plain")})


def _add_company(c, name="Acme Robotics"):
    return c.post(
        "/api/companies",
        json={"name": name, "ats_type": "greenhouse", "ats_token": "acme"},
    )


def _add_interest(c, label="Backend roles"):
    return c.post(
        "/api/interests",
        json={"label": label, "title_keywords": "backend, platform",
              "locations": "remote", "min_score": 70},
    )


def _onboard(c, email="candidate@example.com"):
    """Full pre-scan setup: login + resume + one company + one interest."""
    _signup(c, email)
    assert _upload_resume(c).status_code == 201
    assert _add_company(c).status_code == 201
    assert _add_interest(c).status_code == 201


def _scan_and_score(c):
    """Run a scan (scrape happens in-request; scoring is enqueued) then synchronously
    drain the scoring queue — the same work the background pool would do."""
    r = c.post("/api/run")
    assert r.status_code == 200, r.text
    evaluator.drain_queue(max_workers=1, budget_seconds=0)
    return r.json()


def _latest(c, **params):
    r = c.get("/api/job-lists/latest", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ── Key-functionality slices (each stands alone) ──────────────────────────────
def test_login_flow_establishes_session():
    """Register, log out, log back in; the cookie session authenticates /me, wrong
    credentials are rejected, and an anonymous request is unauthorized."""
    with TestClient(app) as c:
        assert _register(c, "login@example.com").status_code == 200
        assert c.post("/api/auth/logout").status_code == 200
        # Cookie cleared by logout -> protected route now refuses us.
        assert c.get("/api/auth/me").status_code == 401
        assert c.post("/api/auth/login",
                      json={"email": "login@example.com", "password": "wrongpass"}).status_code == 401
        assert _login(c, "login@example.com").status_code == 200
        me = c.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["email"] == "login@example.com"


def test_upload_resume_is_stored_and_listed():
    with TestClient(app) as c:
        _signup(c, "resume@example.com")
        r = _upload_resume(c)
        assert r.status_code == 201
        assert r.json()["filename"] == "resume.txt"
        listed = c.get("/api/resumes").json()
        assert len(listed) == 1 and listed[0]["filename"] == "resume.txt"
        # Extracted text is what scoring later reads.
        content = c.get(f"/api/resumes/{listed[0]['id']}/content").json()
        assert "Python" in content["content_text"]


def test_set_company_and_interests_persist():
    with TestClient(app) as c:
        _signup(c, "settings@example.com")
        assert _add_company(c).status_code == 201
        assert _add_interest(c).status_code == 201
        companies = c.get("/api/companies").json()
        interests = c.get("/api/interests").json()
        assert [x["name"] for x in companies] == ["Acme Robotics"]
        assert [x["label"] for x in interests] == ["Backend roles"]


def test_scan_populates_ranked_job_list():
    with TestClient(app) as c:
        _onboard(c)
        summary = _scan_and_score(c)
        assert summary["new_positions"] == 2  # both scraped postings are new

        data = _latest(c, category="matching")
        assert data["pending"] == 0  # the drain finished the backlog
        titles = {item["title"] for item in data["items"]}
        assert "Senior Backend Engineer" in titles
        # Scored with the fake verdict (88), above the interest's min_score of 70.
        top = next(i for i in data["items"] if i["title"] == "Senior Backend Engineer")
        assert top["match_score"] == 88 and top["non_matching"] is False


def test_position_detail_then_generate_kit(e2e_env):
    kit_kicks = e2e_env
    with TestClient(app) as c:
        _onboard(c)
        _scan_and_score(c)
        uid = c.get("/api/auth/me").json()["id"]
        pid = _latest(c, category="matching")["items"][0]["position_id"]

        # Detail view: the posting + its best match, no kit yet.
        detail = c.get(f"/api/positions/{pid}/detail")
        assert detail.status_code == 200
        assert detail.json()["match_score"] == 88 and detail.json()["kit"] is None

        # Request a kit: 202 + 'generating', and the worker was kicked for this pair.
        started = c.post(f"/api/positions/{pid}/kit")
        assert started.status_code == 202 and started.json()["status"] == "generating"
        assert kit_kicks == [(uid, pid)]

        # Drive the background generation, then poll the kit to completion.
        kit_worker._run(uid, pid)
        kit = c.get(f"/api/positions/{pid}/kit")
        assert kit.status_code == 200
        body = kit.json()
        assert body["status"] == "ok"
        assert body["cover_letter"] and body["revised_resume"]
        assert body["looking_for"] and body["open_questions"]


# ── The full journey, end to end ──────────────────────────────────────────────
def test_full_workflow_end_to_end(e2e_env):
    """The whole story in one flow: login → resume → company + interests → scan →
    job list → detail → application kit."""
    kit_kicks = e2e_env
    with TestClient(app) as c:
        # 1) Login.
        _signup(c, "journey@example.com")
        uid = c.get("/api/auth/me").json()["id"]

        # 2) Upload a resume.
        assert _upload_resume(c).status_code == 201

        # 3) Configure a watch-list company and an interest.
        assert _add_company(c).status_code == 201
        assert _add_interest(c).status_code == 201

        # 4) Run a scan and let scoring complete.
        assert _scan_and_score(c)["new_positions"] == 2

        # 5) Results show up, ranked, in the job list.
        listing = _latest(c, category="matching")
        assert listing["pending"] == 0 and len(listing["items"]) >= 1
        pid = listing["items"][0]["position_id"]

        # 6) Open the detail view for the top match.
        detail = c.get(f"/api/positions/{pid}/detail").json()
        assert detail["position_id"] == pid and detail["match_score"] == 88

        # 7) Generate the application kit and poll it to 'ok'.
        assert c.post(f"/api/positions/{pid}/kit").status_code == 202
        kit_worker._run(uid, pid)
        kit = c.get(f"/api/positions/{pid}/kit").json()
        assert kit["status"] == "ok" and kit["cover_letter"] and kit["revised_resume"]
