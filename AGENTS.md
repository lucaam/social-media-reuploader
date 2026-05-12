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

> Sei un Release Manager per `social-media-reuploader`. Controlla i commit sulla branch `main` e usa il workflow `release-please` per generare le release semantiche. Dopo che una release è pubblicata, esegui il workflow `docker-release` per buildare e pushare l'immagine.

## Onboarding rapido — cosa fare localmente
Comandi utili per l'agente umano o per esecuzione automatica:

```bash
# esegui test
python -m pytest -q
# avvia l'app localmente (long-polling / webhook simulated)
export BOT_TOKEN="<token>"
python -m src.main
# build immagine localmente
docker build -t your-registry/social-media-reuploader:dev .
```

## Note e policy
- Usa Conventional Commits e rispetta le regole di Copyright prima di scaricare/rispedire contenuti.
- Per rilascio automatico e semver, il repository usa `release-please` (workflow GitHub).

***
File agent-specific (esempi): `agents/dev.instructions.md`, `agents/release.instructions.md` (vedi cartella `agents/`).
