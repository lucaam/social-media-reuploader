# Deploy

Un'unica immagine contiene bot e GUI:
`ghcr.io/lucaam/social-media-reuploader:<versione>` (linux/amd64 e
linux/arm64). Comprende ffmpeg/ffprobe, yt-dlp con `curl_cffi`
(impersonation), `yt-dlp-ejs` e il runtime JavaScript `deno` usato per
YouTube. Gira come utente non privilegiato (uid/gid 1000) e funziona anche con
il filesystem root in sola lettura: le uniche cartelle scritte sono `/app/data`
(database) e `/tmp` (download e cache).

| Processo | Comando | Porta |
|---|---|---|
| bot | `python -m src.bot` (default dell'immagine) | `PORT`, default 8080: `/health`, `/metrics`, in webhook `WEBHOOK_PATH` |
| GUI | `uvicorn src.gui:app --host 0.0.0.0 --port 8081` | 8081: GUI e `/health` |

Regole valide per ogni deploy:

1. **Un solo bot per token**: mai due repliche o due container del bot con lo
   stesso `BOT_TOKEN`.
2. **Bot e GUI condividono lo stesso file SQLite** (`REQUESTS_DB`) su un
   filesystem locale dello stesso host/nodo. Niente NFS o SMB: SQLite in
   modalità WAL ha bisogno dei lock del kernel locale.
3. **La GUI va protetta**: `SECRET_KEY` robusta, `ADMIN_TOKEN` o OAuth, HTTPS.

## Docker

```bash
docker volume create smr-data
docker run -d --name smr-bot --restart unless-stopped --stop-timeout 75 \
  -e BOT_TOKEN="$BOT_TOKEN" \
  -v smr-data:/app/data \
  -p 127.0.0.1:8080:8080 \
  ghcr.io/lucaam/social-media-reuploader:latest

docker run -d --name smr-gui --restart unless-stopped \
  -e SECRET_KEY="$(openssl rand -hex 32)" -e ADMIN_TOKEN="$ADMIN_TOKEN" \
  -e SESSION_COOKIE_SECURE=false -e PORT=8081 \
  -v smr-data:/app/data \
  -p 127.0.0.1:8081:8081 \
  ghcr.io/lucaam/social-media-reuploader:latest \
  uvicorn src.gui:app --host 0.0.0.0 --port 8081
```

`PORT=8081` serve solo all'HEALTHCHECK dell'immagine, che controlla `$PORT`.
Per una versione precisa sostituisci `latest` con il numero di release.

`--stop-timeout 75`: allo stop Docker manda SIGTERM e, dopo 10 secondi se non
indicato, SIGKILL. Il bot aspetta fino a `WORKER_SHUTDOWN_TIMEOUT` (30 s) le
richieste in corso e poi concede 15 secondi a quelle annullate per tornare in
coda; un upload interrotto da SIGKILL viene segnato come fallito e non viene
più inviato. Tieni il timeout ad almeno `WORKER_SHUTDOWN_TIMEOUT` + 45 secondi.
Dietro un reverse proxy aggiungi alla GUI `-e FORWARDED_ALLOW_IPS=<indirizzo del
proxy>` (vedi sotto).

## Docker Compose

`docker-compose.yml` avvia bot e GUI con il volume `data` condiviso e
`REQUESTS_DB=/app/data/requests.db` in entrambi:

```bash
cp examples/docker-compose.env.example .env   # BOT_TOKEN, SECRET_KEY, ADMIN_TOKEN
docker compose up -d --build
docker compose logs -f bot
```

- `BOT_TOKEN` e `SECRET_KEY` sono obbligatorie: senza, Compose non parte. La
  GUI si ferma all'avvio anche senza `ADMIN_TOKEN` o una configurazione OAuth
  completa.
- Le porte sono pubblicate solo su `127.0.0.1` (`BIND_ADDRESS` per cambiarlo):
  per esporre la GUI usa un reverse proxy con HTTPS e imposta
  `SESSION_COOKIE_SECURE=true`.
- Dietro il reverse proxy imposta nel file `.env` anche `FORWARDED_ALLOW_IPS`
  con l'indirizzo da cui il proxy si collega alla GUI (è quello che compare nei
  log di accesso della GUI; con il proxy sullo stesso host e le porte su
  `127.0.0.1` è il gateway della rete di Compose:
  `docker network inspect social-media-reuploader_default --format '{{(index .IPAM.Config 0).Gateway}}'`).
  Senza, uvicorn ignora gli header `X-Forwarded-*` e tutti i client sembrano il
  proxy: dieci token sbagliati in 5 minuti, inviati da chiunque, bloccano il
  login con token per tutti. Evita `*`, che si fida anche di un
  `X-Forwarded-For` scritto dal client.
