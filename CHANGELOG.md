# Changelog

All notable changes to this project are documented in this file.

## Unreleased

- Bump runtime dependencies to recent releases (aiohttp, aiogram, yt-dlp, fastapi, uvicorn, websockets, prometheus-client, etc.)
- Introduce shared `aiohttp` ClientSession and cached `aiogram.Bot` instance for performance and reduced connection churn
- Update pre-commit configuration and dev tools (`black`, `ruff`) and apply formatting
- Fix Helm chart indentation and CI workflow issues; ensure `helm lint` passes
- Improve robustness in `telegram_api` to reuse sessions and handle fallbacks


## [v0.2.1](https://github.com/lucaam/social-media-reuploader/compare/v0.2.0...v0.2.1) (2026-05-13)

### Chores

* prepare 0.2.1 + infra fixes (#9) [0dc485f]

* recreate fix_deployment branch with imported changes (#7) [26dbefe]


### CI

* run lint and CI only on pull_request (avoid duplicate runs on merge to main) (#8) [c78e2d4]


## [v0.2.0](https://github.com/lucaam/social-media-reuploader/compare/v0.1.0...v0.2.0) (2026-05-13)

### Features

* add k8s resource profiles for video workloads (#4) [f37cf03]

* support gui.secrets in chart for OAuth and admin token configur… (#2) [0b265b5]


### Bug Fixes

* remove heltcheck from Dockerfile [0947cf2]


### Chores

* release 0.2.0 (#3) [e006d16]


## [v0.1.0] (2026-05-12)

- No user-facing commits found for this release.
