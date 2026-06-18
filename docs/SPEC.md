# JobScout — Spec (contracts)

## Data model fields
- **User**: id, email(unique), hashed_password, created_at, telegram_chat_id?,
  telegram_link_code?
- **Resume**: id, user_id, filename, content_text, is_active, created_at
- **Company**: id, user_id, name, careers_url?, ats_type(auto|greenhouse|lever|
  ashby|html), ats_token?, location_hint?, is_active, last_scraped_at?,
  created_at. Unique(user_id, name).
- **Interest**: id, user_id, label, title_keywords?(csv), locations?(csv),
  seniority?, employment_type?, exclude_keywords?(csv), notes?(free text for LLM),
  min_score(default 70), is_active, created_at
- **Position**: id, company_id, external_id, title, location?, department?,
  employment_type?, url?, description?, posted_at?, first_seen_at.
  Unique(company_id, external_id).
- **MatchResult**: id, user_id, position_id, resume_id?, interest_id?,
  passed_filter, match_score(0-100), win_probability(0-100), reasoning,
  strengths(json), gaps(json), model, created_at.
  Unique(user_id, position_id, resume_id, interest_id) — each interest scores a
  position independently, so the dedup key includes interest_id. A terminal
  scoring failure persists a row with `model="error"` so the pair is skipped on
  re-runs (cleared by `matcher.clear_failed_markers`, so the next scan re-scores).

## LLM contract (Ollama structured output)
`matcher` calls `OllamaClient.chat_json(system, user, schema)` where `schema` is
`schemas.MatchVerdict.model_json_schema()`. The response is parsed with
`MatchVerdict.model_validate` — a Pydantic model is the single source of truth
for both the request `format` and parsing, so an incomplete/drifting response is
a validation error (→ marker row), not a silent zero.

Schema (object, required all):
```json
{
  "matches_requirements": "boolean",
  "match_score": "integer 0-100",      // resume <-> role fit
  "win_probability": "integer 0-100",  // realistic chance to get an offer
  "reasoning": "string",               // 2-4 sentences, user-facing
  "strengths": ["string"],             // why it's a strong match
  "gaps": ["string"]                   // missing/weak areas
}
```
System prompt = strict recruiter persona; rubric weights: must-have skills,
seniority fit, domain overlap, location/eligibility, requirement notes. User
prompt packs: interest requirements, resume text (truncated), position
title/location/description (truncated). Temperature 0.2.

## Scraper contract
`scrape_company(company) -> list[ScrapedPosition]` where
`ScrapedPosition = {external_id, title, location?, department?, employment_type?,
url?, description?, posted_at?}`.
- `ats_type == auto`: try to infer from careers_url host (greenhouse/lever/ashby),
  else HTML.
- Adapters:
  - **Greenhouse**: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
  - **Lever**: `https://api.lever.co/v0/postings/{token}?mode=json`
  - **Ashby**: `POST https://api.ashbyhq.com/posting-api/job-board/{token}`
  - **HTML**: fetch careers_url, extract anchors that look like job links; external_id = url hash.
- Respect `scrape_max_positions_per_company`. Network/parse errors are caught per
  company and reported, never crash the run.

## HTTP API (JSON; auth via Bearer or cookie unless noted)
- `POST /api/auth/register {email,password}` -> {access_token} (public)
- `POST /api/auth/login {email,password}` -> {access_token}; sets cookie (public)
- `POST /api/auth/logout`
- `GET  /api/me` -> user + telegram link code
- `POST /api/resumes` (multipart file) -> resume; sets active
- `GET  /api/resumes`, `DELETE /api/resumes/{id}`
- `POST /api/companies {name, careers_url?, ats_type?, ats_token?, location_hint?}`
- `GET/PATCH/DELETE /api/companies/{id}`, `GET /api/companies`
- `POST /api/interests {label, title_keywords?, locations?, ..., min_score?}`
- `GET/PATCH/DELETE /api/interests/{id}`, `GET /api/interests`
- `GET  /api/positions?company_id=` -> positions
- `POST /api/run` -> trigger this user's daily pipeline now -> run summary
- `GET  /api/report?date=&min_score=` -> ranked matches with reasoning
- `GET  /health` -> {db, ollama}
- Pages: `/` dashboard, `/login`, `/register` (HTML)

## MCP tools (stdio server, auth via JOBSCOUT_MCP_TOKEN = a user's bearer)
- `list_companies` / `add_company` / `remove_company`
- `list_interests` / `add_interest`
- `list_resumes`
- `run_daily_scan` -> {new_positions, scored, top_matches[]}
- `get_report(min_score?, limit?)` -> ranked matches
- `get_position(position_id)` -> full posting + latest match
All tools resolve the user from the token and scope to that user.

## Config (env, JOBSCOUT_ prefix)
secret_key, database_url, data_dir, ollama_base_url, ollama_api_key, ollama_model,
ollama_timeout, telegram_bot_token, daily_run_hour/minute, scheduler_enabled,
use_browser, scrape_user_agent, scrape_max_positions_per_company.