- Il bot ha `stop_grace_period: 75s`: allo stop (`docker compose down`,
  `restart`, `up -d` dopo una build) le richieste in corso possono finire. Se
  aggiungi `WORKER_SHUTDOWN_TIMEOUT` all'`environment`, tieni
  `stop_grace_period` ad almeno quel valore + 45 secondi.
- Le altre impostazioni (tabella in [Configurazione](configuration.md)) si
  aggiungono nel file `.env`; quelle non elencate in `docker-compose.yml` vanno
  aggiunte anche alla sezione `environment`.
- Cookie per i siti che richiedono il login: monta il file in sola lettura e
  imposta `YTDLP_COOKIES_PATH` (esempio commentato nel file).

## Kubernetes con Helm

Il chart è in `charts/social-media-reuploader`. Usa quello del tag di una
release (`git checkout v<versione>`): il chart di ogni release usa l'immagine
della stessa versione (`image.tag`), mentre quello di `main` può richiedere
funzioni di un'immagine non ancora pubblicata (ad esempio la readiness probe su
`/ready`): con un'immagine più vecchia il pod del bot non diventa mai pronto.

```bash
kubectl create namespace smr
kubectl -n smr create secret generic smr-bot --from-literal=BOT_TOKEN='<token>'
helm upgrade --install smr charts/social-media-reuploader -n smr \
  --set secret.name=smr-bot
```

Cosa viene creato:

- **Deployment del bot**: 1 replica (il chart rifiuta `replicaCount > 1`;
  `0` ferma il bot), strategia `Recreate` (il vecchio pod si ferma prima che
  parta il nuovo), probe di avvio e liveness su `/health` e di readiness su
  `/ready`, `WORKER_SHUTDOWN_TIMEOUT` pari al grace
  period meno 15 secondi, emptyDir con `sizeLimit` su `/tmp` per download e
  conversioni.
- **PVC del database** (`<release>-social-media-reuploader-data`,
  `ReadWriteOnce`, 1Gi): abilitato di default e conservato da `helm uninstall`
  (`helm.sh/resource-policy: keep`).
- **Deployment della GUI** sullo stesso nodo del bot (`podAffinity`
  obbligatoria su `kubernetes.io/hostname`, perché il volume è
  `ReadWriteOnce`), con uvicorn dietro proxy (`--proxy-headers`,
  `--forwarded-allow-ips` da `gui.forwardedAllowIps`).
- **Secret della GUI** (`<release>-social-media-reuploader-gui-secret`) con
  `SECRET_KEY` e, senza OAuth, `ADMIN_TOKEN` generati una volta e mantenuti
  agli upgrade:

  ```bash
  kubectl -n smr get secret smr-social-media-reuploader-gui-secret \
    -o jsonpath='{.data.ADMIN_TOKEN}' | base64 -d; echo
  kubectl -n smr port-forward svc/smr-social-media-reuploader-gui 8081:8081
  ```

- Service del bot (porta 80 → `http`) e della GUI (porta `gui.port`),
  ServiceAccount senza token dell'API montato.

Tutti i container girano come uid 1000, senza capability, con
`readOnlyRootFilesystem` e profilo seccomp `RuntimeDefault`.

!!! note "GitOps con `helm template` (Argo CD)"
    I valori generati usano `lookup`, che con `helm template` non vede il
    cluster: a ogni render cambierebbero. Imposta `gui.secrets.secretKey` e
    `gui.secrets.adminToken` oppure usa `gui.existingSecret`. Flux usa
    l'SDK di Helm e non ha questo problema.

### Segreti

- **Token del bot**: `secret.name` punta a un Secret esistente (chiave
  `secret.botTokenKey`, default `BOT_TOKEN`; facoltativa la chiave
  `secret.webhookSecretKey`, default `WEBHOOK_SECRET`). In alternativa
  `--set-string secret.botToken=...` fa creare il Secret al chart (il valore
  resta comunque nei dati della release Helm). Il token non finisce mai in
  chiaro nello spec del pod.
