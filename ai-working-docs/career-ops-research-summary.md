# Career-Ops Research Summary

This note captures research on `santifer/career-ops` and how it maps to JobScout.
Use it as the starting point for a later implementation pass.

## Executive Summary

`career-ops` is useful as a reference architecture and component source, but it
should not be integrated wholesale. It is a local-first, agent/CLI-driven job
search command center that stores state in Markdown/TSV files. JobScout is a
multi-user FastAPI application with SQLAlchemy models, scheduler-driven scans,
Telegram delivery, and MCP tools.

The best path is to port selected ideas into Python:

- A provider registry for career-page/ATS scraping.
- More ATS providers: Workable, SmartRecruiters, Recruitee.
- Safer network handling: host allowlists, redirect controls, timeouts, retries.
- Playwright-based liveness verification for scraped posting URLs.
- Richer report concepts: posting legitimacy, gap mitigation, interview prep,
  and role-specific resume artifacts.

## What Can Be Reused Soon

### Provider Registry

`career-ops` uses a plugin-like provider layer: each `providers/*.mjs` module has
an `id`, optional `detect(entry)`, and required `fetch(entry, ctx)`. The scanner
loads providers dynamically and resolves a company by explicit provider first,
then local parser, then auto-detection.

JobScout currently has a fixed dispatch in `app/services/scraper.py`. A Python
registry would make it easier to add ATS adapters without growing one large
scraper file.

Recommended shape:

- `BaseProvider` protocol with `id`, `detect(company)`, `fetch(company)`.
- `ProviderRegistry` that resolves explicit `ats_type` first, then auto-detect.
- Keep existing `ScrapedPosition` as the adapter output contract.

### Additional ATS Providers

JobScout already has Greenhouse, Lever, Ashby, and generic HTML. Career-ops adds
several practical no-auth providers worth porting:

- Workable: public markdown feed at `https://apply.workable.com/{slug}/jobs.md`.
- SmartRecruiters: public postings API with pagination.
- Recruitee: tenant-specific public `/api/offers/`.
- Local parser hook: per-company executable parser that emits JSON jobs.

These would significantly improve coverage without adding LLM cost.

### Safer Scraper Behavior

Career-ops has useful hardening details:

- Greenhouse URL allowlist and HTTPS enforcement.
- Redirect blocking for known ATS API calls to reduce SSRF risk.
- Ashby-specific longer timeout plus backoff/jitter retry.
- URL validation before browser navigation.
- Private/loopback host blocking for Playwright liveness checks.

JobScout should adopt these patterns because it accepts user-provided career
URLs in a multi-user web service.

### Better Filters

Career-ops has clearer location filter semantics:

- `always_allow` wins first.
- `block` rejects after that.
- `allow` is required only if non-empty.
- Empty or malformed location passes rather than punishing missing provider data.

This is better than a single comma-separated substring match. We can map this to
future structured `Interest` fields while preserving current CSV fields for now.

### Liveness Verification

Career-ops has a Playwright URL liveness classifier:

- Blocks invalid/private URLs before navigation.
- Treats 404/410 as expired.
- Detects common "job no longer available" body patterns.
- Checks visible apply controls.
- Differentiates `active`, `expired`, and `uncertain`.

This fits JobScout's unused `JOBSCOUT_USE_BROWSER` setting. Recommended model:

- Add optional liveness verification only for new positions.
- Store `liveness_status`, `liveness_reason`, `last_verified_at` on `Position`.
- Do not permanently drop `uncertain` results; report them with caveats.

## Valuable Ideas For P2/P3

### Posting Legitimacy

Career-ops evaluates whether a posting looks real and active. Signals include
freshness, apply button state, JD specificity, company hiring signals, reposting
patterns, and salary transparency.

This would make JobScout reports much more useful than a raw match score.

Possible additions:

- `legitimacy_tier`: high_confidence, proceed_with_caution, suspicious.
- `legitimacy_reasoning`: short user-facing summary.
- `legitimacy_signals`: JSON details.
- `reposted_count` or repeated company/title detection.

### Application Lifecycle

Career-ops maintains application status, follow-ups, deduplication, and pipeline
integrity. JobScout currently stops at reports. Before P3 auto-application, add
an `Application` table and a human-reviewed workflow:

- saved, interested, applied, responded, interview, offer, rejected, skipped.
- follow-up due dates.
- notes and application artifacts.
- status filters in dashboard/MCP.

