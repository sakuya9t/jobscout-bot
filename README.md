# JobScout

Multi-user job-matching automation. Each user logs in, uploads a resume, lists
companies to watch and the roles they want. **Daily**, JobScout scrapes each
company's career page and saves **newly published** positions. Matching is
**on-demand**: when a user runs a scan from the dashboard, an **Ollama Cloud**
model (1) decides if each position matches the user's requirements and (2) scores
the resume↔role fit and realistic chance of landing it — producing a ranked report
in the **web dashboard** (and via an **MCP server** that external agents like
openclaw / hermes can drive). Scoring is kept off the daily cron on purpose: it's
the expensive, per-user step, so it runs only when a user asks for it. For each
promising role JobScout also generates a tailored **application kit** (cover
letter, role-specific résumé, and draft answers to the posting's questions). The
LLM and Telegram bot are **per-user**: every account brings its own API key and bot.

> Design lives in [`docs/DESIGN.md`](docs/DESIGN.md),
> [`docs/SPEC.md`](docs/SPEC.md), and [`docs/PLAN.md`](docs/PLAN.md).

## How matching works
The daily cron only runs step 1 (**scrape**), saving new positions. Steps 3–6 (the
LLM filter, score, and report) run **on-demand**, when a user clicks *Run scan now*
/ *Refresh matching scores* — keeping the expensive per-user matching off the cron.

1. **Scrape** — ATS-API-first: **Greenhouse / Lever / Ashby** JSON (robust, stable
   IDs), plus dedicated adapters for **Google Careers** and **Eightfold** (e.g.
   NVIDIA), and a generic HTML fallback for everything else. Companies on a known
   ATS are auto-detected from their careers URL. Eightfold's search API returns no
   job description, so JobScout fetches each posting's detail page for it (and
   `backfill-descriptions` catches up older rows — see below).
2. **Dedup** — a posting is "new" when its `(company, external_id)` hasn't been
   seen before. Re-runs are idempotent and don't re-bill the LLM.
3. **Exclude gate** — the only cheap text filter left is your explicit *exclude*
   keywords; positive relevance is the LLM's job, not substring matching.
4. **Relevance filter (cheap model)** — your chosen **light model** decides,
   semantically, whether a posting matches the interest, so the expensive model
   only sees plausible fits. Filtering is **batched** — one call screens
   `JOBSCOUT_SCORE_FILTER_BATCH_SIZE` postings. After a scan, scoring **drains to
   completion in the background** (no per-run cap); the dashboard shows how many
   positions are still being evaluated. Results are cached per resume *version*,
   so nothing is re-scored until the resume content actually changes.
5. **Score (main model)** — your chosen **main model** scores surviving postings
   in batches of `JOBSCOUT_SCORE_BATCH_SIZE`, returning structured output:
   `matches_requirements`, `match_score` (0–100), `win_probability` (0–100),
   `reasoning`, `strengths[]`, `gaps[]`.
6. **Report** — ranked by match score. The web dashboard always shows the top
   matches (at least a few, even below threshold). (Automatic daily Telegram pushes
   are currently disabled while that flow is reworked.)

## In the dashboard
After registering, everything is managed from one dashboard:
- **Resume** — upload (PDF/DOCX/TXT/MD) with an in-page preview; scores are cached
  per résumé version and only recomputed when the content changes.
- **Company watchlist** — add built-in **presets** (Anthropic, OpenAI, xAI, NVIDIA,
  Google) or your own custom companies. Each has a **detail page**; presets whose
  portals require a login to apply (NVIDIA, Google) let you save an **encrypted
  application account**, surfaced on the list as *account needed* / *account
  attached*. Custom companies are out of scope for auto-apply.
- **Interests** — the titles/locations/seniority you want, plus a per-interest
  match-score threshold and exclude keywords.
- **Application profile** — the contact / work-authorization / job-preference / EEO
  answers applications ask for, stored once for reuse; **Import from resume** drafts
  it from your uploaded résumé.
- **Job lists** — ranked matches with filters by **company**, post-date window, and
  minimum score/win. Every posting has a detail page with an AI **application kit**:
  what the role is looking for, a cover letter, a tailored résumé, and draft answers
  to its application questions.
- **LLM provider & Telegram** — bring your own API key + models and your own bot
  (both per-user; see below).

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # set JOBSCOUT_SECRET_KEY (LLM keys are per-user, in the dashboard)
# Optional, for JS-heavy career pages:
#   pip install -e '.[browser]' && playwright install chromium
```

`.env` only holds deployment-wide settings (secret key, database, scheduler,
scraping/scoring tuning). The LLM **provider, API key, and main/light models are
per-user** — set them on the dashboard's **LLM provider** page after you register.
The built-in provider is **Ollama Cloud** (`https://ollama.com`): bring a key from
ollama.com; the form pre-fills main model `gpt-oss:120b-cloud` (scoring) and light
model `deepseek-v4-flash` (relevance filter). Any other Ollama-compatible host (a
local/self-hosted server) is a one-line addition in `app/llm_providers.py`.

