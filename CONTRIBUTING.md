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

### Come correggere messaggi non validi (es. commit già creati)

Se il tuo commit non rispetta il formato Conventional Commits, ci sono due vie comuni:

- Correggere l'ultimo commit locale (più semplice):

```bash
# modifica il messaggio dell'ultimo commit
git commit --amend -m "fix(scope): messaggio conforme"
# invia la correzione (forzando in modo sicuro)
git push --force-with-lease origin <branch>
```

- Riscrivere più commit (rebase interattivo):

```bash
# riapre gli ultimi N commit in rebase interattivo (sostituisci N)
git rebase -i HEAD~N
# nell'editor: sostituisci 'pick' con 'reword' per i commit da modificare,
# salva e chiudi; per ogni commit verrà aperto l'editor per inserire il nuovo messaggio
git push --force-with-lease origin <branch>
```

Nota: il `force-push` riscrive la storia del branch remoto; fallo solo su branch di feature personali o dopo aver concordato con i collaboratori.

### Verifica locale con `commitlint`

Il repository esegue `commitlint` in CI (action). Per controllare i messaggi in locale puoi usare uno strumento Node se lo hai:

```bash
# se hai node/npm installato
npx --no-install @commitlint/cli --config commitlint.config.js --edit
```

Se non hai Node a disposizione, affidati al workflow che bloccherà le PR non conformi; correggi i messaggi come sopra e riapri la PR.

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
