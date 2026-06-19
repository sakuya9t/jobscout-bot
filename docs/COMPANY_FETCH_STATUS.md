# Company fetch status & auto-apply readiness

Tracks, per built-in preset, **how we fetch its roles today** and **what phase-2
auto-apply will take**. The presets themselves live in
[`app/company_presets.py`](../app/company_presets.py); the scrape adapters in
[`app/services/scraper.py`](../app/services/scraper.py). The machine-readable apply
hints (`requires_account`, `account_portal_url`) are fields on each `CompanyPreset`
— this doc is the human-facing status layer on top of them.

_Last reviewed: 2026-06-18._

## Legend

- **Fetch** — the `ats_type` adapter used.
- **Coverage** — how much of the board one scrape sees (drives removal reconciliation):
  `full` = whole board (absent ⇒ removed); `age-floor` = newest-first walk down to the
  age cutoff; `partial` = capped/lossy, never infers removals.
- **Desc** — does the scrape capture the job description (the LLM matcher scores on it)?
- **Date** — is a per-posting date captured (drives the staleness filter)?
- **Acct** — does *applying* require a candidate account/portal (`requires_account`)?
- **Status** — ✅ working · ⚠️ partial · ⛔ not integrated.

## Status table

| Company | Fetch (`ats_type`) | Status | Coverage | Desc | Date | Acct | Phase-2 auto-apply outlook |
|---|---|---|---|---|---|---|---|
| Anthropic | `greenhouse` | ✅ | full | ✅ | ✅ | no | Easy — Greenhouse form, no account. |
| OpenAI | `ashby` | ✅ | full | ✅ | ✅ | no | Easy — Ashby form, no account. |
| xAI | `greenhouse` | ✅ | full | ✅ | ✅ | no | Easy — Greenhouse form, no account. |
| Airbnb | `greenhouse` | ✅ | full | ✅ | ✅ | no | Easy — Greenhouse form, no account. |
| Databricks | `greenhouse` | ✅ | full | ✅ | ✅ | no | Easy — Greenhouse form, no account. |
| **Jane Street** | `greenhouse` | ✅ | full | ✅ | ✅ | no | Easy — Greenhouse form, no account. (209 live roles.) |
| Apple | `apple` | ✅ | age-floor | ✅ | ✅ | no¹ | Medium — custom portal; Apple ID likely needed at submit. |
| Amazon | `amazon` | ✅ | age-floor | ✅ | ✅ | yes | Hard — amazon.jobs candidate portal/account. |
| Google | `google` | ✅ | full² | ✅ | — | yes | Hard — Google sign-in + custom application flow. |
| NVIDIA | `eightfold` | ✅ | age-floor | ✅³ | ✅ | yes | Hard — funnels into Workday (account required). |
| Netflix | `eightfold` | ✅ | age-floor | ✅³ | ✅ | no¹ | Medium — Eightfold SmartApply flow. |
| **Citadel** | `sitemap` | ✅ | full⁴ | ✅³ | ✅ | yes | Hard — custom candidate portal; **needs verification** (see below). |
| **Two Sigma** | _none_ | ⛔ | — | — | — | yes? | Blocked on fetch first (see below). |

¹ `requires_account` is `False` in the preset (browsing isn't gated), but the final
submit step may still need an account — confirm during phase-2 build.
² Complete only when the paged walk reaches an empty results page within the cap.
³ No description in the listing/sitemap; it's pulled from each job's detail page
(eightfold: JSON-LD over httpx; Citadel: JSON-LD via a browser-TLS fetch — see below).
⁴ The sitemap enumerates every open-role page, so it's treated as a complete listing.

## Notes on the tricky ones

### Citadel — `sitemap`
- Careers pages (`citadel.com/careers/...`) are fronted by **Cloudflare**, which
  blocks plain `httpx` by **TLS/JA3 fingerprint** — every page (incl. the WordPress
  REST API and detail pages) returns the "Just a moment…" interstitial. It is *not*
  an interactive JS/Turnstile challenge: a request with a real browser's TLS profile
  passes straight through, so **no headless browser is needed**.
- **How we fetch it:**
  1. The Yoast `career-sitemap.xml` (`ats_token = https://www.citadel.com/career-sitemap.xml`)
     enumerates every open-role detail page (~35).
  2. Each detail page is fetched with a browser TLS profile via **`curl_cffi`
     (Chrome impersonation)** — `scraper._fetch_impersonated` — which gets past the
     fingerprint wall.
  3. The real **title, description, location, employment type, and post date** are
     parsed from the page's schema.org `JobPosting` JSON-LD. So Citadel roles carry
     full descriptions and score like any ATS board. Detail fetches run concurrently
     (bounded) with retry/backoff; a fetch that still fails leaves that one role
     description-less (then skipped) rather than failing the crawl.
- `requires_account=True` is an **assumption** (enterprise custom portal); the apply
  flow still needs inspecting before building auto-apply.

### Two Sigma — not integrated
- `careers.twosigma.com` is a **Phenom** single-page app. The page loads fine in a
  browser, but over static HTTP:
  - `careers/OpenRoles` server-renders only the **first ~10 roles**; pagination is
    JS-driven and doesn't advance via query params.
  - the `SearchJobsData` fragment endpoint needs an internal `qtvc` token we can't
    mint without executing the page's JS.
  - there is **no un-walled full feed** (the sitemap lists SPA route names, not jobs;
    `/portal/106` redirects back to the JS shell).
- Adding it as an `html` preset would silently surface only 10 of N roles — bad for a
  scout bot — so it's deferred.
- Upgrade path: a Phenom adapter (reverse-engineer the `qtvc` handshake) **or** the
  headless-browser path. Tracked here so it isn't forgotten.

## Maintenance

Update this doc whenever a preset is added/changed in `app/company_presets.py`, an
adapter's behaviour changes, or an apply flow is verified during phase-2 work.
