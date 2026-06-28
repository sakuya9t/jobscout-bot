# Auto-Apply — Design (Phase 2, no-account tier)

_Status: design / not yet built. Author pass: 2026-06-26._

## 1. Goal

Add an **“Auto Apply”** button to a position. Clicking it should, on the user’s
behalf, fill out and submit that posting’s real application using the data we
already hold — the user’s **resume**, their **application profile**, and the
position’s **application kit** (cover letter + draft answers).

This phase covers only the **easiest tier: postings where applying needs no
candidate account on the destination site.** Per
[`docs/COMPANY_FETCH_STATUS.md`](../../docs/COMPANY_FETCH_STATUS.md) that is the
**Greenhouse** and **Ashby** boards (Anthropic, OpenAI, xAI, Airbnb, Databricks,
Jane Street, Waymo, Robinhood, Google DeepMind) plus **Lever** (adapter exists,
same hosted-form model). Explicitly **out of scope** here: Google, Amazon,
NVIDIA→Workday, Apple, and the Meta/Pinterest/Citadel custom portals — all
`requires_account` or sign-in/captcha-heavy (tier 2+).

The two questions the task poses:

1. **How do we actually send an application?** → §4–§5.
2. **What if something needed is missing?** → §6 (the “needs-input” loop).

## 2. TL;DR — the one finding that shapes everything

**There is no public, third-party-usable “submit application” API for any of the
three no-account ATSes.** Their submit endpoints all require the **employer’s
secret API key**, which by design we never possess:

| ATS | Read schema (public, no auth) | Submit application |
|---|---|---|
| **Greenhouse** | `GET /v1/boards/{token}/jobs/{id}?questions=true` → full field list ✅ | `POST /v1/boards/{token}/jobs/{id}` needs **employer Job Board API key** (Basic-Auth) ❌ |
| **Ashby** | `jobPosting.info` → application-form spec ✅ | `applicationForm.submit` needs **`candidatesWrite`** (employer key) ❌ |
| **Lever** | postings JSON includes the hosted-form fields ✅ | `POST` apply needs an **employer API key** (Super-Admin generated) ❌ |

Greenhouse’s own docs are explicit: _“Any form posts should be proxied by your own
servers. Any direct post to the …POST method would reveal your secret key.”_ The
hosted careers pages submit through the **employer’s** backend, not a public API.

So the architecture is forced, and it’s actually a clean split:

- **Read side (deterministic, public):** fetch the **real application-form schema**
  — exact fields, types, required flags, and dropdown option values. This is far
  better than today’s LLM-*guessed* questions in the application kit.
- **Write side (automation):** drive the **hosted apply form in a headless
  browser** (Playwright), filling it from the schema map. The repo already ships
  the optional `browser = ["playwright>=1.44"]` extra and a `curl_cffi`
  TLS-impersonation fetch layer, so this fits the existing toolbox.

Everything below builds on that split.

