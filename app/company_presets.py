"""Built-in company presets for the watch-list.

This is the whole "plugin" surface for popular companies: each :class:`CompanyPreset`
captures everything the scraper needs (careers URL + which ATS, if any) so a user can
add a well-known company without knowing how it's hosted. To support a new company,
append one entry to :data:`PRESETS` — no other code changes are required, because the
scraper (`app/services/scraper.py`) already dispatches generically on ``ats_type`` /
``ats_token``.

``ats_type`` is one of ``greenhouse`` | ``lever`` | ``ashby`` | ``eightfold`` |
``google`` | ``amazon`` | ``apple`` | ``html`` | ``auto``. The last four are custom
adapters for big self-hosted boards (Google careers, amazon.jobs, jobs.apple.com,
and Eightfold-hosted boards like NVIDIA/Netflix). For ATS-backed boards prefer
setting ``ats_type`` explicitly (plus ``ats_token``) so a preset doesn't depend on
URL auto-detection; ``html`` is the generic best-effort fallback for companies that
run their own (often JS-rendered) careers page.
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
    # Whether submitting an application to this company requires registering an
    # account on its application portal (vs. a one-shot form). Drives the company
    # detail page's "application account" section and the watch-list tag, and is
    # the gate for phase-2 auto-apply. Greenhouse/Ashby/Lever boards let you apply
    # without an account; portals like Google Careers and NVIDIA's Workday don't.
    requires_account: bool = False
    # Where the user registers/signs in to apply (prefilled into the account form).
    # Only meaningful when ``requires_account`` is True.
    account_portal_url: str | None = None


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
        # x.ai/careers/open-roles embeds a Greenhouse board; scrape the board
        # directly (its API serves the full role list, ~200+ postings).
        key="xai",
        name="xAI",
        careers_url="https://job-boards.greenhouse.io/xai",
        ats_type="greenhouse",
        ats_token="xai",
    ),
    CompanyPreset(
        # NVIDIA's careers site is an Eightfold (PCSX) board: client-rendered, but
        # it exposes an unauthenticated JSON search API the "eightfold" adapter
        # pages over plain HTTP. ats_token carries the org's *registrable* domain
        # (the API's ``domain`` param), which differs from the careers host.
        key="nvidia",
        name="NVIDIA",
        careers_url="https://jobs.nvidia.com/careers",
        ats_type="eightfold",
        ats_token="nvidia.com",
        # Applying funnels into NVIDIA's Workday portal, which requires a candidate
        # account/sign-in before you can submit.
        requires_account=True,
        account_portal_url="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    ),
    CompanyPreset(
        # Google has no public ATS API, but its careers site server-renders each
        # results page's jobs as embedded JSON; the dedicated "google" adapter
        # parses that over plain HTTP (see scrape_google in services/scraper.py).
        key="google",
        name="Google",
        careers_url="https://www.google.com/about/careers/applications/jobs/results/",
        ats_type="google",
        # Google Careers requires signing in with a Google account to submit.
        requires_account=True,
        account_portal_url="https://accounts.google.com/",
    ),
    CompanyPreset(
        # Airbnb runs a Greenhouse board (careers.airbnb.com embeds it); scrape the
        # board API directly via its token.
        key="airbnb",
        name="Airbnb",
        careers_url="https://job-boards.greenhouse.io/airbnb",
        ats_type="greenhouse",
        ats_token="airbnb",
    ),
    CompanyPreset(
        # amazon.jobs is Amazon's own board (no third-party ATS) but exposes a JSON
        # search API the "amazon" adapter pages over plain HTTP (see scrape_amazon).
        key="amazon",
        name="Amazon",
        careers_url="https://www.amazon.jobs/en/search",
        ats_type="amazon",
        # Submitting funnels into amazon.jobs' candidate portal, which requires a
        # sign-in/account before you can apply.
        requires_account=True,
        account_portal_url="https://www.amazon.jobs/",
    ),
    CompanyPreset(
        # Apple's careers site is a JS SPA backed by a CSRF-guarded JSON search API
        # the "apple" adapter drives (see scrape_apple). Browsing/searching needs no
        # account, so it isn't gated here.
        key="apple",
        name="Apple",
        careers_url="https://jobs.apple.com/en-us/search",
        ats_type="apple",
    ),
    CompanyPreset(
        key="databricks",
        name="Databricks",
        careers_url="https://job-boards.greenhouse.io/databricks",
        ats_type="greenhouse",
        ats_token="databricks",
    ),
    CompanyPreset(
        # Netflix's careers site (explore.jobs.netflix.net) is an Eightfold board, but
        # serves the SmartApply API flavour (the eightfold adapter falls back to it
        # when the PCSX flavour 403s). ats_token carries the org's registrable domain.
        key="netflix",
        name="Netflix",
        careers_url="https://explore.jobs.netflix.net/careers",
        ats_type="eightfold",
        ats_token="netflix.com",
    ),
]

PRESETS_BY_KEY: dict[str, CompanyPreset] = {p.key: p for p in PRESETS}
