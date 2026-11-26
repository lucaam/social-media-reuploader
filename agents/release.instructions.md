# Release manager agent

Usa questo agente per preparare release, changelog e immagini. Il flusso completo è in `RELEASE.md`.

Checklist per una release:

- CI verde su `main` (`ci.yml`, `lint.yml`, `docker-pr-build.yml`).
- `./scripts/prepare_release.sh` in locale (test, pre-commit, commitlint, helm lint).
- Avvia "Manual Version Bump" (`gh workflow run manual-version-bump.yml --ref main`): aggiorna `src/__init__.py`,
  `charts/social-media-reuploader/Chart.yaml` (`version`, `appVersion`) e i tag immagine in `values.yaml`, poi apre la
  PR `chore(release): bump versions to vX.Y.Z`. Senza il secret `RELEASE_TOKEN` sulla PR parte solo la CI: chiedi
  all'utente di chiudere e riaprire la PR per avviare gli altri check obbligatori (vedi `RELEASE.md`).
- Dopo il merge (con conferma dell'utente) controlla "Publish Release on Bump PR Merge": tag, release GitHub e job
  "Publish the Docker image" (`ghcr.io/<owner>/social-media-reuploader:X.Y.Z` e `:latest`).
- Se l'immagine manca: "Manual Docker build and publish to GHCR" con `tag=vX.Y.Z`.
- Aggiorna `HISTORY.md` per le tappe importanti.

Prompt suggeriti:

- "Prepara la release successiva: elenca i commit dall'ultimo tag, indica il bump semantico atteso e avvia il bump."
- "Verifica che l'immagine della release vX.Y.Z esista su GHCR per amd64 e arm64."
