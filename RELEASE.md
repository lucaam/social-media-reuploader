# Guida alle release

Le release sono semantiche e derivano dai Conventional Commits. Il flusso è
automatico a partire da un'azione manuale.

## Prerequisiti

- CI verde su `main` (test, linters, commitlint).
- Consigliato: secret `RELEASE_TOKEN`, un fine-grained personal access token
  limitato a questo repository con i permessi *Contents* e *Pull requests* in
  lettura e scrittura. *Manual Version Bump* lo usa per pushare il branch e
  aprire la PR di bump, così partono tutti i workflow delle PR e i check
  obbligatori. Senza, branch e PR sono creati con `GITHUB_TOKEN`, che non avvia
  workflow: parte solo la CI (lanciata con `workflow_dispatch`), mentre lint,
  commitlint e la build Docker restano in attesa finché non chiudi e riapri la
  PR.
- Facoltativo: secret `GHCR_PAT` (token con permessi di amministrazione del
  package GHCR) per rendere pubblico il package dopo il push. Il push delle
  immagini usa `GITHUB_TOKEN`.

## Flusso

1. **Bump**: avvia il workflow *Manual Version Bump* (tab Actions, oppure
   `gh workflow run manual-version-bump.yml --ref main`). Lo script
   `scripts/release/bump_versions.py`:
   - calcola la versione dai commit dall'ultimo tag (`feat` → minor, `fix` e
     altri → patch, breaking → major);
   - aggiorna `src/__init__.py`, `charts/social-media-reuploader/Chart.yaml`
     (`appVersion`, e `version` +1 patch) e i tag immagine in `values.yaml`;
   - crea il branch `release/bump-X.Y.Z` e apre la PR
     `chore(release): bump versions to vX.Y.Z` (con `RELEASE_TOKEN`, se c'è);
   - senza `RELEASE_TOKEN` avvia la CI sul branch (le PR aperte da
     `GITHUB_TOKEN` non avviano workflow) e lo segnala nella PR: chiudi e
     riapri la PR per far partire gli altri check obbligatori.
2. **Revisione e merge** della PR di bump, con tutti i check verdi.
3. **Pubblicazione**: *Publish Release on Bump PR Merge* crea tag e release
   GitHub (note generate automaticamente) sul commit di merge, poi chiama
   *Docker publish* che builda l'immagine linux/amd64 + linux/arm64 e la
   pubblica come `ghcr.io/<owner>/social-media-reuploader:X.Y.Z` e `:latest`.

Le release create a mano (interfaccia GitHub o `gh release create`) pubblicano
l'immagine con *Build and publish Docker image to GHCR on release*.

## Controlli in locale

```bash
npm ci                                                   # commitlint
pip install -r requirements.txt -r requirements-dev.txt  # i test importano l'app
./scripts/prepare_release.sh      # test, pre-commit, commitlint, helm lint, versioni
```

Se helm o actionlint non sono installati, lo script salta i relativi hook di
pre-commit (`helm-lint`, `actionlint`): la CI li esegue comunque.

## Ricostruire un'immagine

*Manual Docker build and publish to GHCR* con l'input `tag` (es. `v0.9.0`,
obbligatorio). `push-latest` sposta `:latest` solo se quel tag è la release più
alta, quindi non si torna mai indietro per errore.

```bash
gh workflow run docker-release-manual.yml -f tag=v0.9.0
```

## Problemi frequenti

- *La PR di bump non compare*: nessun commit dall'ultimo tag, oppure lo script
  è fallito (log del job "Run bump script").
- *La PR di bump aspetta check che non partono* ("Expected — Waiting for status
  to be reported"): è stata aperta con `GITHUB_TOKEN`. Chiudi e riapri la PR,
  oppure configura `RELEASE_TOKEN` per le prossime release.
- *La release c'è ma l'immagine no*: controlla il job "Publish the Docker image"
  di *Publish Release on Bump PR Merge*, poi rilancia la build manuale con quel
  tag.
- *Package GHCR privato*: aggiungi `GHCR_PAT` o cambia la visibilità dalle
  impostazioni del package.
