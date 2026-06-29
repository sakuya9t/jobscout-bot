# Auto-Apply — Implementation Plan

Phased build for [`DESIGN.md`](./DESIGN.md). Each phase is independently
shippable and de-risks the next. The hard, irreversible part (actually clicking
submit) comes last and behind a flag.

## Phase 2a — Schema + resolution + gaps (no submission yet)

Goal: for an in-scope position, fetch the **real** application form, resolve every
field from existing data, and tell the user exactly what (if anything) is missing —
**without sending anything.** This proves the field-mapping with zero outward risk.

- `app/services/autoapply/schema.py` — `FormSchema` / `Field` dataclasses +
  per-ATS fetchers:
  - Greenhouse: `GET boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`
  - Ashby: `jobPosting.info`
  - Lever: postings JSON (already fetched by the scraper — reuse).
  Reuse the scraper’s HTTP layer (`httpx` / `_fetch_impersonated`).
- `app/services/autoapply/resolve.py` — map `Field → value` from
  `ApplicantProfile` / `ProfileEducation` / `ProfileExperience` /
  `ApplicationKit.open_questions` / resume file. LLM (via `services.llm`) only for
  fuzzy/custom questions and select-option choice. Returns
  `(answers, missing_required[])`.
- DB: add `ApplicationAnswer` model + the new `Application` lifecycle columns
  (§7). Follow the schema-reconcile pattern in [`app/db.py`](../../app/db.py).
- API: `app/routers/autoapply.py` — `POST /api/autoapply/{position_id}/preflight`
  → returns resolved answers + `missing_required`. `GET …/status`.
- Reuse `_require_visible` (position must be in the user’s job list) from
  [`app/routers/applications.py`](../../app/routers/applications.py).
- Tests: schema-parse fixtures for each ATS; resolver unit tests (profile→field,
  gap detection) with an injected fake LLM client, like the matcher/kit tests.

**Exit:** a position shows “ready to auto-apply” or “needs N answers,” computed
from the real form. Nothing is submitted.

## Phase 2b — Needs-input loop + write-back

- Frontend (Vue SPA, `frontend/src`, see the `vue-spa-migration` memory): a
  “needs answers” panel on the position detail page; reuse the kit/profile
  components.
- `ApplicationAnswer` write path + write-back of generic answers into
  `ApplicantProfile` (so they’re reused).
- Re-run resolve after save; surface the cleared/remaining gaps.

**Exit:** the user can fill every gap and reach `status = ready`.

## Phase 2c — Greenhouse submission (the first real send)

- `app/services/autoapply/submit_greenhouse.py` — Playwright driver: open the
  hosted apply URL (`position.url`), fill mapped fields, upload the resume file,
  submit, capture confirmation (screenshot + any ref). Greenhouse hosted boards
  are plain forms — the simplest first target — and most presets are Greenhouse.
- `app/services/autoapply/worker.py` — background worker mirroring
  [`app/services/kit_worker.py`](../../app/services/kit_worker.py); a queue row
  like [`scoring_queue`](../../app/services/scoring_queue.py).
- `ApplicationSubmission` audit rows; advance `Application` to `submitted`/`failed`.
- Guardrails (§9): `JOBSCOUT_AUTOAPPLY_ENABLED` flag (default off) in
  [`app/config.py`](../../app/config.py); per-submit confirmation; per-user/company
  rate-limit; captcha → `needs_human`.
- Infra: add the `browser` extra + `playwright install chromium` to the deploy
  image (`Procfile`/`.do`/`docs/DEPLOY.md`) — `requirements.txt` excludes it today.
- Tests: drive the Playwright flow against a **local static fixture** of a
  Greenhouse form (don’t hit live boards in CI); assert the filled values +
  the recorded `Application`/`ApplicationSubmission` rows.

**Exit:** one-click apply works end-to-end for Greenhouse presets, review-gated.

## Phase 2d — Ashby + Lever, then polish

- `submit_ashby.py` (React SPA — Playwright fills the rendered form),
  `submit_lever.py`.
- Optional: render `ApplicationKit.revised_resume` (Markdown) → PDF for upload
  (pick a renderer; `pypdf` is read-only).
- Optional: HTTP form-replay fast-path for Greenhouse (lighter than a browser).

## Touchpoints summary

| Area | New / changed |
|---|---|
| Models | `Application` (+lifecycle cols), `ApplicationAnswer` (new), `ApplicationSubmission` (new) |
| Services | `autoapply/{schema,resolve,submit_*,worker}.py` |
| Routers | `autoapply.py` (preflight / status / submit) |
| Config | `JOBSCOUT_AUTOAPPLY_ENABLED` + browser/Chromium in deploy |
| Frontend | needs-input panel + “auto-apply” button/state on position detail |
| Docs | update `docs/COMPANY_FETCH_STATUS.md` as each ATS apply flow is verified |

## Risks / watch-items

- **Hosted forms change** — selectors drift; keep the per-ATS submit driver thin
  and schema-driven, and fail loudly (audit row) rather than mis-submitting.
- **Chromium in prod** — image size + memory; the worker must bound concurrency
  like the scoring pool does.
- **Captcha / ToS** — never evade; park as `needs_human`. Keep the human in the
  loop (review step) as both UX and consent.
- **Wrong answers are worse than no answers** — never fabricate work history or
  EEO answers; a required field we can’t honestly fill stays a gap.
