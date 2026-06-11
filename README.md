# JobScout

Multi-user job-matching automation. Each user logs in, uploads a resume, lists
companies to watch and the roles they want. Daily, JobScout scrapes each
company's career page, finds **newly published** positions, and uses an **Ollama
Cloud** model to (1) decide if a position matches the user's requirements and
(2) score the resume↔role fit and realistic chance of landing it — then delivers
a ranked report via **web dashboard**, **Telegram**, and an **MCP server** that
external agents (openclaw / hermes) can drive.

> Design lives in [`docs/DESIGN.md`](docs/DESIGN.md),
> [`docs/SPEC.md`](docs/SPEC.md), and [`docs/PLAN.md`](docs/PLAN.md).

## How matching works
1. **Scrape** — ATS-API-first: Greenhouse / Lever / Ashby JSON (robust, stable
   IDs), with a generic HTML fallback for everything else. Companies on a known
   ATS are auto-detected from their careers URL.
2. **Dedup** — a posting is "new" when its `(company, external_id)` hasn't been
   seen before. Re-runs are idempotent and don't re-bill the LLM.
3. **Exclude gate** — the only cheap text filter left is your explicit *exclude*
   keywords; positive relevance is the LLM's job, not substring matching.
4. **Relevance filter (cheap model)** — `JOBSCOUT_OLLAMA_FILTER_MODEL` decides,
   semantically, whether a posting matches the interest, so the expensive model
   only sees plausible fits. Filtering is **batched** — one call screens
   `JOBSCOUT_SCORE_FILTER_BATCH_SIZE` postings — and a per-run cap
   (`JOBSCOUT_SCORE_MAX_PER_RUN`) bounds cost/latency; the rest score next run.
   Results are cached per resume *version*, so nothing is re-scored until the
   resume content actually changes.
5. **Score (main model)** — `JOBSCOUT_OLLAMA_MODEL` structured output:
   `matches_requirements`, `match_score` (0–100), `win_probability` (0–100),
   `reasoning`, `strengths[]`, `gaps[]`.
6. **Report** — ranked by match score. The web dashboard always shows the top
   matches (at least a few, even below threshold); the Telegram push keeps only
   matches above each interest's threshold.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # then edit: set JOBSCOUT_OLLAMA_API_KEY etc.
# Optional, for JS-heavy career pages:
#   pip install -e '.[browser]' && playwright install chromium
```

Configure Ollama in `.env`:
- **Cloud:** `JOBSCOUT_OLLAMA_BASE_URL=https://ollama.com`,
  `JOBSCOUT_OLLAMA_API_KEY=<key from ollama.com>`,
  `JOBSCOUT_OLLAMA_MODEL=gpt-oss:120b-cloud` (scoring),
  `JOBSCOUT_OLLAMA_FILTER_MODEL=deepseek-v4-flash` (cheap relevance filter).
- **Local:** `JOBSCOUT_OLLAMA_BASE_URL=http://localhost:11434`, key blank,
  set both models to one you've pulled, e.g. `llama3.1`.

## Run
```bash
jobscout init-db          # create tables
jobscout health           # verify DB + Ollama reachability
jobscout serve --reload   # web app at http://127.0.0.1:8000
```
Open the dashboard, register, upload a resume, add companies + interests, and
click **Run scan now**. The in-process scheduler also runs the scan daily at
`JOBSCOUT_DAILY_RUN_HOUR`.

### Telegram (optional)
Set `JOBSCOUT_TELEGRAM_BOT_TOKEN` (from @BotFather). On the dashboard you get a
link code; DM the bot `/start <code>` to receive your daily report there.

### Cron instead of the in-process scheduler
Set `JOBSCOUT_SCHEDULER_ENABLED=0` and run the pipeline from cron:
```bash
0 8 * * *  cd /path/to/jobscout && /path/to/.venv/bin/jobscout run-daily
```

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
- **P2:** cover letters, "why this company", role-specific resume refinement,
  application-page Q&A prep.
- **P3:** auto-application with per-company application budgets gated on
  match/win scores.
