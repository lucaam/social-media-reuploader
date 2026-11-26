# Contribuire

Grazie per contribuire a `social-media-reuploader`. Qui trovi come preparare
l'ambiente, le convenzioni e come funzionano CI e release.

## Ambiente di sviluppo

Requisiti:

- **Python 3.14**
- **ffmpeg e ffprobe** nel `PATH` (`sudo apt install ffmpeg`,
  `brew install ffmpeg`): servono al bot e ai test dei media
- **deno**: arriva con `requirements.txt` (`yt-dlp[default,curl-cffi,deno]`
  installa il binario accanto a `python` nel virtualenv, dove yt-dlp lo cerca);
  insieme arrivano `curl_cffi` (impersonation) e `yt-dlp-ejs` (challenge di
  YouTube)
- facoltativi: Docker con Compose, Helm 4 con il plugin helm-unittest,
  actionlint, Node.js per commitlint

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
deno --version && ffmpeg -version | head -n 1
npm ci                                    # commitlint (facoltativo)
pre-commit install --hook-type pre-commit --hook-type pre-push --hook-type commit-msg
```

## Test

```bash
REQUESTS_DB=/tmp/smr-tests/requests.db python -m pytest -q
```

I test non usano la rete né Telegram; i test dei media vengono saltati se
ffmpeg manca. Ogni nuova funzionalità o correzione arriva con i suoi test.

Per il chart Helm:

```bash
helm lint charts/social-media-reuploader --strict
helm unittest charts/social-media-reuploader
```

## Stile del codice

- `ruff` (regole predefinite di ruff 0.16, `target-version = "py314"`),
  `black` e `isort` (profilo black), configurati in `.ruff.toml` e
  `pyproject.toml`. Le versioni sono fissate in `requirements-dev.txt` e negli
  hook di `.pre-commit-config.yaml`: aggiornale insieme.
- Niente `except Exception: pass`: si catturano eccezioni specifiche e gli
  errori inattesi si registrano con `logger.exception` / `logger.warning(...,
  exc_info=True)`.
- Il codice async non blocca l'event loop: le chiamate al database passano da
  `await db.aio(...)`.
- Testi per gli utenti in italiano; log e codice in inglese. Il token del bot
  non va mai nei log.
- Ogni nuova impostazione va in `src/config.py`, nella tabella del README e
  nella mappa `env` di `charts/social-media-reuploader/values.yaml`; le
  impostazioni della GUI (lette in `src/gui_auth.py` con `_env`, `_env_bool`,
  `_env_int`) vanno nella tabella del README. `python scripts/check_docs.py`
  (eseguito in CI) lo verifica.

```bash
pre-commit run --all-files
SKIP=helm-lint,actionlint pre-commit run --all-files   # se non hai helm o actionlint
```

## Conventional Commits

I messaggi di commit seguono [Conventional Commits](https://www.conventionalcommits.org/)
(controllati da commitlint con `commitlint.config.mjs`, in locale e sulle PR):

- `feat: ...` nuova funzionalità → versione minor
- `fix: ...` correzione → patch
- `perf:`, `refactor:`, `docs:`, `test:`, `chore:`, `ci:`, `build:` → patch
- breaking change: `feat!: ...` oppure `BREAKING CHANGE: ...` nel corpo → major

Lo script `scripts/commit-msg-fix.sh` (hook `commit-msg`) accorcia gli header
oltre i 120 caratteri e lancia commitlint; `scripts/commitlint-pre-push.sh`
controlla i commit prima del push.

Correggere messaggi non conformi:

```bash
git commit --amend -m "fix(scope): messaggio conforme"     # ultimo commit
git rebase -i HEAD~N                                       # più commit: 'reword'
git push --force-with-lease origin <branch>
```

Riscrivi la storia solo sui tuoi branch di feature.

## CI

| Workflow | Quando | Cosa |
|---|---|---|
| `ci.yml` (job `build`) | PR, push su `main` | test con ffmpeg installato |
| `lint.yml` | PR, push su `main` | pre-commit, `check_docs.py`, Helm lint + unittest + kubeconform, actionlint, build della documentazione |
| `docker-pr-build.yml` | PR | build dell'immagine e smoke test (deno, curl_cffi, ffmpeg, import con filesystem in sola lettura) |
| `commitlint.yml` | PR | messaggi di commit |

I job che non riguardano i file modificati vengono saltati (e contano come
superati).

## Release

Le release partono dal workflow "Manual Version Bump" e si concludono con il
merge della PR di bump: vedi [RELEASE.md](RELEASE.md).
