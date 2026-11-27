# AGENTS — Prompt e istruzioni per agenti AI

Questo file contiene prompt e indicazioni riutilizzabili per aiutare un agente (es. Copilot o altro assistant) a riprendere il lavoro su questo progetto in altre conversazioni.

## Dev Assistant (italiano)
Prompt suggerito:

> Sei un assistente di sviluppo per il repository `social-media-reuploader`.
> - Mostrami lo stato attuale del progetto (file principali, test). Quindi esegui i test localmente.
> - Se trovi task aperti in `HISTORY.md` o `AGENTS.md`, proponi i passi successivi e crea branch/patch minime.
> - Quando modifichi file, includi un commit con messaggio conforme a Conventional Commits (es. `feat: add X` o `fix: correct Y`).
> - Per le modifiche più grandi, genera anche un breve changelog e, se opportuno, crea un PR draft.

## Release Manager
Prompt suggerito:

> Sei un Release Manager per `social-media-reuploader`. Le release sono automatiche: ogni push su `main` passa dal
> workflow "Release" (`release.yml`), che calcola la versione dai Conventional Commits dall'ultimo tag
> (`python scripts/release/version.py plan` in locale), pubblica immagine e chart firmati su GHCR e crea tag e release.
> Controlla che il titolo delle PR sia del tipo giusto (decide la release), che il run "Release" su `main` sia verde e
> che la release `vX.Y.Z` abbia immagine (amd64/arm64) e chart. Per forzare o ripubblicare usa l'avvio manuale
> (`gh workflow run release.yml --ref main -f bump=patch` o `-f tag=vX.Y.Z`) solo con conferma dell'utente; non fare
> merge senza conferma.

## Onboarding rapido — cosa fare localmente
Comandi utili per l'agente umano o per esecuzione automatica:

```bash
# dipendenze (servono anche ffmpeg/ffprobe nel PATH; deno arriva con requirements.txt)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
# test (database usa-e-getta, nessuna rete)
REQUESTS_DB=/tmp/smr-tests/requests.db python -m pytest -q
# lint
pre-commit run --all-files && python scripts/check_docs.py
# avvia il bot in locale (MODE=polling è il default; webhook richiede WEBHOOK_URL)
export BOT_TOKEN="<token>"
export MODE=polling
python -m src.bot
# avvia la GUI (stesso REQUESTS_DB del bot)
SECRET_KEY="$(openssl rand -hex 32)" ADMIN_TOKEN="<token-admin>" ./scripts/run-dev.sh
# build immagine localmente
docker build -t your-registry/social-media-reuploader:dev --build-arg VERSION=0.0.0-dev .
# chart Helm
helm lint charts/social-media-reuploader --strict && helm unittest charts/social-media-reuploader
# smoke test di un'immagine (gli stessi controlli della CI)
scripts/smoke_test_image.sh your-registry/social-media-reuploader:dev 0.0.0-dev
```

## Note e policy
- Usa Conventional Commits e rispetta le regole di Copyright prima di scaricare/rispedire contenuti.
- `src/config.py` è l'unica fonte delle impostazioni: una nuova impostazione va anche nella tabella del README e
  nella mappa `env` di `charts/social-media-reuploader/values.yaml` (`scripts/check_docs.py` lo verifica).
- Bot e GUI sono processi separati che condividono solo il database SQLite: una sola istanza del bot per token.
- Nessun commit di versione: il tag `vX.Y.Z` è l'unica fonte della versione, iniettata in fase di build (immagine:
  `--build-arg VERSION` → `SMR_VERSION`; chart: `helm package --version --app-version`; `Chart.yaml` resta `0.0.0`).
  Regole e workflow in `RELEASE.md`; lo script è `scripts/release/version.py` (test in
  `tests/test_release_version.py`).
- Le PR si uniscono con squash: il titolo della PR (Conventional Commits) diventa il commit su `main` e decide la
  release (`feat` minor, `fix`/`perf` patch, `chore`/`docs`/`ci`/... nessuna).

***
File agent-specific (esempi): `agents/dev.instructions.md`, `agents/release.instructions.md` (vedi cartella `agents/`).
