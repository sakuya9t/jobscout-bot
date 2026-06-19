# DigitalOcean App Platform / Cloud Native Buildpack process definition.
#
# One process runs the whole app: the web server PLUS the in-process scoring drain,
# kit worker, and daily scheduler (defaults background_workers_enabled=1 +
# scheduler_enabled=1). This is the native single-process topology the migration doc
# recommends — no separate worker, no GitHub Actions cron, no dispatch.
#
# Bind 0.0.0.0 and the platform-injected $PORT so App Platform's router can reach it
# (serve otherwise defaults to 127.0.0.1:8000 and is unreachable). Invoke via
# `python -m app.cli` rather than the `jobscout` console script: the buildpack installs
# requirements.txt but not this package, so the entry-point script isn't on PATH.
web: python -m app.cli serve --host 0.0.0.0 --port ${PORT:-8080}
