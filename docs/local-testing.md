# Sviluppo e test in locale

## Requisiti

- Python 3.14
- ffmpeg e ffprobe nel `PATH` (`apt install ffmpeg`, `brew install ffmpeg`):
  servono per convertire i media e per i test dei media (senza, quei test
  vengono saltati)
- deno, `curl_cffi` e `yt-dlp-ejs` arrivano con `requirements.txt`
  (`yt-dlp[default,curl-cffi,deno]`): il binario `deno` viene installato
  accanto a `python` nel virtualenv, dove yt-dlp lo cerca
- facoltativi: Docker con Compose, Helm (con il plugin helm-unittest), Node.js
  (commitlint)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
deno --version && python -c "import curl_cffi, yt_dlp_ejs"   # controllo rapido
```

## Test

```bash
REQUESTS_DB=/tmp/smr-tests/requests.db python -m pytest -q
```

- I test non usano la rete né Telegram.
- `REQUESTS_DB` punta a un database usa-e-getta (ogni test che usa il DB ne
  crea uno proprio; la variabile evita di toccare `data/requests.db`).
- I test dei media usano ffmpeg/ffprobe del `PATH`.

Lint (gli stessi controlli della CI):

```bash
pre-commit run --all-files                 # ruff, black, isort, whitespace, YAML
SKIP=helm-lint,actionlint pre-commit run --all-files   # senza helm / actionlint installati
python scripts/check_docs.py               # ogni impostazione è documentata
```

## Avviare bot e GUI

In due terminali, con lo stesso database:

```bash
# bot (polling): /health e /metrics su http://127.0.0.1:8080
export BOT_TOKEN="<token-di-un-bot-di-prova>"
export REQUESTS_DB="$PWD/data/requests.db"
python -m src.bot

# GUI: http://127.0.0.1:8081
export REQUESTS_DB="$PWD/data/requests.db"
export SECRET_KEY="$(openssl rand -hex 32)" ADMIN_TOKEN="$(openssl rand -hex 24)"
./scripts/run-dev.sh            # uvicorn con --reload, SESSION_COOKIE_SECURE=false
```

Usa un bot di prova: due processi con lo stesso token si rubano gli update a
vicenda (il bot in produzione smetterebbe di ricevere messaggi).

`python -m src` avvia lo stesso bot; `MODE=webhook` richiede `WEBHOOK_URL`
pubblico (ad esempio un tunnel HTTPS verso la porta 8080).

Diagnosi rapida del token (getMe, getWebhookInfo):

```bash
BOT_TOKEN="<token>" ./scripts/diagnose_bot.sh
```

## Docker Compose

```bash
cp examples/docker-compose.env.example .env
docker compose up --build
```

Bot e GUI condividono il volume `data`; la GUI è su <http://127.0.0.1:8081>.

## Chart Helm

```bash
helm lint charts/social-media-reuploader --strict
helm template smr charts/social-media-reuploader -f charts/social-media-reuploader/ci/webhook-ingress-values.yaml
helm plugin install https://github.com/helm-unittest/helm-unittest.git   # Helm 4: aggiungi --verify=false
helm unittest charts/social-media-reuploader
```

I file in `charts/social-media-reuploader/ci/` sono le combinazioni di valori
usate dalla CI (lint, render e validazione con kubeconform).
