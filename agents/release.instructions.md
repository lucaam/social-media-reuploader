# Release manager agent

Usa questo agente per seguire le release, le immagini e i chart. Il flusso completo è in `RELEASE.md`.

Le release sono automatiche: ogni push su `main` passa dal workflow "Release" (`.github/workflows/release.yml`), che
ripete i controlli delle PR (`ci.yml`), calcola la versione dai Conventional Commits dall'ultimo tag
(`scripts/release/version.py`), pubblica l'immagine `ghcr.io/<owner>/social-media-reuploader:X.Y.Z` (amd64 e arm64) e
il chart `oci://ghcr.io/<owner>/charts/social-media-reuploader` `X.Y.Z`, li firma e attesta, poi crea il tag `vX.Y.Z`
e la release GitHub. Nessun commit di versione, nessuna PR di bump, nessun token personale.

Checklist:

- Prima del merge: il titolo della PR (diventa il commit di squash su `main`) ha il tipo giusto; il riepilogo del
  check `commitlint` dice che release produrrà. Anteprima locale: `python scripts/release/version.py plan`
  (servono i tag: `git fetch --tags`) e `./scripts/prepare_release.sh` (test, pre-commit, commitlint, helm lint).
- Dopo il merge: il run "Release" su `main` è verde (job Gate, Version, Image per amd64 e arm64, Image index, Chart,
  Publish); se il job Version dice "no release" il merge non conteneva commit rilasciabili.
- Solo Publish rende pubblico qualcosa: se fallisce un job precedente restano solo digest senza tag e nessun tag Git.
  "Re-run failed jobs", oppure (con conferma dell'utente) `gh workflow run release.yml --ref main -f tag=vX.Y.Z` per
  ripubblicare un tag di `main` o `-f bump=patch` per forzare una release.
- Un run più vecchio di una release già uscita si ferma da solo (piano o controllo dei tag): non c'è niente da
  correggere. Non cancellare mai il tag di una release pubblicata; per i tag fuori da `main` vedi "Problemi frequenti"
  in `RELEASE.md`.
- Verifica degli artefatti: `docs/ghcr.md` (cosign verify, gh attestation verify).
- Aggiorna `HISTORY.md` per le tappe importanti.

Prompt suggeriti:

- "Che release produrrà il merge di questa PR? Elenca i commit dall'ultimo tag e la versione prevista."
- "Verifica che la release vX.Y.Z abbia l'immagine su GHCR per amd64 e arm64, il chart OCI e firme valide."
