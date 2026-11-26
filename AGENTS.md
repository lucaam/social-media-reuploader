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

> Sei un Release Manager per `social-media-reuploader`. Controlla i commit sulla branch `main`, avvia il workflow
> "Manual Version Bump" (`gh workflow run manual-version-bump.yml --ref main`) e verifica la PR di bump
> `chore(release): bump versions to vX.Y.Z`. Dopo il merge, "Publish Release on Bump PR Merge" crea tag e release
> e pubblica l'immagine su GHCR: controlla che il job Docker sia verde. Non fare merge senza conferma dell'utente.

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
docker build -t your-registry/social-media-reuploader:dev .
# chart Helm
helm lint charts/social-media-reuploader --strict && helm unittest charts/social-media-reuploader
```

## Note e policy
- Usa Conventional Commits e rispetta le regole di Copyright prima di scaricare/rispedire contenuti.
- `src/config.py` è l'unica fonte delle impostazioni: una nuova impostazione va anche nella tabella del README e
  nella mappa `env` di `charts/social-media-reuploader/values.yaml` (`scripts/check_docs.py` lo verifica).
- Bot e GUI sono processi separati che condividono solo il database SQLite: una sola istanza del bot per token.
- Per rilascio automatico e semver il repository usa gli script di bump (`scripts/release/bump_versions.py`) e i
  workflow descritti in `RELEASE.md`.

***
File agent-specific (esempi): `agents/dev.instructions.md`, `agents/release.instructions.md` (vedi cartella `agents/`).
