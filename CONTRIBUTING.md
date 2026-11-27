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
  Node.js per commitlint (actionlint, shellcheck e zizmor li installa
  pre-commit)

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
SKIP=helm-lint pre-commit run --all-files   # se non hai helm
```

Tutti gli hook sono fissati a una versione in `.pre-commit-config.yaml`
(aggiornati da Renovate); solo `helm-lint` usa lo `helm` del sistema.

## Conventional Commits

I messaggi di commit seguono [Conventional Commits](https://www.conventionalcommits.org/)
(controllati da commitlint con `commitlint.config.mjs`). Le PR vengono unite
con **squash**: il commit che arriva su `main` è il **titolo della PR**, che
il check `commitlint` controlla e che decide la release automatica:

- `feat: ...` nuova funzionalità → release minor
- `fix: ...`, `perf: ...`, `revert: ...`, `fix(deps): ...`, `build(deps): ...`
  → patch
- breaking change: `feat!: ...` (il `!` nel titolo della PR) oppure il footer
  `BREAKING CHANGE: ...` → major, minor finché la versione è `0.y.z`
- `refactor:`, `docs:`, `test:`, `chore:`, `ci:`, `style:`, `build:` (tranne
  `build(deps)`) → nessuna release (arrivano con la release successiva)

Il riepilogo del check `commitlint` dice che release produrrà la PR;
`python scripts/release/version.py check-title "<titolo>"` fa lo stesso in
locale.

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
| `ci.yml` | PR; su `main` richiamato da `release.yml` | test con ffmpeg (job `build`), pre-commit e `check_docs.py`, Helm lint + unittest + kubeconform, actionlint + zizmor, build della documentazione, build e smoke test dell'immagine (`scripts/smoke_test_image.sh`); solo sulle PR che toccano immagine o release, la *Release dry run* (piano, chart e immagine `linux/amd64` e `linux/arm64` costruiti come nella release, su un registry locale; check non obbligatori) |
| `pr-title.yml` | PR | titolo della PR (job `commitlint`) e release che produrrà |
| `release.yml` | push su `main`, avvio manuale | stessi controlli di `ci.yml`, poi versione, immagine (amd64 e arm64 su runner nativi), indice firmato e chart; infine un solo job pubblica tag dell'immagine, chart, tag e release ([RELEASE.md](RELEASE.md)) |
| `docs-deploy.yml` | push su `main` che tocca la documentazione | sito su GitHub Pages |

Sulle PR i job che non riguardano i file modificati vengono saltati (e
contano come superati), tranne i test (`build`) e "Python linters", che
girano sempre. Le azioni sono fissate per commit SHA e ogni job dichiara solo
i permessi che usa.

## Release

Automatiche: ogni merge su `main` con un commit rilasciabile pubblica
immagine, chart, tag e release GitHub, senza passi manuali. Dettagli in
[RELEASE.md](RELEASE.md).
