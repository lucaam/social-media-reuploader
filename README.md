# Social Media Reuploader Bot

[![Release](https://img.shields.io/github/v/release/lucaam/social-media-reuploader?label=release)](https://github.com/lucaam/social-media-reuploader/releases/latest) [![GHCR Image Version](https://img.shields.io/docker/v/ghcr.io/lucaam/social-media-reuploader?label=ghcr.io&sort=semver)](https://github.com/lucaam/social-media-reuploader/pkgs/container/social-media-reuploader)

Scaffold per un bot Telegram che rileva link a contenuti (YouTube shorts, TikTok, Instagram, Facebook) e prova a scaricare e reinviare il file nel gruppo.

Questo repository contiene un'implementazione minima, un `Dockerfile` e un `Helm` chart per il deploy in Kubernetes.

Quickstart (locale, webhook):

1. Esporta le variabili d'ambiente richieste:

```bash
export BOT_TOKEN="<your-bot-token>"
export WEBHOOK_URL="https://example.com/webhook/$BOT_TOKEN"
export MODE=webhook
export WORKERS=2
```

2. Avvia l'app (sviluppo):

```bash
python -m src.main
```

3. Costruire l'immagine Docker:

```bash
docker build -t your-registry/social-media-reuploader:latest .
```

4. Helm: il chart è in `charts/social-media-reuploader` (valori di default in `values.yaml`).

GitHub Actions & GHCR

See the documentation for publishing and required Actions secrets: [docs/ghcr.md](docs/ghcr.md).

Nota: questo scaffold usa `yt-dlp` per il download; verifica limiti e policy di Telegram prima dell'uso in produzione.

Developer setup — pre-commit & linters
------------------------------------

Per uno sviluppo coerente e per riprodurre i controlli eseguiti in CI, installa le dipendenze di sviluppo e configura `pre-commit`:

```bash
python -m pip install -r requirements-dev.txt
pre-commit install
pre-commit install --hook-type pre-push
```

Questo installerà i hook locali che eseguono controlli `ruff`, `black`, `isort` al commit e `pytest` al pre-push. In CI gli stessi controlli vengono eseguiti dal workflow `ci.yml` e dal workflow `lint.yml`.

Se vuoi solo eseguire i controlli manualmente:

```bash
pre-commit run --all-files
pytest -q
```

Nota: i comandi sopra assumono che le dipendenze di sviluppo (ruff, black, isort, pre-commit) siano installate (vedi `requirements-dev.txt`).
