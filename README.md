# Social Media Reuploader Bot

[![Release](https://img.shields.io/github/v/release/lucaam/social-media-reuploader?label=release)](https://github.com/lucaam/social-media-reuploader/releases/latest) [![GHCR Image Version](https://img.shields.io/docker/v/ghcr.io/lucaam/social-media-reuploader?label=ghcr.io&sort=semver)](https://github.com/lucaam/social-media-reuploader/pkgs/container/social-media-reuploader)

Bot Telegram che, quando in una chat viene incollato il link di un post social,
scarica il media con [yt-dlp](https://github.com/yt-dlp/yt-dlp) e lo ripubblica
nella chat come video, audio o album, così nessuno deve aprire l'app esterna.

- **Tutti i siti supportati da yt-dlp**: YouTube (anche Shorts), TikTok,
  Instagram, Facebook, X (Twitter), Reddit, Vimeo, Twitch, Dailymotion,
  SoundCloud, Bilibili e oltre 800 altri siti (più di 1700 estrattori).
  L'estrattore generico di yt-dlp non viene mai usato: un link è accettato solo
  se un estrattore dedicato lo riconosce.
- **Pronto per Telegram**: sceglie il formato H.264/AAC MP4 che entra nel limite
  di upload, altrimenti converte o comprime con ffmpeg; post con più media
  diventano un album.
- **Limiti per chat** (rate limit, coda, duplicati) e **GUI di amministrazione**
  (FastAPI) per vedere richieste, coda e limiti e per intervenire.
- Immagine Docker multi-arch (amd64/arm64) e chart Helm.

Documentazione completa: <https://lucaam.github.io/social-media-reuploader/>
(sorgenti in [`docs/`](docs/)).

> Rispetta il copyright e i termini d'uso dei siti: ripubblica solo contenuti
> che hai il diritto di condividere.

## Come funziona

1. Il bot legge testo e didascalie dei messaggi (anche link "nascosti" nel
   testo, link senza `https://` e link seguiti da punteggiatura) e scarta i
   duplicati nello stesso messaggio.
2. Per ogni link trova l'estrattore yt-dlp che lo gestisce. I link brevi di
   condivisione (`fb.watch`, `v.redd.it`, `pin.it`, `/share/` di Facebook e
   Instagram, ...) vengono risolti; i link a Telegram vengono ignorati; profili,
   canali e playlist sono rifiutati (si accetta il link al singolo post) a meno
   di `ALLOW_COLLECTION_LINKS=true`.
3. La richiesta entra nella coda (salvata nel database SQLite): il bot mette la
   reazione 👀 sul messaggio, estrae i metadati, scarica, converte se serve e
   invia il media in risposta al messaggio originale. Al termine la reazione
   diventa 🏆 (fatto) o 🍌 (errore).
4. I file piccoli e progressivi possono essere scaricati direttamente dai server
   di Telegram (`TELEGRAM_SERVER_FETCH`), senza passare dal bot.

### Chat private e gruppi

| | Chat privata | Gruppo / supergruppo / canale |
|---|---|---|
| Link non supportato | risposta con l'elenco dei siti supportati (`UNSUPPORTED_LINK_REPLY=private`, default) | nessuna risposta (salvo `UNSUPPORTED_LINK_REPLY=always`) |
| Link rifiutato (rate limit, troppi link in coda, duplicato) | messaggio con il motivo e quando riprovare (avvisi limitati da `NOTIFY_*_THROTTLE_SECONDS`) | nessun messaggio |
| Errore (contenuto privato, rimosso, troppo lungo, live, ...) | messaggio con il motivo | solo la reazione 🍌 |
| Errore "troppo grande" (vedi [Limiti](#limiti)) | messaggio con il motivo e il link originale | uguale |
| Successo | media in risposta al messaggio, reazione 🏆 | uguale |

Se Telegram rifiuta le reazioni (reazioni disattivate o limitate nella chat,
messaggio originale già cancellato), il bot risponde al link con "In lavorazione
👀", anche nei gruppi: il messaggio viene cancellato quando il media arriva e
diventa la spiegazione dell'errore se la richiesta fallisce. Dopo un rifiuto il
bot non riprova le reazioni in quella chat per `REACTION_DISABLE_SECONDS`.

I testi per gli utenti sono in italiano. Se un messaggio viene modificato,
vengono elaborati solo i link nuovi.

**Gruppi e privacy mode**: con le impostazioni predefinite di BotFather un bot
non riceve i normali messaggi dei gruppi. Disattiva la privacy mode
(`/setprivacy` → *Disable* in @BotFather, poi rimuovi e riaggiungi il bot ai
gruppi esistenti) oppure rendi il bot amministratore del gruppo.

### Limiti

- **Dimensione**: l'API cloud di Telegram accetta upload fino a 50 MB
  (`TELEGRAM_MAX_FILE_SIZE`). I file più grandi vengono compressi (risoluzione
  ridotta solo se necessario); se non si riesce a stare nel limite la richiesta
  fallisce con "troppo grande". Con un [Bot API server locale](https://github.com/tdlib/telegram-bot-api)
  (`TELEGRAM_API_BASE`) si arriva a 2 GB.
- **Download**: al massimo `MAX_DOWNLOAD_BYTES` (500 MB) prima della
  compressione e `MAX_DURATION_SECONDS` (1 ora) di durata; le dirette non sono
  supportate. Un post con più media produce un album di al massimo
  `MAX_ITEMS_PER_LINK` (10) elementi.
- **Rate limit per chat** (`RATE_LIMITS`, default `1/10,3/60,5/3600,20/30d`):
  1 link ogni 10 secondi, 3 al minuto, 5 all'ora, 20 ogni 30 giorni. Se il
  prossimo slot libero arriva entro `RATE_LIMIT_MAX_DEFER_SECONDS` (2 minuti) il
  link viene accettato e elaborato automaticamente più tardi; altrimenti è
  rifiutato con l'indicazione di quando riprovare.
- **Coda per chat**: al massimo `MAX_PENDING_PER_CHAT` (3) richieste in coda o in
  lavorazione; i link in più vengono rifiutati.
- **Duplicati**: lo stesso link (normalizzato: `youtu.be/ID`, `shorts/ID` e
  `watch?v=ID` sono lo stesso video) nella stessa chat entro
  `DUPLICATE_WINDOW_SECONDS` (90 minuti) non viene ripubblicato.
- I link rifiutati non consumano quota e non bloccano gli altri link della chat.
  Dalla GUI si possono azzerare i limiti di una chat e rimettere in coda le
  richieste.
- Contenuti che richiedono il login (es. molti post Instagram/Facebook)
  funzionano solo con un file di cookie (`YTDLP_COOKIES_PATH`).

## Avvio rapido

### Docker Compose (bot + GUI)

```bash
cp examples/docker-compose.env.example .env   # inserisci BOT_TOKEN, SECRET_KEY, ADMIN_TOKEN
docker compose up -d --build
```

Il bot gira in polling; la GUI è su <http://127.0.0.1:8081> (login con
`ADMIN_TOKEN`). I due container condividono il database nel volume `data`.

### Python (sviluppo)

Servono Python 3.14 e ffmpeg (`deno`, `curl_cffi` e `yt-dlp-ejs` arrivano con
`requirements.txt`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="<token>"            # MODE=polling è il default
python -m src.bot                     # bot: /health e /metrics su :8080
# in un altro terminale
export SECRET_KEY="$(openssl rand -hex 32)" ADMIN_TOKEN="$(openssl rand -hex 24)"
export SESSION_COOKIE_SECURE=false
uvicorn src.gui:app --host 127.0.0.1 --port 8081
```

Bot e GUI devono usare lo stesso `REQUESTS_DB` (default `data/requests.db`
nella cartella corrente).

### Kubernetes (Helm)

```bash
kubectl create secret generic smr-bot --from-literal=BOT_TOKEN='<token>'
helm upgrade --install smr oci://ghcr.io/lucaam/charts/social-media-reuploader \
  --version <versione> --set secret.name=smr-bot
```

Il chart è pubblicato su GHCR a ogni [release](https://github.com/lucaam/social-media-reuploader/releases)
con la stessa versione dell'immagine, che installa (`<versione>`, es. `0.9.0`;
senza `--version` l'ultima). Crea il PVC del database, la GUI sullo stesso
nodo del bot e un Secret con `SECRET_KEY` e `ADMIN_TOKEN` generati (vedi le
note mostrate da Helm). Tutti i dettagli in [docs/deploy.md](docs/deploy.md).

Il chart nella cartella `charts/` del repository è solo per lo sviluppo: ha
la versione segnaposto `0.0.0` e va installato con
`--set image.tag=<versione>`.

### Versioni e release

Le release sono automatiche: ogni merge su `main` con una correzione o una
funzionalità ([Conventional Commits](https://www.conventionalcommits.org/))
pubblica la versione successiva `vX.Y.Z`, cioè l'immagine
`ghcr.io/lucaam/social-media-reuploader:X.Y.Z` (amd64/arm64, anche
`:latest`) e il chart `X.Y.Z`, firmati con cosign e con attestazione di
provenienza ([come verificarli](docs/ghcr.md)). L'immagine riceve la versione
in fase di build (`SMR_VERSION`): compare nei log di avvio e in `/health`
con `HEALTH_DEBUG=true`; un'immagine costruita in locale riporta
`0.0.0-dev`. Dettagli in [RELEASE.md](RELEASE.md).

## Configurazione

Tutte le impostazioni sono variabili d'ambiente. I valori non validi non
bloccano l'avvio: viene usato il default e il problema compare nei log
all'avvio. Nel chart Helm le impostazioni dell'applicazione si passano con la
mappa `env` di `values.yaml` (numeri e booleani vengono resi come stringhe).

<!-- section:configurazione:start -->
#### Telegram e processo del bot

| Variabile | Default | Descrizione |
|---|---|---|
| `BOT_TOKEN` | obbligatorio | Token del bot fornito da @BotFather. Serve solo al processo del bot. |
| `MODE` | `polling` | `polling` oppure `webhook`. In webhook serve `WEBHOOK_URL`; senza, il bot torna in polling con un avviso. In polling il webhook eventualmente registrato viene rimosso. |
| `WEBHOOK_URL` | – | URL pubblico HTTPS a cui Telegram invia gli update, es. `https://bot.example.com/webhook`. Non mettere il token nell'URL. |
| `WEBHOOK_PATH` | `/webhook` | Percorso servito dal bot per gli update in modalità webhook: l'URL pubblico deve arrivare qui. |
| `WEBHOOK_SECRET` | derivato da `BOT_TOKEN` | Secret verificato nell'header `X-Telegram-Bot-Api-Secret-Token` (1-256 caratteri `A-Z`, `a-z`, `0-9`, `_`, `-`). Se manca viene derivato in modo stabile dal token. |
| `HOST` | `0.0.0.0` | Indirizzo di ascolto del server HTTP del bot. |
| `PORT` | `8080` | Porta del server HTTP del bot: `/health`, `/metrics` e, in webhook, `WEBHOOK_PATH`. |
| `WORKERS` | `2` | Richieste elaborate in parallelo (1-32). |
| `TELEGRAM_MAX_FILE_SIZE` | `52428800` (50 MB) | Dimensione massima di un upload. Oltre 50 MB solo con un Bot API server locale (`TELEGRAM_API_BASE`). |
| `TELEGRAM_URL_FETCH_MAX_SIZE` | `20971520` (20 MB) | Dimensione massima dei file che Telegram scarica direttamente dall'URL. |
| `TELEGRAM_SERVER_FETCH` | `true` | Prova prima a far scaricare a Telegram i file piccoli e progressivi (risparmia banda e CPU; mai per YouTube). |
| `TELEGRAM_API_TIMEOUT` | `60` | Timeout in secondi delle chiamate alla Bot API. |
| `TELEGRAM_UPLOAD_TIMEOUT` | `300` | Timeout in secondi degli upload. |
| `TELEGRAM_MAX_RETRIES` | `3` | Tentativi automatici su 429 (flood wait) ed errori di rete (0-10). Gli invii di media non vengono mai ripetuti dopo un timeout, per evitare doppi post. |
| `REACTION_DISABLE_SECONDS` | `300` | Secondi per cui le reazioni restano disattivate in una chat dopo un rifiuto di Telegram. |
| `TELEGRAM_API_BASE` | – | URL di un Bot API server locale (upload fino a 2 GB). |

#### Log e health check

| Variabile | Default | Descrizione |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Livello dei log: `DEBUG`, `INFO`, `WARNING`, `ERROR` o `CRITICAL`. Il token del bot viene oscurato nei log. |
| `LOG_TO_FILE` | `false` | Scrive i log anche su `LOG_FILE`. |
| `LOG_FILE` | `data/telegram_downloader.log` | File di log, relativo alla cartella di lavoro (nel container `/app`, quindi sul volume dati). |
| `LOG_MAX_BYTES` | `5242880` (5 MB) | Dimensione oltre cui il file di log ruota. |
| `LOG_BACKUP_COUNT` | `5` | File di log ruotati conservati. |
| `HEALTH_DEBUG` | `false` | Aggiunge dettagli diagnostici a `/health`. Da lasciare spento in produzione. |

#### Download (yt-dlp)

| Variabile | Default | Descrizione |
|---|---|---|
| `TMP_DIR` | `/tmp/telegram_downloader` | Cartella delle directory di lavoro di ogni richiesta (cancellate a fine richiesta; quelle vecchie vengono ripulite periodicamente). |
| `KEEP_DOWNLOADED_FILES` | `false` | Non cancella le directory di lavoro (debug). |
| `SIMPLE_YTDLP_ONLY` | `false` | Invia l'output di yt-dlp così com'è, senza conversioni (debug). |
| `EXTRACT_TIMEOUT` | `60` | Timeout in secondi dell'estrazione dei metadati (min 5). |
| `DOWNLOAD_TIMEOUT` | `300` | Timeout in secondi del download di ogni media (min 10). |
| `MAX_DOWNLOAD_BYTES` | `524288000` (500 MB) | Dimensione massima scaricata prima della compressione. |
| `MAX_DURATION_SECONDS` | `3600` | Rifiuta i media più lunghi (0 = nessun limite). |
| `MAX_ITEMS_PER_LINK` | `10` | Media massimi pubblicati per un post multiplo o album (1-10). |
| `YTDLP_FORMAT` | – | Selezione del formato di yt-dlp (`-f`) al posto di quella predefinita (MP4 H.264/AAC che entra nel limite di upload, altrimenti i flussi migliori da convertire). |
| `YTDLP_FORMAT_SORT` | – | Ordinamento dei formati di yt-dlp (`-S`). |
| `YTDLP_USER_AGENT` | – | User-Agent usato da yt-dlp. |
| `YTDLP_COOKIES_PATH` | – | File `cookies.txt` (formato Netscape) per i contenuti che richiedono il login. Il file non viene mai modificato: yt-dlp lavora su una copia. Nome legacy: `YTDLP_COOKIES`. |
| `YTDLP_COOKIES_FROM_BROWSER` | – | Opzione `--cookies-from-browser` di yt-dlp; richiede un profilo del browser nel container, meglio `YTDLP_COOKIES_PATH`. |
| `YTDLP_HEADERS` | – | Header HTTP aggiuntivi nel formato `Header: Valore`; più header si separano con una barra verticale. |
| `YTDLP_IMPERSONATE` | – | Target di `--impersonate` (es. `chrome`); curl_cffi è già installato. |
| `YTDLP_PROXY` | – | Proxy per yt-dlp, es. `socks5://host:1080`. La risoluzione degli share link (es. `pin.it`, `fb.watch`) lo usa solo se è un proxy `http(s)`; con un proxy SOCKS quelle richieste partono dirette (avviso all'avvio). |

#### Provider

| Variabile | Default | Descrizione |
|---|---|---|
| `ALLOWED_PROVIDERS` | tutti | Provider ammessi, separati da virgola: nomi degli estrattori di yt-dlp (`youtube`, `tiktok`, `instagram`, `twitter`, `reddit`, ...) o domini (`vimeo.com`). Il dominio di un sito vale per tutto il sito e i suoi altri domini (`youtube.com` copre anche `youtu.be`, `x.com` anche `twitter.com`); un sottodominio (es. `vm.tiktok.com`) vale solo per quell'host. Vuoto = tutti i siti supportati da yt-dlp. |
| `BLOCKED_PROVIDERS` | – | Provider esclusi, stesso formato. |
| `ALLOW_COLLECTION_LINKS` | `false` | Accetta anche link a profili, canali, playlist e album (vengono pubblicati i primi `MAX_ITEMS_PER_LINK` elementi). |
| `UNSUPPORTED_LINK_REPLY` | `private` | Quando rispondere ai link che il bot non può gestire: `private` (solo chat private), `always`, `never`. |
| `MAX_LINKS_PER_MESSAGE` | `10` | Link massimi di un singolo messaggio che vengono controllati e messi in coda (1-50); gli altri vengono ignorati. |

#### Conversione (ffmpeg)

| Variabile | Default | Descrizione |
|---|---|---|
| `WORKER_GENERATE_THUMBNAIL` | `true` | Genera la miniatura dei video. |
| `TRANSCODE_PRESET` | `veryfast` | Preset di x264: `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, `medium`, `slow`, `slower`, `veryslow` o `placebo` (più lento = file più piccoli, più CPU). |
| `TRANSCODE_TIMEOUT` | `900` | Timeout in secondi di una conversione completa (min 30). |
| `TRANSCODE_CONCURRENCY` | `1` | Conversioni ffmpeg contemporanee (1-8): sono il lavoro più pesante per la CPU. |
| `TRANSCODE_MAX_SIDE` | `1280` | Lato lungo in pixel quando la compressione richiede di ridurre la risoluzione (min 240). |

#### Coda, rate limit e duplicati

| Variabile | Default | Descrizione |
|---|---|---|
| `MAX_PENDING_PER_CHAT` | `3` | Richieste in coda o in lavorazione per chat; oltre, il nuovo link viene rifiutato (la chat non viene bloccata). |
| `RATE_LIMITS` | `1/10,3/60,5/3600,20/30d` | Finestre scorrevoli per chat nel formato `massimo/finestra`, finestra in secondi o con suffisso `s`, `m`, `h`, `d`. Una stringa vuota disattiva il rate limit. |
| `RATE_LIMIT_MAX_DEFER_SECONDS` | `120` | Se il prossimo slot libero arriva entro questo tempo, il link viene accettato ed elaborato più tardi in automatico; altrimenti viene rifiutato con l'indicazione di quando riprovare. |
| `DUPLICATE_WINDOW_SECONDS` | `5400` (90 min) | Lo stesso link (normalizzato) nella stessa chat entro questa finestra è un duplicato. |
| `NOTIFY_RATE_THROTTLE_SECONDS` | `60` | Intervallo minimo tra due avvisi di rate limit alla stessa chat. |
| `NOTIFY_DUPLICATE_THROTTLE_SECONDS` | `600` | Intervallo minimo tra due avvisi di duplicato alla stessa chat. |
| `WORKER_REHYDRATE_ON_START` | `true` | All'avvio riprende le richieste rimaste in coda; con `false` le segna come `aborted`. |
| `WORKER_POLL_SECONDS` | `2` | Ogni quanto il worker controlla la coda nel database, ad esempio per le richieste rimesse in coda dalla GUI (min 0.2). Nome legacy: `WORKER_PERIODIC_REHYDRATE_SECONDS`. |
| `WORKER_HEARTBEAT_SECONDS` | `5` | Ogni quanto il worker pubblica il proprio stato per la GUI. |
| `WORKER_SHUTDOWN_TIMEOUT` | `30` | Secondi concessi alle richieste in corso allo spegnimento prima di annullarle (il chart usa il grace period del pod meno 15 secondi). Docker deve concedere al container almeno questo valore più 45 secondi prima di ucciderlo (`stop_grace_period: 75s` in `docker-compose.yml`, `--stop-timeout` con `docker run`). |
| `WORKER_MAX_ATTEMPTS` | `3` | Tentativi massimi di una richiesta: ogni presa in carico da parte del worker conta come un tentativo, anche se viene interrotta da uno spegnimento. Arrivati a questo numero gli errori temporanei non vengono più ritentati e una richiesta interrotta da un crash viene segnata come fallita all'avvio successivo. "Rimetti in coda" dalla GUI azzera il conteggio. |
| `WORKER_RETRY_BACKOFF_SECONDS` | `30` | Base del backoff esponenziale per gli errori temporanei (30, 60, 120 s, ... fino a 15 minuti, mai meno del `retry_after` di Telegram). |

#### Database e retention

| Variabile | Default | Descrizione |
|---|---|---|
| `REQUESTS_DB` | `data/requests.db` | File SQLite condiviso da bot e GUI (nel container `/app/data/requests.db`). Deve stare su un filesystem locale, mai su NFS/SMB. `:memory:` solo per i test. |
| `UPDATES_RETENTION_DAYS` | `14` | Giorni di log degli eventi conservati (0 = per sempre). |
| `REQUESTS_RETENTION_DAYS` | `0` | Giorni di richieste concluse conservati (0 = per sempre). Le richieste che contano ancora per `RATE_LIMITS` non vengono mai cancellate. |
| `TMP_RETENTION_HOURS` | `24` | Con `KEEP_DOWNLOADED_FILES` attivo, ore dopo cui le directory di lavoro vengono comunque rimosse dalla pulizia periodica di `TMP_DIR` (senza, le directory orfane vengono rimosse dopo 1 ora). |

#### GUI di amministrazione

| Variabile | Default | Descrizione |
|---|---|---|
| `SECRET_KEY` | obbligatorio | Chiave che firma i cookie di sessione: almeno 32 caratteri casuali (`openssl rand -hex 32`). Cambiarla chiude tutte le sessioni. |
| `ADMIN_TOKEN` | – | Token di accesso amministratore (login con token o header `Authorization: Bearer`). Necessario se OAuth non è configurato. |
| `SESSION_COOKIE_SECURE` | `auto` | `auto`: il cookie di sessione è `Secure` quando la richiesta arriva in HTTPS (direttamente o da un reverse proxy fidato, vedi `FORWARDED_ALLOW_IPS`); `true`/`false` lo forzano. |
| `SESSION_MAX_AGE_SECONDS` | `43200` (12 h) | Durata massima di una sessione della GUI (300-604800). |
| `GUI_TRUSTED_ORIGINS` | – | Origin aggiuntive (separate da virgola, es. `https://admin.example.com`) accettate per le richieste che modificano dati e per il WebSocket, quando un proxy riscrive l'header `Host`. |
| `OAUTH_CLIENT_ID` | – | Client ID OAuth2 / OpenID Connect. |
| `OAUTH_CLIENT_SECRET` | – | Client secret OAuth2. |
| `OAUTH_SERVER_METADATA_URL` | – | URL di discovery OpenID (`.../.well-known/openid-configuration`): consigliato, ricava endpoint e JWKS. |
| `OAUTH_AUTHORIZE_URL` | – | Endpoint di autorizzazione, per provider senza discovery (es. GitHub). |
| `OAUTH_TOKEN_URL` | – | Endpoint del token. |
| `OAUTH_USERINFO_URL` | – | Endpoint userinfo. |
| `OAUTH_JWKS_URI` | – | JWKS per verificare l'id_token, se non arriva dalla discovery. |
| `OAUTH_SCOPE` | `openid profile email` | Scope richiesti al provider. |
| `OAUTH_REDIRECT_URI` | derivato dalla richiesta | URL pubblico di callback, es. `https://admin.example.com/auth`: impostalo quando la GUI è dietro un reverse proxy. |
| `OAUTH_ADMIN_GROUPS` | – | Gruppi del provider (separati da virgola) i cui membri sono amministratori. |
| `OAUTH_ISSUER` | – | `iss` atteso nell'id_token quando non si usa la discovery. |
| `OAUTH_ASSUME_EMAIL_VERIFIED` | `false` | Il ruolo amministratore registrato nella GUI vale solo per e-mail con `email_verified=true`. Impostalo a `true` solo per provider che non inviano quel claim ma restituiscono esclusivamente e-mail verificate (es. GitHub). Non usarlo con Microsoft Entra ID, dove il claim `email` non è verificato: lì usa `OAUTH_ADMIN_GROUPS`. |
| `OAUTH_TIMEOUT_SECONDS` | `10` | Timeout delle chiamate HTTP verso il provider OAuth (1-60). |
| `GUI_DB_POLL_SECONDS` | `1` | Ogni quanto la GUI legge il log degli eventi dal database per gli aggiornamenti live (min 0.2). |
| `GUI_REQUEUE_MAX_AGE_SECONDS` | `86400` (24 h) | "Sblocca e rimetti in coda" rimette in coda solo i link rifiutati più recenti di così. |
<!-- section:configurazione:end -->

## Note di deploy

- **Un solo processo bot per token.** Telegram consegna gli update a un solo
  consumatore (polling) o a un solo webhook, e il bot è l'unico worker della
  coda: non avviare mai due repliche. Il chart Helm rifiuta
  `replicaCount > 1` e aggiorna il bot con la strategia `Recreate`.
- **Database condiviso sullo stesso nodo.** Bot e GUI comunicano solo tramite il
  file SQLite (`REQUESTS_DB`, in modalità WAL): devono vedere lo stesso file su
  un filesystem locale. Con Docker Compose è il volume `data`; con Helm è un PVC
  `ReadWriteOnce` e la GUI viene schedulata sullo stesso nodo del bot
  (`podAffinity` obbligatoria su `kubernetes.io/hostname`). Niente NFS.
- **Accesso alla GUI.** La GUI richiede una `SECRET_KEY` robusta (chi la conosce
  può firmare sessioni da amministratore) e `ADMIN_TOKEN` oppure OAuth per il
  login. Esponila solo in HTTPS dietro un reverse proxy fidato da uvicorn
  (il cookie di sessione diventa `Secure` da solo; `SESSION_COOKIE_SECURE=true`
  lo forza); con OAuth imposta `OAUTH_REDIRECT_URI`. Le sessioni sono salvate
  nel database: l'uscita e il pulsante "Termina" nella scheda Utenti le
  chiudono davvero, anche se il cookie fosse stato copiato. Docker Compose
  pubblica le porte solo su `127.0.0.1`.
- **Health e metriche.** Il bot espone `/health` e `/metrics` (Prometheus) su
  `PORT` sia in polling sia in webhook; la GUI espone `/health`. Non pubblicare
  `/metrics` su Internet: in webhook instrada verso il bot solo `WEBHOOK_PATH`.
- **Webhook.** `MODE=webhook` con `WEBHOOK_URL` HTTPS pubblico che arriva al
  servizio del bot su `WEBHOOK_PATH`; Telegram autentica le richieste con
  `WEBHOOK_SECRET`. In tutti gli altri casi il polling è più semplice.
- **yt-dlp aggiornato.** I siti cambiano spesso: aggiorna l'immagine quando esce
  una nuova versione di yt-dlp (Renovate apre le PR in automatico).

Altre guide: [utilizzo](docs/usage.md), [deploy](docs/deploy.md),
[OAuth](docs/oauth.md), [sviluppo e test in locale](docs/local-testing.md),
[immagini e release](docs/ghcr.md), [contribuire](CONTRIBUTING.md).

## Sviluppo

```bash
pip install -r requirements.txt -r requirements-dev.txt
npm ci                                # facoltativo: commitlint per l'hook commit-msg
pre-commit install && pre-commit install --hook-type pre-push --hook-type commit-msg
REQUESTS_DB=/tmp/smr-tests/requests.db python -m pytest -q   # i test dei media richiedono ffmpeg
```

Commit nel formato [Conventional Commits](https://www.conventionalcommits.org/);
dettagli in [CONTRIBUTING.md](CONTRIBUTING.md).
