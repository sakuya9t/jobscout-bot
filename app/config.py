"""Application configuration, loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOBSCOUT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    secret_key: str = DEFAULT_SECRET
    database_url: str = "sqlite:///./jobscout.db"
    data_dir: Path = Path("./data")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # one week
    # Set the session cookie's Secure flag (HTTPS-only). Leave off for localhost
    # dev; turn on (JOBSCOUT_COOKIE_SECURE=1) behind HTTPS in production.
    cookie_secure: bool = False
    # Internal/admin endpoints (e.g. POST /api/admin/crawl) require this token in an
    # X-Admin-Token header. Empty (default) disables them with 503 — never an open
    # trigger. Set JOBSCOUT_ADMIN_TOKEN to a long random string to enable.
    admin_token: str = ""

    # Registration control (see app/invites.py). When on (default), /api/auth/register
    # requires a valid invite code; set JOBSCOUT_REQUIRE_INVITE=0 for open registration
    # (local dev / tests). invite_secret is the HMAC key for codes; empty falls back to
    # secret_key so there's no extra key to manage — set it only to rotate invites
    # independently of sessions (rotating invalidates outstanding codes).
    require_invite: bool = True
    invite_secret: str = ""

    # Rate limiting (see app/ratelimit.py). In-process per-IP limits: a global blanket
    # plus stricter caps on login/register. Disable for tests/dev with
    # JOBSCOUT_RATE_LIMIT_ENABLED=0. Behind a multi-instance/serverless deploy these are
    # per-instance only — use the platform WAF as the real DoS shield (docs/DEPLOY_VERCEL.md).
    rate_limit_enabled: bool = True
    rate_limit_global_per_minute: int = 120
    rate_limit_auth_per_minute: int = 5      # login attempts / IP / minute
    rate_limit_register_per_hour: int = 5    # signups / IP / hour (also throttles code guessing)
    # Trust the left-most X-Forwarded-For hop for the client IP (correct behind a proxy
    # like Vercel/nginx). Turn OFF for a directly-exposed server, where the header is
    # client-controlled and could be spoofed to dodge the limit.
    trust_forwarded_for: bool = True

    @property
    def secret_is_default(self) -> bool:
        return self.secret_key == DEFAULT_SECRET

    # Logging
    # Central logging is configured by app/logging_config.py:configure_logging().
    log_level: str = "INFO"
    # Optional rotating log file. Empty = log to stderr only.
    log_file: str = ""
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 3
    # Persist every Ollama request/response (full prompt + completion) to the
    # llm_logs table (see services/llm_log.py); stdout gets only a terse summary.
    # Prompts carry resume + job text, so they can be large and sensitive — set
    # JOBSCOUT_LOG_OLLAMA=0 in shared environments to disable the wire log.
    log_ollama: bool = True
    # Truncate each stored prompt/response to this many chars (0 = store in full).
    log_ollama_max_chars: int = 0

    # Ollama / LLM
    # Provider base URL, API key, and the main/light model names are now per-user
    # (see app/llm_providers.py + the LlmConfig table); only the request timeout
    # stays a deployment-wide setting.
    ollama_timeout: int = 120

    # Telegram is now per-user (each user brings their own bot token + linked chat;
    # see app/models.py:User and routers/telegram_config.py) — no deployment-wide
    # token. The scheduler still pushes the daily report, through each user's bot.

    # Scheduler
    daily_run_hour: int = 8
    daily_run_minute: int = 0
    scheduler_enabled: bool = True
    # Background worker threads (evaluator + kit_worker) started in the app lifespan.
    # They drain scoring/kit-generation backlogs off the request path on a long-lived
    # server. Set JOBSCOUT_BACKGROUND_WORKERS_ENABLED=0 on serverless (Vercel), where
    # threads don't survive a function freeze — there scoring is enqueued durably and
    # drained by the run-scoring cron instead of in-process workers.
    background_workers_enabled: bool = True

    # Scraping
    # TODO(browser): reserved flag for a future Playwright fallback for JS-heavy
    # career pages. NOT yet implemented — the scraper currently always uses plain
    # HTTP regardless of this value. See app/services/scraper.py:scrape_html.
    use_browser: bool = False
    scrape_user_agent: str = "Mozilla/5.0 (compatible; JobScoutBot/0.1)"
    scrape_max_positions_per_company: int = 40
    # Per-response buffer cap (bounds memory from a hostile/misconfigured endpoint).
    # Large popular boards are legitimately big — a full Greenhouse board with
    # descriptions (~5MB) or a big Ashby board (~11MB) — so this must clear them.
    scrape_max_response_mb: int = 32
    # Google careers has no ATS API; we page its server-rendered results (20/page).
    # Cap pages so a run pulls a bounded slice instead of all ~thousands of roles.
    scrape_google_max_pages: int = 20
    # Eightfold (PCSX) pages 10 roles each, newest-first; paging stops early once
    # postings predate scrape_max_age_days, so this only caps very high-volume
    # boards (60 pages = 600 newest roles) to bound request count per crawl.
    scrape_eightfold_max_pages: int = 60
    # Eightfold's search API returns no job description — it lives only on each job's
    # detail page (a JSON-LD block). Without it, every posting is description-less and
    # the matcher skips it, so we fetch detail pages to enrich/backfill descriptions.
    # That's one request per job, so cap how many we fetch per crawl (newest-first);
    # the rest are picked up on subsequent crawls. Fetched in bounded-concurrency
    # batches (scrape_eightfold_desc_workers) to stay polite. 0 disables enrichment.
    scrape_eightfold_max_descriptions: int = 150
    scrape_eightfold_desc_workers: int = 10
    # Only pull postings posted/updated within this many days, to bound how much we
    # store and score. Applies only to sources that expose a date (greenhouse/
    # lever/ashby); Google careers and the HTML fallback carry no per-posting date,
    # so their postings are always kept. 0 = no age filter. Availability tracking now
    # prunes closed roles independently (Position.removed_at), so this window can be
    # generous without leaving stale postings around.
    scrape_max_age_days: int = 90

    # Scoring
    # Stage-1 relevance filtering is batched: one cheap call screens this many
    # postings at once (returns a verdict per posting), cutting filter calls ~Nx.
    score_filter_batch_size: int = 10
    # Stage-2 scoring is also batched: after the cheap filter passes postings,
    # one expensive request scores this many postings against the resume.
    score_batch_size: int = 10
    # Background evaluation: a worker drains each user's whole scoring backlog to
    # completion off the request path (see app/services/evaluator.py). This bounds
    # how many users drain concurrently. Internal — not a user-facing knob.
    eval_max_workers: int = 2

    # Periodic scoring queue (services/scoring_queue.py + the `jobscout run-scoring`
    # cron in .github/workflows/scoring.yml). This drains every user's matching
    # backlog on its own schedule, separate from the daily scrape.
    # scoring_max_concurrency is THE database-connection throttle: at most this many
    # users drain at once, so concurrent Supabase connections stay constant in the
    # number of users (each held connection = one pooler client; the cap is ~15 and
    # shared with the web app, so keep this well under it).
    scoring_max_concurrency: int = 3
    # Wall-clock cap for one cron run (under the workflow's 60-min timeout), so a huge
    # backlog spans several runs instead of being killed mid-drain. 0 = no cap.
    scoring_run_budget_seconds: int = 3000
    # A job stuck "running" longer than this (its worker crashed/was killed) is
    # reclaimed to "pending" by the next reconcile sweep so its user isn't stranded.
    scoring_stale_minutes: int = 60

    @property
    def resume_dir(self) -> Path:
        return self.data_dir / "resumes"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.resume_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


settings = get_settings()