- **GUI**: `gui.existingSecret` per un Secret già presente (gestito con
  SealedSecrets, External Secrets, SOPS...) con le chiavi `SECRET_KEY`,
  `ADMIN_TOKEN`, `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, ... caricate con
  `envFrom`; altrimenti i valori di `gui.secrets.*` finiscono nel Secret creato
  dal chart.

```bash
kubectl -n smr create secret generic smr-gui \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=OAUTH_CLIENT_ID='<client-id>' \
  --from-literal=OAUTH_CLIENT_SECRET='<client-secret>' \
  --from-literal=OAUTH_SERVER_METADATA_URL='https://idp.example.com/.well-known/openid-configuration' \
  --from-literal=OAUTH_REDIRECT_URI='https://admin.example.com/auth' \
  --from-literal=OAUTH_ADMIN_GROUPS='admins'
helm upgrade --install smr charts/social-media-reuploader -n smr \
  --set secret.name=smr-bot --set gui.existingSecret=smr-gui
```

Impostazioni della GUI che non sono segreti (es. `SESSION_COOKIE_SECURE`) vanno
in `gui.env`. Il chart rifiuta segreti in chiaro in `env` e `gui.env`.

### Impostazioni

La mappa `env` di `values.yaml` elenca tutte le impostazioni con il loro
default; viene passata al bot e alla GUI:

```yaml
env:
  MODE: polling
  MAX_PENDING_PER_CHAT: 3
  RATE_LIMITS: "1/10,3/60,5/3600,20/30d"
  ALLOWED_PROVIDERS: [youtube, tiktok, instagram]
  UNSUPPORTED_LINK_REPLY: private
```

Numeri, booleani e liste diventano stringhe (`TELEGRAM_MAX_FILE_SIZE: 52428800`
diventa `"52428800"`); `null` lascia il default dell'applicazione. Per variabili
con `valueFrom` usa `extraEnv`, per interi Secret o ConfigMap `extraEnvFrom`.

### Esporre la GUI

Ingress (o Gateway API) instradano verso la GUI:

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
  hosts:
    - host: admin.example.com
      paths:
        - path: /
          service: gui
  tls:
    - secretName: admin-example-tls
      hosts: [admin.example.com]
gui:
  env:
    SESSION_COOKIE_SECURE: true
```

Con Gateway API:

```yaml
gatewayApi:
  enabled: true
  gatewayName: shared-gateway
  gatewayNamespace: gateway-system
  sectionName: https
  hosts: [admin.example.com]
  paths:
    - path: /
      service: gui
```

`gui.forwardedAllowIps` (default `"*"`) indica a uvicorn da quali indirizzi
fidarsi degli header `X-Forwarded-*`: va bene finché il Service della GUI è
raggiungibile solo dentro il cluster; altrimenti limitalo agli indirizzi
dell'ingress controller.

### Modalità webhook

```yaml
env:
  MODE: webhook
  WEBHOOK_URL: https://bot.example.com/webhook
  WEBHOOK_PATH: /webhook
ingress:
  enabled: true
  hosts:
    - host: bot.example.com
      paths:
        - path: /webhook
          service: bot
```

Instrada verso il bot solo `WEBHOOK_PATH`, mai `/` (esporrebbe `/metrics`). Il
chart rifiuta `MODE=webhook` senza `WEBHOOK_URL`. Telegram autentica le
richieste con `WEBHOOK_SECRET` (dal Secret del bot, oppure derivato dal token).

### Cookie per i siti con login

```bash
kubectl -n smr create secret generic smr-cookies --from-file=cookies.txt
helm upgrade --install smr charts/social-media-reuploader -n smr \
  --reuse-values --set ytdlpCookies.existingSecret=smr-cookies
```

Il file viene montato in sola lettura in
`/etc/social-media-reuploader/cookies/cookies.txt` e `YTDLP_COOKIES_PATH` punta
lì; yt-dlp lavora su una copia per ogni richiesta. Per montaggi diversi ci sono
`extraVolumes` / `extraVolumeMounts`.

### Persistenza e scheduling

- Il PVC è `ReadWriteOnce`: lo usano due pod, quindi devono stare sullo stesso
  nodo. La GUI ha una `podAffinity` obbligatoria verso il bot; il bot preferisce
  il nodo della GUI, così dopo un riavvio il volume resta collegabile.
- Se il bot ha `replicaCount: 0`, la GUI resta `Pending` (non c'è un bot
  accanto a cui partire).
- `db.pvc.existingClaim` usa un PVC già esistente; `db.pvc.storageClass` sceglie
  la StorageClass (`"-"` per nessuna). Con local-path (k3s) il volume vincola
  già entrambi i pod al nodo.
