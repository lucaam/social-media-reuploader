# Contributing

Grazie per contribuire a `social-media-reuploader` — ecco alcune regole per facilitare release automatiche e la collaborazione.

## Conventional Commits (breve guida)
Usa il formato Conventional Commits per i messaggi di commit, esempi:

- `feat: aggiungi supporto per reels` — un nuovo feature -> incrementa MINOR
- `fix: correggi parsing link` — bugfix -> incrementa PATCH
- `perf: ottimizza download` 
- `docs: aggiorna README`
- `chore: aggiornamenti di build`
- `refactor: ristruttura codice senza cambiare behavior`

Per breaking change, aggiungi `BREAKING CHANGE: descrizione` nel corpo del commit o `!` dopo il tipo: `feat!: change API`.

## Versionamento Semantico
Il repository usa `release-please` per rilevare i tipi di commit e generare automaticamente PR di release / tag semantici.
- `feat` -> minor
- `fix` -> patch
- `BREAKING CHANGE` -> major

## Release / Docker images
- Le release semantiche vengono create dal workflow `release-please`.
- Alla pubblicazione della release, il workflow `docker-release` builda e pubblica l'immagine Docker usando i segreti `DOCKER_REGISTRY`, `DOCKER_USERNAME`, `DOCKER_PASSWORD`.

## Lint commit
Un workflow CI esegue `commitlint` su PR per applicare le regole di Conventional Commits.

## Esecuzione locale
1. Imposta virtualenv e dipendenze:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Esegui i test:
```bash
pytest -q
```

3. Avvia il server:
```bash
export BOT_TOKEN="<token>"
python -m src.main
```

