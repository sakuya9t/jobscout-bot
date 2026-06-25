# PR preview deploys (DigitalOcean App Platform)

Every pull request **into `release/next`** gets an ephemeral App Platform app: a
GitHub Action deploys it, comments the live URL on the PR, and deletes it when the
PR closes. This is separate from production (prod is the control-panel app that
auto-deploys `main` via DO's native GitHub integration).

Moving parts:

| File | Role |
|---|---|
| [`.do/app.yaml`](../.do/app.yaml) | App spec the preview is built from (buildpack, port, preview env vars). |
| [`.github/workflows/preview.yml`](../.github/workflows/preview.yml) | Deploys on PR open/sync, comments the URL, deletes on close. |

## One-time setup

Both of these are currently **missing** — do them once before the first PR.

### 1. Create a DigitalOcean API token

DO Control Panel → **API → Tokens → Generate New Token**. Give it **read + write**
scope (full access, or a custom token with App Platform `create/read/update/delete`).
Copy the value — it's shown once.

### 2. Add it as a GitHub Actions secret

The workflow reads it as `DIGITALOCEAN_ACCESS_TOKEN`. Either via the UI
(repo **Settings → Secrets and variables → Actions → New repository secret**) or:

```bash
gh secret set DIGITALOCEAN_ACCESS_TOKEN   # paste the token when prompted
```

### 3. Authorize GitHub with App Platform (once)

App Platform can only pull the repo after your GitHub account is linked to it. If you
haven't already, create **any** app from the DO control panel once and connect the
`sakuya9t/jobscout-bot` GitHub repo during that flow. After that the linkage is
reusable and the action can create preview apps unattended.

## Using it

- **Open / push to a PR into `release/next`** → a preview app builds; a bot comment
  posts the live URL (and is updated in place on each push).
- **Close or merge the PR** → the preview app is deleted (`ignore_not_found` keeps
  the cleanup green if it was never created).

Previews run **open registration** (`JOBSCOUT_REQUIRE_INVITE=0`) on an **ephemeral
SQLite DB** that resets on every redeploy — register a throwaway account to click
through. The daily scheduler is off, and LLM provider/key are per-user (set them in
the preview's dashboard if you need scoring to run).

## Notes & knobs

- **Cost:** each open preview runs one `apps-s-1vcpu-0.5gb` basic instance (~$5/mo,
  billed hourly). Closing the PR stops the meter. Bump `instance_size_slug` in
  `.do/app.yaml` if a preview needs more memory.
- **Region:** `nyc` in the spec — change it there if you prefer another.
- **Forked PRs** don't receive the secret, so their deploy step fails by design;
  previews only run for branches pushed to this repo.
- **Also preview PRs into `main`?** Add `main` to `on.pull_request.branches` in
  `preview.yml`.
- The [`ci.yml`](../.github/workflows/ci.yml) test gate is independent — a preview
  can deploy before tests finish; merging is still blocked by the required checks.
