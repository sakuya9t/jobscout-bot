# JobScout — Design

## What it is
Multi-user job-matching automation. Each user logs in, uploads a resume, defines
what roles they want and a list of companies to watch. Daily, the tool scrapes
each company's career page, detects **newly published** positions, uses an
**Ollama Cloud** model to (1) decide whether a position matches the user's
requirements and (2) score the resume↔role fit and the chance of landing it, and
delivers a ranked daily report via **web dashboard + Telegram**, with an **MCP
server** so external agents (openclaw / hermes) can drive everything too.

## Stack
- **Language/web:** Python 3.10+, FastAPI, Uvicorn.
- **DB:** SQLite via SQLAlchemy 2.0 (swap `DATABASE_URL` for Postgres unchanged).
- **LLM:** Ollama `/api/chat` with structured outputs (JSON schema). Works against
  Ollama Cloud (`https://ollama.com` + bearer key) or local (`localhost:11434`).
- **Scraping:** ATS-API-first (Greenhouse/Lever/Ashby JSON), generic HTML fallback
  (httpx + BeautifulSoup), optional Playwright for JS pages.
- **Scheduling:** APScheduler in-process daily job (toggleable; cron/MCP can drive instead).
- **Interfaces:** Web UI (Jinja), Telegram bot (long-poll), MCP server (stdio).

## Why ATS-first scraping
Reference repo `santifer/career-ops` (MIT) showed that ~most tech companies host
postings on a known ATS with a clean JSON API. Hitting those APIs is far more
robust than parsing arbitrary HTML and gives stable IDs for new-posting dedup.
We borrow that architecture (not the code — it's Node/Go/Claude).

## Components
```
app/
  config.py          Settings (env, JOBSCOUT_ prefix)
  db.py              Engine, SessionLocal, get_db(), session_scope()
  models.py          User, Resume, Company, Interest, Position, MatchResult
  auth.py            bcrypt + JWT; header(Bearer) or cookie; MCP token auth
  schemas.py         Pydantic request/response models
  services/
    ollama_client.py chat_json(schema)/chat_text(); Cloud or local
    resume_parser.py PDF/docx/txt -> normalized text
    scraper.py       ATS adapters + HTML fallback -> list[ScrapedPosition]
    matcher.py       per-user pipeline: scrape -> dedup -> LLM filter+score -> persist
    reporter.py      build daily report (dict/markdown) from MatchResults
    telegram_bot.py  long-poll bot: /start linking + sends reports
    scheduler.py     APScheduler daily trigger -> matcher.scrape_for_all_users
  routers/           auth, resumes, companies, interests, positions, reports, pages
  templates/         dashboard Jinja templates
  mcp_server.py      MCP tools wrapping the same services
  cli.py             init-db, run-daily, serve, mcp, health
```

## Data model (see SPEC for fields)
`User 1—N Resume`, `User 1—N Company`, `User 1—N Interest`,
`Company 1—N Position`, `MatchResult` links (user, position, resume, interest).
Dedup key for "new position" = (`company_id`, `external_id`). Scoring is cached by
unique (`user_id`, `position_id`, `resume_id`, `interest_id`) so re-runs don't
re-bill the LLM. Note: uploading a **new** resume changes the active `resume_id`,
so the `already`-scored set no longer matches and every passing (position,
interest) pair is re-scored against the new resume — a deliberate, one-time LLM
cost when the candidate's resume materially changes.

## Pipeline (per user)
Step 1 runs **daily** (cron via `matcher.scrape_for_all_users`). Steps 2–4 run
**on-demand** — when the user runs a scan (`POST /api/run` -> background evaluator
-> `matcher.score_to_completion`) — because scoring is the expensive per-user step.
1. For each active Company: scrape -> upsert Positions (new = unseen external_id).
2. Candidate set = new positions (+ any never-scored). Cheap keyword/location
   pre-filter against the user's Interests to cut LLM calls.
3. For each candidate + active resume: call Ollama -> `{passed, match_score,
   win_probability, reasoning, strengths[], gaps[]}`. Persist MatchResult.
4. Reporter ranks results above the interest's `min_score`; shown in the dashboard.

## Security / multi-tenancy
Every user-owned query is scoped by `user_id`. Resumes stored as extracted text
(+ original under `data/resumes/<user>/`). JWT in httpOnly cookie (browser) or
Bearer (API/MCP). Secrets via env only.

## Roadmap
- **P1 (this build):** login, resume upload, company/interest config, scrape,
  LLM filter+score, daily report (web + Telegram), MCP. 
- **P2:** cover letter, "why this company", role-specific resume refinement,
  application-page Q&A prep. Reuse `ollama_client.chat_text`; store as artifacts.
- **P3:** auto-application with per-company application budgets gated on
  `match_score`/`win_probability`. Needs an Application table + rate limits.
```
```
