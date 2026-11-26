# Immagini GHCR e release

Le immagini sono pubblicate su GitHub Container Registry:

```text
ghcr.io/lucaam/social-media-reuploader:<versione>   # es. 0.9.0
ghcr.io/lucaam/social-media-reuploader:latest       # ultima release
```

Ogni immagine è multi-arch (linux/amd64, linux/arm64) e porta le label OCI
(versione, commit, data di build). `:latest` si sposta solo sulla release con il
numero di versione più alto.

## Flusso di release

1. **Manual Version Bump** (`manual-version-bump.yml`, avvio manuale): calcola
   la versione successiva dai Conventional Commits dall'ultimo tag (`feat` →
   minor, `fix` e altri → patch, `!`/`BREAKING CHANGE` → major), aggiorna
   `src/__init__.py`, `Chart.yaml` (`version`, `appVersion`) e i tag immagine in
   `values.yaml`, apre la PR `chore(release): bump versions to vX.Y.Z` e avvia
   la CI su quel branch.
2. Si rivede e si fa il merge della PR.
3. **Publish Release on Bump PR Merge** (`publish-bump-release.yml`) crea il tag
   e la release GitHub sul commit di merge e chiama **Docker publish**
   (`docker-publish.yml`), che builda e pubblica l'immagine.

Le release create da un workflow con `GITHUB_TOKEN` non avviano altri workflow:
per questo `publish-bump-release.yml` chiama direttamente il workflow
riutilizzabile `docker-publish.yml` invece di contare sull'evento `release`.

Altri workflow:

- `docker-release.yml`: pubblica l'immagine per le release create a mano
  (interfaccia GitHub o `gh release create`).
- `docker-release-manual.yml`: ricostruisce l'immagine di un tag esistente
  (input `tag` obbligatorio; `push-latest` sposta `:latest` solo se il tag è la
  release più alta).
- `docker-pr-build.yml`: su ogni PR che tocca l'immagine la builda e controlla
  che ci siano deno, curl_cffi, yt-dlp-ejs, ffmpeg e che i moduli si importino
  con filesystem in sola lettura.

## Permessi e segreti

- Il push su GHCR usa `GITHUB_TOKEN` con `packages: write` (già nei workflow).
- `GHCR_PAT` (facoltativo): token personale con permessi di amministrazione del
  package, usato solo per rendere pubblico il package dopo il push. Senza, il
  passaggio viene saltato.

## Badge

```markdown
[![GHCR Image Version](https://img.shields.io/docker/v/ghcr.io/lucaam/social-media-reuploader?label=ghcr.io&sort=semver)](https://github.com/lucaam/social-media-reuploader/pkgs/container/social-media-reuploader)
```

## Build locale

```bash
docker build -t ghcr.io/lucaam/social-media-reuploader:dev \
  --build-arg VERSION=dev --build-arg VCS_REF="$(git rev-parse HEAD)" .
docker run --rm ghcr.io/lucaam/social-media-reuploader:dev deno --version
docker run --rm -e BOT_TOKEN="$BOT_TOKEN" -p 127.0.0.1:8080:8080 \
  ghcr.io/lucaam/social-media-reuploader:dev
```
