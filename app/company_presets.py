"""Built-in company presets for the watch-list.

This is the whole "plugin" surface for popular companies: each :class:`CompanyPreset`
captures everything the scraper needs (careers URL + which ATS, if any) so a user can
add a well-known company without knowing how it's hosted. To support a new company,
append one entry to :data:`PRESETS` — no other code changes are required, because the
scraper (`app/services/scraper.py`) already dispatches generically on ``ats_type`` /
``ats_token``.

``ats_type`` is one of ``greenhouse`` | ``lever`` | ``ashby`` | ``html`` | ``auto``.
For ATS-backed boards prefer setting it explicitly (plus ``ats_token``) so a preset
doesn't depend on URL auto-detection; ``html`` is the generic best-effort fallback for
companies that run their own (often JS-rendered) careers page.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanyPreset:
    key: str  # stable slug used by the API/UI (not shown to users)
    name: str
    careers_url: str
    ats_type: str = "auto"
    ats_token: str | None = None
    location_hint: str | None = None


# Order here is the display order in the dashboard dropdown.
PRESETS: list[CompanyPreset] = [
    CompanyPreset(
        key="anthropic",
        name="Anthropic",
        careers_url="https://job-boards.greenhouse.io/anthropic",
        ats_type="greenhouse",
        ats_token="anthropic",
    ),
    CompanyPreset(
        key="openai",
        name="OpenAI",
        careers_url="https://jobs.ashbyhq.com/openai",
        ats_type="ashby",
        ats_token="openai",
    ),
    CompanyPreset(
        # Google has no public ATS API, but its careers site server-renders each
        # results page's jobs as embedded JSON; the dedicated "google" adapter
        # parses that over plain HTTP (see scrape_google in services/scraper.py).
        key="google",
        name="Google",
        careers_url="https://www.google.com/about/careers/applications/jobs/results/",
        ats_type="google",
    ),
]

PRESETS_BY_KEY: dict[str, CompanyPreset] = {p.key: p for p in PRESETS}