### Resume/Cover Letter Artifacts

Career-ops has an ATS-oriented PDF flow:

- Extract JD keywords.
- Rewrite only truthful existing experience.
- Generate single-column ATS-friendly HTML.
- Render to PDF with Playwright.
- Normalize problematic Unicode for ATS parsing.

For JobScout, this should become P2 artifacts tied to a match:

- cover letter.
- role-specific resume rewrite.
- "why this company".
- application Q&A draft.

Use `ollama_client.chat_text` for content generation and store outputs in DB or
`data/artifacts/<user>/`.

### Interview Story Bank

Career-ops accumulates STAR+Reflection stories. This is not in JobScout's plan,
but it is valuable. It turns one-off match reports into long-term interview prep.

Possible model:

- `Story` table scoped by user.
- tags: skill, role archetype, company/domain, seniority signal.
- source match/report.
- generated interview questions mapped to stories.

### Pattern Analysis

Career-ops analyzes tracker outcomes to learn which roles, companies, and score
patterns produce results. JobScout could later learn from user outcomes:

- Calibrate match thresholds.
- Recommend better interests.
- Detect companies or role types with low ROI.
- Tune prompt weights based on actual response rates.

## What Does Not Fit JobScout Directly

- It is local-first and file-based, not a multi-user web app.
- It assumes an AI coding CLI/agent reads and writes files.
- It does not provide JWT auth, user isolation, HTTP API, scheduler, Telegram, or
  MCP server behavior.
- Its tracker is Markdown/TSV; JobScout needs DB tables and migrations.
- Many provider outputs are too thin for JobScout scoring because they include
  title/url/location but not full job descriptions.
- It does not provide a reliable general Workday/LinkedIn provider.
- The default scanner is mostly zero-token HTTP/API/local parser; the richer
  Playwright/WebSearch flow is agent-mediated, not a reusable backend service.
- Claude-specific batch runner flags are not portable to JobScout's Ollama
  backend.

## Recommended Implementation Order

1. Fix current JobScout startup/review issues first:
   - Add `email-validator` or replace `EmailStr`.
   - Decide whether `MatchResult` uniqueness should include `interest_id`.

2. Refactor scraping into provider registry:
   - Move current Greenhouse/Lever/Ashby/HTML logic behind provider classes.
   - Preserve `scrape_company(company) -> list[ScrapedPosition]`.

3. Add safer network controls:
   - HTTPS checks where possible.
   - ATS host allowlists.
   - Redirect blocking for API providers.
   - Per-provider timeout/retry settings.

4. Port extra providers:
   - Workable.
   - SmartRecruiters.
   - Recruitee.
   - Optional local parser hook later.

5. Implement liveness verification:
   - Use Playwright only when configured.
   - Verify only new positions to bound cost.
   - Store status and reason on positions.

6. Add report quality fields:
   - Posting legitimacy tier.
   - Gap mitigation.
   - Short role summary.

7. Plan P2 artifacts:
   - Cover letter.
   - Role-specific resume.
   - Application Q&A.
   - Interview story bank.

## Source References

- Repository: https://github.com/santifer/career-ops
- README: https://raw.githubusercontent.com/santifer/career-ops/main/README.md
- Scanner: https://raw.githubusercontent.com/santifer/career-ops/main/scan.mjs
- Providers directory: https://github.com/santifer/career-ops/tree/main/providers
- Workable provider: https://github.com/santifer/career-ops/blob/main/providers/workable.mjs
- Liveness browser check: https://raw.githubusercontent.com/santifer/career-ops/main/liveness-browser.mjs
- Liveness classifier: https://raw.githubusercontent.com/santifer/career-ops/main/liveness-core.mjs
- Portal template: https://raw.githubusercontent.com/santifer/career-ops/main/templates/portals.example.yml
- Architecture: https://raw.githubusercontent.com/santifer/career-ops/main/docs/ARCHITECTURE.md
- Scripts reference: https://raw.githubusercontent.com/santifer/career-ops/main/docs/SCRIPTS.md
- PDF generator: https://raw.githubusercontent.com/santifer/career-ops/main/generate-pdf.mjs
- Data contract: https://raw.githubusercontent.com/santifer/career-ops/main/DATA_CONTRACT.md
- License: https://raw.githubusercontent.com/santifer/career-ops/main/LICENSE
