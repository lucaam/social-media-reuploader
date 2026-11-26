# Dev agent instructions

Obiettivo: riprendere lo sviluppo delle funzionalità del bot social-media-reuploader.

1. Contesto rapido:
   - il bot (`python -m src.bot`, anche `python -m src`) riceve gli update con aiogram in polling (`MODE=polling`,
     default) o webhook (`MODE=webhook` + `WEBHOOK_URL`, secret token in `WEBHOOK_SECRET`) ed espone `/health` e
     `/metrics` su `PORT` (8080);
   - per ogni link supportato da yt-dlp crea una richiesta nella coda SQLite (`REQUESTS_DB`); il worker la elabora
     (yt-dlp → ffmpeg → Bot API) rispettando rate limit, coda per chat e finestra anti-duplicati;
   - la GUI (`uvicorn src.gui:app`, porta 8081) è un processo separato che legge e scrive solo il database.
2. Verifiche iniziali:
   - `pip install -r requirements.txt -r requirements-dev.txt` (servono ffmpeg/ffprobe nel PATH).
   - `REQUESTS_DB=/tmp/smr-tests/requests.db python -m pytest -q`.
   - `pre-commit run --all-files` e `python scripts/check_docs.py`.
   - In locale: `BOT_TOKEN=<token di prova> MODE=polling python -m src.bot`; GUI con `./scripts/run-dev.sh`
     (stesso `REQUESTS_DB`, `SECRET_KEY` e `ADMIN_TOKEN` impostati).
3. Regole:
   - nuove impostazioni solo in `src/config.py`, documentate nel README e nella mappa `env` del chart;
   - chiamate al database dal codice async con `await db.aio(...)`;
   - niente `except Exception: pass`; testi per gli utenti in italiano, log in inglese, mai il token nei log;
   - test senza rete né Telegram reale.
4. Task frequenti (prompt per l'agente):
   - "Aggiungi un fallback per i file oltre il limite di Telegram (Bot API server locale o storage S3/MinIO)".
   - "Riutilizza il `file_id` dei media già inviati per i link ripetuti".
   - "Aggiorna yt-dlp e le liste di provider in tests/test_providers.py".
5. Pushing & deployment:
   - Il remote atteso è `git@github.com:lucaam/social-media-reuploader.git`.
   - L'agente NON deve fare push, merge o force-push senza conferma esplicita dell'utente.
6. Convenzioni commit: Conventional Commits (es. `feat:`, `fix:`, `chore:`).
