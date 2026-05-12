# GHCR (GitHub Container Registry) and publishing

This repository includes a GitHub Actions workflow that builds and publishes the Docker image to GHCR when a release is published. It also includes a workflow to set the package visibility to `public` after publishing.

How it works

- On release publish, `.github/workflows/docker-release.yml` builds the image and pushes tags to `ghcr.io/${{ github.repository }}`.
- After a release, `.github/workflows/ghcr-visibility.yml` attempts to set the package visibility to `public` using the GitHub API.

Badges

You can show the published image's latest tag using Shields.io in the README:

```
[![GHCR Image Version](https://img.shields.io/docker/v/ghcr.io/lucaam/social-media-reuploader?label=ghcr.io&sort=semver)](https://github.com/lucaam/social-media-reuploader/pkgs/container/social-media-reuploader)
```

Publishing notes

- The GitHub runner uses the `GITHUB_TOKEN` to push the image. For private repositories, ensure the token has `packages: write` permissions in the workflow `permissions` block (already set).
- Making the package public requires additional repository/org permissions; the `ghcr-visibility` workflow attempts both org and user package endpoints.

Repository secrets and PAT (optional)

If the `ghcr-visibility` workflow fails due to insufficient permissions, create a Personal Access Token (PAT) with the `write:packages` (and `repo` if needed) scopes and add it to the repository Secrets as `GHCR_PAT`:

1. Create a PAT in GitHub (Settings → Developer settings → Personal access tokens) with scopes `write:packages` and `repo` (if you need to access private repo resources).
2. In the repository, go to Settings → Secrets and variables → Actions → New repository secret and add `GHCR_PAT` with the token value.

You can then modify `.github/workflows/ghcr-visibility.yml` or other workflows to use `${{ secrets.GHCR_PAT }}` where a higher-privileged token is required.

Note: keep PATs secret and rotate them regularly. For organization-wide automation, consider creating a GitHub App with limited permissions instead.

Local testing of images

After building locally, tag and run the image:

```bash
docker build -t ghcr.io/lucaam/social-media-reuploader:dev .
docker run -e BOT_TOKEN="$BOT_TOKEN" -p 8080:8080 ghcr.io/lucaam/social-media-reuploader:dev
```