Sources:
[Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html) ·
[Greenhouse submit-application docs](https://github.com/grnhse/greenhouse-api-docs/blob/master/source/includes/job-board/_applications.md) ·
[Ashby applicationForm.submit](https://developers.ashbyhq.com/reference/applicationformsubmit) ·
[Ashby public job-posting API](https://developers.ashbyhq.com/docs/public-job-posting-api) ·
[Lever postings-api](https://github.com/lever/postings-api).

## 3. What we already have (don’t rebuild)

The codebase was clearly built toward this; the pieces line up:

- **`Application` row** ([`app/models.py`], one per `(user_id, position_id)`,
  unique) — already the single source of truth for “has this user applied.”
  `status` defaults `"applied"`, `source` defaults `"manual"`; the model comment
  already reserves `"auto_applied"` / `"failed"` and a `source="auto"`. The
  manual “Mark applied” toggle ([`app/routers/applications.py`](../../app/routers/applications.py))
  creates exactly the rows auto-apply will create and advance.
- **`ApplicantProfile` (+ `ProfileEducation` / `ProfileExperience`)** — the
  structured answers forms ask for: contact, links, **work authorization**
  (`authorized_to_work`, `requires_sponsorship`, `work_authorization`),
  **EEO** (`gender`, `race_ethnicity`, `veteran_status`, `disability_status`),
  preferences (`desired_salary`, `remote_preference`, `earliest_start_date`,
  `notice_period`). The booleans are deliberately nullable (`NULL` = “not
  answered”, distinct from an explicit no) — exactly what form-fill needs.
- **`ApplicationKit`** — per-position `cover_letter`, `revised_resume` (Markdown),
  and `open_questions` (`[{question, advice, suggested_answer}]`). The draft
  answers feed free-text application questions.
- **`Resume`** — the **original uploaded file** lives on disk at
  `data/resumes/{user_id}/{resume_id}_{filename}` (PDF/DOCX/TXT/MD) **and** as
  extracted `content_text`. The on-disk file is what an `input_file` upload needs.
- **`CompanyAccount`** (Fernet-encrypted `username_enc`/`password_enc`) — not used
  in this tier (no account), but it’s where tier-2 portal logins already live.
- **Per-company apply hints** — `CompanyPreset.requires_account` /
  `account_portal_url`, and the readiness table, already encode which companies
  are in this tier.

The gap is everything **between** “we have the data” and “the form is submitted”:
the form-schema fetch, the answer-mapping, the submission driver, and the
missing-data loop.

## 4. How submission works, per ATS (the “how to send” answer)

We considered three mechanisms:

- **Option A — Official ATS submit API.** Blocked for all three (needs the
  employer key, §2). We still use its **read** half for the form schema.
- **Option B — HTTP form-replay.** Reverse-engineer the hosted form’s internal
  POST (scrape the CSRF/`authenticity_token`, replay multipart via
  `httpx`/`curl_cffi`). Lightweight, no browser. But undocumented, per-ATS,
  fragile, and dies the moment a board adds a JS-computed token, a SPA submit
  (Ashby is a React SPA), or a captcha.
- **Option C — Headless browser (Playwright).** Drive the actual hosted apply
  page: navigate, fill, upload the resume file, submit, screenshot the
  confirmation. Heaviest (needs Chromium in the deploy image) but the most robust
  and general — it handles SPA forms and lets us capture proof-of-submission.

**Recommendation: Option C (Playwright), guided by the Option-A read schema.**
Consider Option B as a later optimization for Greenhouse specifically (its hosted
form is a plain server-rendered form), once the field-mapping is proven.

### The public read schema we get for free

- **Greenhouse** `…/jobs/{id}?questions=true` → `questions[]`, each with
  `required` (bool), `label`, and `fields[]` of `{name, type, values?}` where
  `type ∈ {input_text, textarea, input_file, input_hidden,
  multi_value_single_select, multi_value_multi_select}` and select `values[]` are
  `{value, label}`. This is the **authoritative required-field list** — the basis
  for both filling and gap-detection (§6).
- **Ashby** `jobPosting.info` → the application-form specification (field list +
  types) used to build a custom careers page.
- **Lever** postings JSON → the hosted form’s fields (`name`, `email`, `resume`
  required; custom/“additional” fields per posting).

## 5. Architecture — the pipeline

A new `app/services/autoapply/` package, run by a **background worker** mirroring
[`app/services/kit_worker.py`](../../app/services/kit_worker.py) (DB-persisted
status, polled by the UI). One position’s auto-apply run is a small state machine:

```
[Auto Apply clicked]
        │
        ▼
1. SCHEMA FETCH  ─ ats adapter pulls the real form schema (public GET)
        │            → canonical [Field{key, label, type, required, options[]}]
        ▼
2. RESOLVE       ─ map each Field → a value from:
        │            • ApplicantProfile / Education / Experience  (deterministic)
        │            • ApplicationKit.open_questions + cover_letter (free-text)
        │            • Resume file on disk                         (input_file)
        │            • LLM only to: match a custom/free-text question to the best
        │              available answer, and pick the right select option value[]
        ▼
3. GAP CHECK     ─ any *required* field with no confident value?
        │            ├─ yes → status = needs_input, list the unanswered fields (§6)
        │            └─ no  → status = ready
        ▼
4. REVIEW        ─ show the exact packet that will be sent (dry-run). User confirms.
        │            (default ON — submission is outward-facing + irreversible)
        ▼
5. SUBMIT        ─ Playwright adapter drives the hosted form, uploads resume,
        │            submits, captures the confirmation (screenshot + any ref id)
        ▼
6. RECORD        ─ upsert Application(source="auto", status="submitted",
                     submitted_at, confirmation ref); on failure status="failed"
                     with error_detail. Append a submission-attempt audit row.
```

Per-ATS code is isolated behind an adapter interface (parallels the scraper’s
per-ATS adapters), so Greenhouse/Ashby/Lever differ only in steps 1 and 5:

```python
class ApplyAdapter(Protocol):
    def fetch_form(self, position) -> FormSchema: ...        # public read
    def submit(self, position, answers, resume_path) -> SubmitResult: ...  # browser
```

## 6. The “what if something’s missing?” loop

Because the schema in step 1 gives us the **real required-field list**, gap
detection is deterministic, not guesswork — this is the heart of requirement (2).

When step 3 finds required fields we can’t confidently fill (a custom essay
question, a work-authorization dropdown whose options don’t map to the profile, a
field the profile simply doesn’t have):

1. The run parks at **`status = needs_input`** and records the specific
   unanswered fields (label, type, options, an LLM-drafted suggested answer).
2. The position’s detail page shows a **“This application needs N answers”**
   panel — reusing the kit/profile UI — listing each question with the draft for
   the user to confirm/edit. (For selects, we show the form’s own option labels.)
3. On save we **persist** answers in the right place so they’re reused, not
   re-asked:
   - **Generic** answers (salary expectation, start date, sponsorship, EEO, etc.)
     → back into **`ApplicantProfile`**, improving every future application.
   - **Position-specific** custom questions → a new **`ApplicationAnswer`** store
     keyed `(user_id, position_id, field_key)`.
4. Resolution re-runs; once no required field is unanswered the run moves to
   `ready` → review → submit.

This also lets us *pre-flight*: we can compute the gap set the moment a kit is
generated and badge a position as **“ready to auto-apply”** vs **“needs 2
answers”** before the user ever clicks.

## 7. Data / state changes

- **`Application`** — promote `status` to a real lifecycle:
  `queued → needs_input → ready → submitting → submitted | failed`
  (keep `"applied"` as the manual-toggle value for back-compat). Add
  `submitted_at`, `confirmation_ref` (nullable), `error_detail` (nullable),
  `attempts` (int). `source="auto"` distinguishes these from manual marks. The
  existing `(user_id, position_id)` uniqueness stays — auto and manual share the row.
- **`ApplicationAnswer`** (new) — `(user_id, position_id, field_key)` unique;
  `label`, `value`, `source` (`profile|kit|user|llm`), timestamps. The per-position
  answer cache for custom questions.
- **`ApplicationSubmission`** (new, audit) — one row per submit attempt:
  `application_id`, `ats_type`, `status`, `request_snapshot` (JSON of what we sent,
  secrets redacted), `confirmation_ref`, `screenshot_path`, `error_detail`,
  `created_at`. Mirrors the `llm_log` / `scoring_log` trace-table pattern (see the
  memory note: prefer a queryable DB trace table over stdout).
- **`ApplicationKit.open_questions`** — optionally reconcile with the *fetched*
  schema so the kit shows the form’s **actual** questions, not guesses.

## 8. Missing pieces to build (checklist)

1. **Form-schema fetchers** — per ATS (Greenhouse/Ashby/Lever), public GET,
   deterministic; normalize to one `FormSchema`.
2. **Answer-resolver** service — profile/education/experience/kit → field map;
   LLM only for fuzzy/custom questions and choosing a select option from `values[]`.
3. **Resume file for upload** — start with the **original on-disk file** (already
   PDF/DOCX, perfect for `input_file`). _Enhancement:_ render the kit’s tailored
   `revised_resume` Markdown to PDF — needs a renderer dependency (`pypdf` is
   read-only); decision in §10.
4. **Playwright submission adapters** + put **Chromium in the deploy image**.
   `requirements.txt` deliberately **excludes** the `browser`/`playwright` extra
   today, so deployment needs `pip install -e '.[browser]' && playwright install
   chromium` — a real infra change to plan for.
5. **Needs-input UI** + the `ApplicationAnswer` store + write-back to profile (§6).
6. **Background worker + queue** — mirror `kit_worker`; never run a browser submit
   on the request thread.
7. **Guardrails** (§9).

## 9. Safety, legal, anti-abuse (must-haves, not nice-to-haves)

Submitting an application is **outward-facing and irreversible** — the strictest
bucket. Non-negotiables:

- **Review-before-submit ON by default.** The dry-run (step 4) shows the exact
  packet; the user clicks the real submit. An “auto-submit without review” mode, if
  ever added, is opt-in per user.
- **Explicit, logged consent** per submission (who/when/what) — the
  `ApplicationSubmission` row is the audit trail.
- **Captcha / anti-bot → graceful stop.** If the hosted form presents a
  reCAPTCHA/Turnstile or other challenge, do **not** attempt to defeat it; park the
  run as `needs_human` and hand off to the user. (No detection-evasion.)
- **Rate-limit** per user and per company (Lever already 429s above ~2 apply/s);
  reuse the existing `app/ratelimit.py` posture. No bulk “apply to everything.”
- **Idempotency** — the unique `(user, position)` row + a pre-submit check prevents
  double-submits; a failed attempt is retryable, a succeeded one is terminal.
- **Kill-switch** — a settings flag (`JOBSCOUT_AUTOAPPLY_ENABLED`, default off)
  gating the whole feature while it stabilizes.
- **ToS reality check** — several boards’ terms restrict automated submission;
  surface this to the user and keep them in the loop (the review step doubles as
  informed consent). This is *assisted* apply, not silent mass-apply.

## 10. Open decisions (need a call before/within build)

1. **Submission mechanism** — Playwright (robust, heavier) vs HTTP form-replay
   (light, fragile). _Recommended: Playwright first._
2. **Resume uploaded** — original file (simplest, ship first) vs the kit’s tailored
   PDF (needs a Markdown→PDF renderer). _Recommended: original first, tailored PDF
   as a fast-follow._
3. **Safety posture** — review-before-submit only, vs an opt-in true-auto mode.
   _Recommended: review-only for v1._

See [`PLAN.md`](./PLAN.md) for the phased build and concrete file touchpoints.
