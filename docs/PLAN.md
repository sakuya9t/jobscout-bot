# JobScout — Build Plan & Status

Recovery note: if context is lost, read DESIGN.md + SPEC.md, then this checklist.
Implement remaining `[ ]` items in order. Each file's responsibility is in DESIGN.

## P1 build order & status
- [x] pyproject.toml, .env.example, app/__init__.py
- [x] app/config.py            — Settings
- [x] app/db.py                — engine/session/init_db
- [x] app/models.py            — ORM (all 6 models)
- [x] app/auth.py              — hash/jwt/deps/MCP token auth
- [x] app/services/ollama_client.py — chat_json/chat_text/health
- [x] app/services/resume_parser.py — extract_text(filename, bytes)
- [x] app/services/scraper.py        — ATS adapters + HTML fallback
- [x] app/schemas.py                 — Pydantic in/out models
- [x] app/services/matcher.py        — run_for_user / scrape_for_all_users pipeline
- [x] app/services/reporter.py       — build_report -> dict + markdown + telegram
- [x] app/services/telegram_bot.py   — long-poll: /start link + send report
- [x] app/services/scheduler.py      — APScheduler daily trigger + telegram poller
- [x] app/routers/auth.py
- [x] app/routers/resumes.py
- [x] app/routers/companies.py
- [x] app/routers/interests.py
- [x] app/routers/positions.py
- [x] app/routers/reports.py         — /run, /report
- [x] app/routers/pages.py           — HTML dashboard/login/register
- [x] app/templates/*.html           — base, login, dashboard
- [x] app/mcp_server.py              — 9 MCP tools
- [x] app/main.py                    — FastAPI app, mount routers, startup init_db+scheduler
- [x] app/cli.py                     — serve | mcp | run-daily | init-db | health | token
- [x] README.md                      — setup + run
- [x] tests/test_smoke.py            — parser, prefilter, ATS infer, greenhouse parse (offline) + live
- [~] runtime smoke (BLOCKED): this env has no pip/venv (Debian needs
      `apt install python3-venv python3-pip`). All 22 modules byte-compile.
      Once deps install: `pip install -e '.[dev]' && pytest -q` then `jobscout serve`.

## P1 STATUS: code-complete, byte-compiles. Pending: dependency install + runtime test.

## Key implementation notes
- Matcher must be callable headless (scheduler, CLI, MCP) via session_scope().
- Pre-filter before LLM: title_keywords / exclude_keywords / locations substring
  match (case-insensitive) to limit token spend; `notes` always passed to LLM.
- New-position detection: position is "new" if its (company,external_id) was not
  in DB before this scrape. Always (re)score positions lacking a MatchResult for
  the user's active resume.
- Catch per-company scrape errors and per-position LLM errors; accumulate into a
  run summary so one failure never aborts the daily run.
- Telegram + scheduler are optional: skip cleanly if token/flag absent.

## P2 (later)
- artifacts table (cover_letter, why_company, tailored_resume, app_questions)
- services/writer.py using ollama_client.chat_text; routers + MCP tools + UI.

## P3 (later)
- Application table (status, applied_at, source), per-company application budget,
  auto-apply worker gated on match_score/win_probability thresholds.