- Senza GUI (`gui.enabled: false`) si può disattivare il PVC
  (`db.pvc.enabled: false`): il database finisce in un emptyDir e si perde con
  il pod. Con la GUI abilitata il PVC è obbligatorio.

### Risorse

`resourceProfile` sceglie richieste e limiti del bot:

| Profilo | CPU req/limit | Memoria req/limit | Ephemeral storage req/limit | `/tmp` (sizeLimit) | Grace period | Uso |
|---|---|---|---|---|---|---|
| `small` | 100m / 300m | 256Mi / 512Mi | 512Mi / 3Gi | 2Gi | 60 s | clip brevi |
| `medium` (default) | 200m / 1000m | 512Mi / 1Gi | 1Gi / 5Gi | 4Gi | 180 s | video fino a ~100 MB |
| `large` | 500m / 2000m | 1Gi / 2Gi | 2Gi / 10Gi | 8Gi | 300 s | video fino a 500 MB, compressioni pesanti |

`resources`, `terminationGracePeriodSeconds` e `tmpVolume.sizeLimit` scavalcano
il profilo. La GUI usa `guiResources`.

### Metriche

`metrics.serviceMonitor.enabled: true` crea un `ServiceMonitor` (Prometheus
Operator) che legge `/metrics` dal Service del bot.

### Aggiornare da versioni del chart precedenti a 0.2.0

!!! warning "Database su PVC (`db.pvc.enabled=true` con il chart 0.1.x)"
    Il chart 0.1.x creava il PVC `social-media-reuploader-data` (oppure
    `<fullnameOverride>-data`; controlla con `kubectl get pvc`) senza
    l'annotazione `helm.sh/resource-policy: keep`. `helm upgrade` cancella le
    risorse che spariscono dal manifest: se il PVC cambia nome (i nuovi nomi
    delle risorse, qui sotto) o viene sostituito da `db.pvc.existingClaim`, il
    vecchio PVC viene cancellato e con lui, con la reclaim policy `Delete`, il
    database. Prima dell'upgrade proteggilo:

    ```bash
    kubectl -n <namespace> annotate pvc social-media-reuploader-data \
      helm.sh/resource-policy=keep
    ```

    Poi aggiorna con `fullnameOverride: social-media-reuploader` (o il
    `fullnameOverride` che usavi) e con gli stessi `db.pvc.size` e
    `db.pvc.storageClass` di prima: il PVC resta lo stesso e il nuovo chart lo
    tiene con `keep`. In alternativa, con i nuovi nomi, usa
    `db.pvc.existingClaim: social-media-reuploader-data`: il PVC protetto resta
    fuori dalla release (da cancellare a mano quando non serve più). Non usare
    `db.pvc.existingClaim` senza aver aggiunto l'annotazione.

- I nomi delle risorse sono ora `<release>-social-media-reuploader`, oppure
  solo `<release>` se il nome della release contiene già
  `social-media-reuploader`. Per mantenere i nomi precedenti imposta
  `fullnameOverride: social-media-reuploader`.
- Il PVC del database è attivo di default. Se usavi già `db.pvc.enabled=true`,
  segui l'avviso qui sopra prima dell'upgrade.
- `env.MODE` è `polling` di default; `TELEGRAM_MAX_FILE_SIZE` e gli altri
  numeri sono resi correttamente (prima `5.24288e+07` bloccava l'avvio).
- Le credenziali della GUI stanno sempre in un Secret: `gui.useSecret` non
  serve più, `gui.secretName` vale come `gui.existingSecret`.
- Ingress e HTTPRoute puntano alla GUI; `gatewayApi.servicePort` è ignorato
  (si usa `gatewayApi.paths[].service`).
- `WORKER_REHYDRATE_ON_UNLIMIT` non esiste più;
  `WORKER_PERIODIC_REHYDRATE_SECONDS` è il vecchio nome di
  `WORKER_POLL_SECONDS`.

## Flux

`examples/flux/` contiene un `GitRepository` e una `HelmRelease` di esempio:
il chart viene preso dall'ultima release di questo repository (tag `vX.Y.Z`,
mai `main`), il token dal Secret `smr-bot`, le credenziali della GUI dal Secret
`smr-gui` (da creare con SealedSecrets, External Secrets o SOPS: mai segreti
nel repository).

```bash
kubectl apply -f examples/flux/gitrepository.yaml
kubectl apply -f examples/flux/helmrelease.yaml
flux get helmreleases -n smr
```

Altre note in [DEPLOYMENT.md](https://github.com/lucaam/social-media-reuploader/blob/main/DEPLOYMENT.md).
