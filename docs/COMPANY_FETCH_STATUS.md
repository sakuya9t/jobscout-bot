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
| **Waymo** | `greenhouse` | ✅ | full | ✅ | ✅ | no | Easy — Greenhouse form, no account. (391 live roles.) |
| **Robinhood** | `greenhouse` | ✅ | full | ✅ | ✅ | no | Easy — Greenhouse form, no account. (136 live roles.) |
| Apple | `apple` | ✅ | age-floor | ✅ | ✅ | no¹ | Medium — custom portal; Apple ID likely needed at submit. |
| Amazon | `amazon` | ✅ | age-floor | ✅ | ✅ | yes | Hard — amazon.jobs candidate portal/account. |
| Google | `google` | ✅ | full² | ✅ | — | yes | Hard — Google sign-in + custom application flow. |
| **Google DeepMind** | `greenhouse` | ✅ | subset⁵ | ✅ | ✅ | no | Easy — Greenhouse form, no account. |
| NVIDIA | `eightfold` | ✅ | age-floor | ✅³ | ✅ | yes | Hard — funnels into Workday (account required). |
| Netflix | `eightfold` | ✅ | age-floor | ✅³ | ✅ | no¹ | Medium — Eightfold SmartApply flow. |
| **Pinterest** | `sitemap` | ✅ | full⁴ | ✅³ | ✅ | yes | Hard — Phenom portal; mixed sitemap, job_url_filter (see below). |
| **Meta** | `sitemap` | ✅ | full⁴ | ✅³ | ✅ | yes | Hard — custom portal; ~586 detail fetches/crawl (see below). |
| **Citadel** | `sitemap` | ✅ | full⁴ | ✅³ | ✅ | yes | Hard — custom candidate portal; **needs verification** (see below). |
| **Two Sigma** | _none_ | ⛔ | — | — | — | yes? | Blocked on fetch first (see below). |

¹ `requires_account` is `False` in the preset (browsing isn't gated), but the final
submit step may still need an account — confirm during phase-2 build.
² Complete only when the paged walk reaches an empty results page within the cap.
³ No description in the listing/sitemap; it's pulled from each job's detail page
(eightfold: JSON-LD over httpx; Citadel: JSON-LD via a browser-TLS fetch — see below).
⁴ The sitemap enumerates every open-role page, so it's treated as a complete listing.
⁵ DeepMind's self-hosted Greenhouse board carries only a curated subset (~18 roles) of
the full DeepMind listing. The complete ~81-role set is on Google's careers board under
`company=DeepMind` (`ats_type="google"`), but that funnels into a Google sign-in to
apply; we chose the Greenhouse board for the clean structured fetch + no-account apply.

## Notes on the tricky ones

### Pinterest — `sitemap`
- `pinterestcareers.com` is a **Phenom People** SPA (the `/phb/` script path), **fronted
  by Cloudflare** that 403s plain `httpx` — the `sitemap` adapter's browser-TLS fetch
  (`_fetch_impersonated`) gets through. Phenom builds its job-search API endpoint in JS,
  so there's no clean JSON feed to call.
- **How we fetch it:**
  1. `sitemap.xml` (`ats_token`) is a flat, **mixed** urlset — 188 job-detail pages
     (`/jobs/<id>/<slug>/`) plus ~205 marketing/blog/department URLs. The preset's
     **`job_url_filter`** (`/jobs/\d`) keeps only the job-detail entries, so we don't
     fetch — and surface as description-less "jobs" — the non-job pages.
  2. Each job page carries a schema.org `JobPosting` JSON-LD, read like Citadel/Meta.
- **Two reusable fixes came out of this** (both benefit any future board):
  - `job_url_filter` is a new optional field on `CompanyPreset`/`Company` (snapshotted
    via the schema-reconcile) threaded into `scrape_sitemap` — for any mixed sitemap.
  - `_LD_JSON_RE` now tolerates an HTML-entity-encoded `+` in the script type
    (`application/ld&#x2B;json`), which Phenom emits.
- `requires_account=True`: applying funnels into Phenom's candidate portal (assumption).

### Meta — `sitemap`
- `metacareers.com` is Meta's own careers site: a **Comet/Relay GraphQL SPA** with no
  third-party ATS. The initial HTML carries no job JSON, and the GraphQL endpoint is a
  persisted-query system (rotating `doc_id` + `fb_dtsg` tokens) — too fragile to drive,
  so there's no clean API adapter.
- **How we fetch it:**
  1. The jobsearch sitemap (`ats_token = https://www.metacareers.com/jobsearch/sitemap.xml`)
     enumerates every open role's detail page (`/profile/job_details/<id>/`, ~586).
  2. Each detail page carries a schema.org `JobPosting` JSON-LD, so the shared `sitemap`
     adapter (`scraper.scrape_sitemap`) pulls the real title/description/employment
     type/date the same way as Citadel.
- **Two gotchas:**
  - **Location**: Meta's JSON-LD `address` breakdown is broken — every role repeats
    `addressLocality: "Menlo Park"` and `addressCountry` is a nested `{"@type":"Country",
    "name":[...]}` object. The *correct* location is each `Place`'s top-level `name`
    ("New York, NY", "Remote, US", …), so `_jsonld_location` now **prefers `name`** and
    only joins string-valued address fields (Citadel, which has no `name`, is unchanged).
  - **Volume**: `scrape_sitemap` has no cap, so a Meta crawl fetches **all ~586 detail
    pages** (~330 KB each) — far heavier than Citadel's ~35. Watch for rate-limiting; if
    it bites, the fix is a per-board fetch cap on `scrape_sitemap`.
- `requires_account=True`: applying funnels into Meta's candidate portal (sign-in).
- **No lighter feed exists** (checked Jun 2026): Meta is on **no third-party ATS** —
  Greenhouse / Lever / Ashby / SmartRecruiters / Workday / Eightfold all return no Meta
  board. Meta's own GraphQL (`CareersJobSearchResultsDataQuery`) would be one call
  instead of ~586 fetches, but it needs session cookies + `fb_dtsg`/`lsd` + a `doc_id`
  that **rotates with every JS-bundle ship** — too fragile to depend on. The sitemap +
  JSON-LD path is the most stable option because it rides SEO web standards. So if
  volume becomes a problem, cap `scrape_sitemap` rather than switching feeds.

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
- **Note:** Pinterest (also Phenom) was integrated via the `sitemap` route — but only
  because *its* sitemap enumerates real job-detail pages. Two Sigma's sitemap lists SPA
  route names, not jobs, so the same trick doesn't apply here. Worth re-checking whether
  Two Sigma ever publishes a job-detail sitemap.

## Maintenance

Update this doc whenever a preset is added/changed in `app/company_presets.py`, an
adapter's behaviour changes, or an apply flow is verified during phase-2 work.
