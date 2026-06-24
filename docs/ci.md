# CI: the test gate for pull requests & DigitalOcean deploys

`.github/workflows/ci.yml` runs the **unit + e2e suites** on every pull request (and
on pushes to `main` / `release/next`). It is the gate that keeps untested code from
merging — and, because it mirrors the DigitalOcean App Platform runtime, from
deploying.

## What runs

Two jobs, both on **Python 3.11** (matching `.python-version` and the `Procfile`, i.e.
the DO runtime), with `JOBSCOUT_SKIP_NET=1` so the live-network smoke tests are skipped
(CI must never depend on a real career board being up; every other test mocks the
network and the LLM):

| Job | Installs deps from | Verifies |
|---|---|---|
| **Unit + e2e tests** | `pip install -e '.[dev]'` | the full suite (`pytest -q`), including `tests/test_e2e_workflow.py` |
| **DigitalOcean build parity** | `requirements.txt` + `pytest` | the app boots the way DO starts it (`python -m app.cli --help`) **and** the suite passes against DO's *exact* runtime dependency set |

The second job is what "verifies these tests for the DO deployment": DO's buildpack
installs from `requirements.txt` (not `pyproject.toml`), so this job runs the tests
against that same set and catches `requirements.txt` ↔ `pyproject.toml` drift before it
reaches production.

## Make it block merges (one-time repo setting)

The workflow runs automatically, but **GitHub only blocks a merge when the checks are
marked _required_** in a branch-protection rule. This is a repo-admin setting, done
once:

- **UI:** Settings → Branches → Add branch ruleset (or protect `main`) → enable
  *Require status checks to pass before merging* → add **`Unit + e2e tests`** and
  **`DigitalOcean build parity`**. Repeat for `release/next` if you merge into it.
- **CLI** (requires admin on the repo):

  ```bash
  gh api -X PUT repos/:owner/:repo/branches/main/protection \
    -F required_status_checks.strict=true \
    -f 'required_status_checks.contexts[]=Unit + e2e tests' \
    -f 'required_status_checks.contexts[]=DigitalOcean build parity' \
    -F enforce_admins=true \
    -F required_pull_request_reviews.required_approving_review_count=1 \
    -F restrictions=
  ```

## How this gates DigitalOcean

DO App Platform auto-deploys on push to the branch it watches (it builds from the repo
itself; there is no separate CI step inside DO). So the gate is: **required checks →
merge blocked until green → only tested code lands on the deploy branch → DO only ever
builds code the suite passed.** Point the DO app at the same branch the protection rule
guards (`main`), and the "DigitalOcean build parity" job guarantees the deps DO installs
are the deps the tests passed against.