## Run
```bash
jobscout init-db          # create tables
jobscout health           # verify DB + Ollama reachability
jobscout serve --reload   # web app at http://127.0.0.1:8000
```
Open the dashboard, register, upload a resume, add companies + interests, and
click **Run scan now** to scrape and score. The in-process scheduler also scrapes
daily at `JOBSCOUT_DAILY_RUN_HOUR` (new positions only — scoring stays on-demand,
so the button reads **Refresh matching scores** once you have a saved list).

### Registration control (invite codes + rate limiting)
Registration is **invite-gated** by default (`JOBSCOUT_REQUIRE_INVITE=1`). Mint codes
from the CLI — only an HMAC of each code is stored (derived from `JOBSCOUT_SECRET_KEY`),
so the DB never holds a usable code or the key:
```bash
jobscout invite mint --max-uses 5 --expires-days 30 --count 3   # prints codes once
jobscout invite list                                            # uses/expiry/state
jobscout invite revoke <id|code>
```
Set `JOBSCOUT_REQUIRE_INVITE=0` for open registration (local dev). The app also applies
in-process per-IP **rate limits** (a global blanket plus stricter caps on login/register)
to blunt brute-force and DoS — see [docs/DEPLOY_VERCEL.md](docs/DEPLOY_VERCEL.md) for the
knobs and how this interacts with Vercel's edge DDoS/WAF protection.

### Telegram (optional, per-user)
Each user brings their own bot. Create one with @BotFather, paste its token on the
dashboard's **Telegram settings** page and Save, then DM the bot `/start <code>`
(the page shows your code) and click **Link chat**. Note: automatic daily Telegram
pushes are currently disabled while that flow is reworked.

### Cron instead of the in-process scheduler
Set `JOBSCOUT_SCHEDULER_ENABLED=0` and run the daily scrape from cron (it saves new
positions; scoring stays on-demand from the dashboard):
```bash
0 8 * * *  cd /path/to/jobscout && /path/to/.venv/bin/jobscout run-daily
```

### Production database (Supabase / Postgres)
The app is database-agnostic (SQLAlchemy) and runs on Postgres unchanged — only
the default is SQLite. To publish on a hosted DB:
```bash
# 1) Copy the local SQLite schema + data into the target (creates tables, copies
#    every row, fixes Postgres id sequences). --drop recreates the target schema.
jobscout migrate-db --target 'postgresql://postgres:[PW]@db.[REF].supabase.co:5432/postgres?sslmode=require' --drop
# 2) Point the app at it and restart:
export JOBSCOUT_DATABASE_URL='postgresql://postgres:[PW]@db.[REF].supabase.co:5432/postgres?sslmode=require'
jobscout serve
```
Use the **same `JOBSCOUT_SECRET_KEY`** on the target — encrypted application-account
credentials (and JWT sessions) are keyed off it. For Supabase prefer the direct /
session-pooler connection (port 5432) with `sslmode=require`. The same `migrate-db`
command works against a **local** Supabase/Postgres too (e.g. `supabase start`'s
`postgresql://postgres:postgres@127.0.0.1:54322/postgres`).

### Maintenance
```bash
jobscout backfill-descriptions --company nvidia   # fetch missing Eightfold descriptions
jobscout run-daily                                # scrape all companies + save new positions
```
Eightfold boards (e.g. NVIDIA) expose descriptions only on each job's detail page;
crawls fetch them for new postings, and `backfill-descriptions` catches up postings
stored before that (one detail fetch per job, in bounded-concurrency batches).

### MCP (agents: openclaw / hermes / …)
```bash
jobscout token you@example.com          # mint a bearer token for your account
JOBSCOUT_MCP_TOKEN=<token> jobscout mcp  # stdio MCP server
```
Example MCP client config:
```json
{
  "mcpServers": {
    "jobscout": {
      "command": "jobscout",
      "args": ["mcp"],
      "env": { "JOBSCOUT_MCP_TOKEN": "<token>" }
    }
  }
}
```
Tools: `list_companies`, `add_company`, `remove_company`, `list_interests`,
`add_interest`, `list_resumes`, `run_daily_scan`, `get_report`, `get_position`.

## Roadmap
- **P1 (done):** login, resume upload, company/interest config, scrape, LLM
  filter + score, daily report (web + Telegram + MCP).
- **P2 (done):** per-position application kit — "what this role wants", cover
  letter, role-specific résumé, and application-question Q&A prep; plus a reusable
  applicant profile and, for portals that require a login, encrypted per-company
  application accounts.
- **P3:** auto-application with per-company application budgets gated on
  match/win scores, driven by the saved profile + application accounts.
