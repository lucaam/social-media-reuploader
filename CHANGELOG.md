# Changelog

All notable changes to this project are documented in this file.

## Unreleased

- Bump runtime dependencies to recent releases (aiohttp, aiogram, yt-dlp, fastapi, uvicorn, websockets, prometheus-client, etc.)
- Introduce shared `aiohttp` ClientSession and cached `aiogram.Bot` instance for performance and reduced connection churn
- Update pre-commit configuration and dev tools (`black`, `ruff`) and apply formatting
- Fix Helm chart indentation and CI workflow issues; ensure `helm lint` passes
- Improve robustness in `telegram_api` to reuse sessions and handle fallbacks
# Changelog

## [0.2.0](https://github.com/lucaam/social-media-reuploader/compare/0.1.0...v0.2.0) (2026-05-12)


### Features

* add k8s resource profiles for video workloads ([#4](https://github.com/lucaam/social-media-reuploader/issues/4)) ([f37cf03](https://github.com/lucaam/social-media-reuploader/commit/f37cf03588dcd5c71d64b63af9532f71dce8ab72))
* support gui.secrets in chart for OAuth and admin token configur… ([#2](https://github.com/lucaam/social-media-reuploader/issues/2)) ([0b265b5](https://github.com/lucaam/social-media-reuploader/commit/0b265b5eb9695d3fd1e531d012508a722e69d8d2))

## 0.1.0 (2026-05-12)


### Bug Fixes

* **ci:** accept RELEASE_PLEASE_TOKEN fallback for release-please action ([b6dce06](https://github.com/lucaam/social-media-reuploader/commit/b6dce061d6df42ecc8eacb00cc010e331375ac35))
* **ci:** bump upload-pages-artifact to v2; quote serviceAccountName to avoid YAML parse issues ([78314a1](https://github.com/lucaam/social-media-reuploader/commit/78314a186c3b26da72f1f25fddec7d69b96debde))
* **ci:** give release-please write permissions (contents,pull-requests,pages) ([6f6f540](https://github.com/lucaam/social-media-reuploader/commit/6f6f540260288f0d535395a72ef94e4317b24c4c))
* **ci:** make tests importable (PYTHONPATH); fix gui helm template; docs deploy permissions ([110a867](https://github.com/lucaam/social-media-reuploader/commit/110a867e51fce7942fa901717bbb0e0e53c5251b))
* **docs:** ensure GitHub Pages site configured after deploy ([29b6d4e](https://github.com/lucaam/social-media-reuploader/commit/29b6d4e50ab3c70b4338a4a4bf2627d6066c4b04))
* **docs:** use upload-pages-artifact + deploy-pages (first-party) ([8701696](https://github.com/lucaam/social-media-reuploader/commit/8701696305b8157b24217525cb60c8df9b562bec))
* **docs:** use upload-pages-artifact@v3 (uses upload-artifact@v4) ([0d652f4](https://github.com/lucaam/social-media-reuploader/commit/0d652f461f2aeee60cd944bf49b197117a061d8e))
* **helm:** preserve YAML spacing for serviceAccountName and ServiceAccount name ([99e5fd5](https://github.com/lucaam/social-media-reuploader/commit/99e5fd55fa3ddce0d8d12ed559c28f8b468b0b73))
* **helm:** remove backslash escapes in deployment env defaults ([e24ade7](https://github.com/lucaam/social-media-reuploader/commit/e24ade7c2663c5a68391cd64e7aeadf6b9230148))

## 0.1.0 - Initial release

- Initial import: Social media reuploader bot (aiogram) with background workers using `yt-dlp` + `ffmpeg`.
- Reaction-based UX (reactions for queued/processing/failures), per-chat behavior, and suppression of noisy errors.
- SQLite diagnostics (`request_events`) and admin GUI (`src/gui.py`).
- Helm chart under `charts/social-media-reuploader` and Dockerfile for container builds.
- Metrics and basic observability integrated.
