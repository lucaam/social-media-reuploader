# Release manager agent

Use this agent to prepare releases, changelogs and publish images.

Checklist for a release:

- Bump `charts/social-media-reuploader/Chart.yaml` `appVersion` and `version` as needed.
- Ensure `CHANGELOG.md` has the entry for the release (follow Keep a Changelog).
- Build and push container images (GHCR, DockerHub) using CI or local `docker build`.
- Create annotated Git tag and push tags to remote.

Suggested prompts:

- "Prepare a release PR for v0.1.1 with changelog and chart bump."
- "Generate GitHub Actions workflow to build and push GHCR image on release."
