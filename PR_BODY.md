Prepare release 0.2.1 and infra/dev tooling updates

This PR prepares the project for a new release and fixes CI/Helm issues.

Summary of changes:

- Bump package version to `0.2.1` and add `CHANGELOG.md` (Unreleased)
- Pin runtime dependencies to recent releases for compatibility testing (aiohttp, aiogram, yt-dlp, fastapi, uvicorn, websockets, prometheus-client, etc.)
- Add shared HTTP client (`src/http_client.py`) to reuse `aiohttp.ClientSession`
- Add cached aiogram Bot helper (`src/telegram_client.py`) to reuse Bot instance
- Refactor `src/telegram_api.py` to reuse HTTP session and Bot and improve fallback behavior
- Update `src/bot.py` to register the shared Bot and close shared resources on shutdown
- Register cleanup handlers in `src/main.py` for shared resources
- Pre-commit/dev tooling updates: bump `black`, adjust `ruff` hook to `astral-sh/ruff-pre-commit`, apply formatting
- Helm chart fixes (indentation) and CI workflow tweaks (actionlint invocation, pinned GH action versions)

Validation performed locally:

- `pytest` — 12 passed
- `pre-commit --all-files` (ruff/black/isort) — passed; applied minor formatting
- `helm lint charts/social-media-reuploader` — passed
- Verified core modules import correctly in a fresh venv

Notes for reviewers:

- I pinned runtime deps for testing; if you prefer looser constraints (>=) we can revert pins before merging.
- The `isort` mirror used by pre-commit doesn't expose v8 tags; kept isort 5.x to maintain hook compatibility. If you want `isort` v8, update `.pre-commit-config.yaml` to use a different hook source.
- Database remains SQLite for now; for horizontal scaling consider migrating to Postgres (future work).

Next steps after merge:

- Merge to `main` will allow CI to run; create the official GitHub Release (or use `release-please`) to trigger Docker publish workflow.

Signed-off-by: Automated PR Bot
