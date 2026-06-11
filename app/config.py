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

    @property
    def secret_is_default(self) -> bool:
        return self.secret_key == DEFAULT_SECRET

    # Ollama Cloud
    ollama_base_url: str = "https://ollama.com"
    ollama_api_key: str = ""
    # Two models: a cheap one triages relevance (does this posting match the
    # interest?), and the main one does the expensive resume<->role scoring only
    # for postings that pass. Keeping a strong cheap default here means matching
    # doesn't silently inherit whatever the user set as the scoring model.
    ollama_model: str = "gpt-oss:120b-cloud"  # scoring model (the "good" one)
    ollama_filter_model: str = "deepseek-v4-flash"  # cheap relevance filter
    ollama_timeout: int = 120

    # Telegram
    telegram_bot_token: str = ""

    # Scheduler
    daily_run_hour: int = 8
    daily_run_minute: int = 0
    scheduler_enabled: bool = True

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

    # Scoring
    # The LLM (not a keyword filter) decides relevance, so a run could otherwise
    # score every scraped posting. Cap LLM calls per run to keep "Run scan now"
    # responsive and costs bounded; remaining postings score on the next run.
    # 0 = unlimited.
    score_max_per_run: int = 50
    # Stage-1 relevance filtering is batched: one cheap call screens this many
    # postings at once (returns a verdict per posting), cutting filter calls ~Nx.
    score_filter_batch_size: int = 10

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
