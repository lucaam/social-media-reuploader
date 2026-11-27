# Note di deploy (Kubernetes / Flux)

La guida completa è in [docs/deploy.md](docs/deploy.md); qui il riassunto delle
regole e la checklist.

## Regole

- **Una sola replica del bot** per token (`replicaCount: 1`, il chart rifiuta
  valori maggiori). Il bot si aggiorna con la strategia `Recreate`: il vecchio
  pod si ferma prima che parta il nuovo, quindi non ci sono mai due processi
  che leggono gli update o scrivono il database.
- **Database SQLite condiviso** tra bot e GUI su un PVC `ReadWriteOnce`
  (`db.pvc`, attivo di default e conservato da `helm uninstall`). La GUI viene
  schedulata sullo stesso nodo del bot (`podAffinity` obbligatoria su
  `kubernetes.io/hostname`). Usa storage a blocchi o local-path, non NFS.
- **Segreti solo nei Secret**: token del bot (`secret.name`), credenziali della
  GUI (`gui.existingSecret` o il Secret generato dal chart). Gestiscili con
  SealedSecrets, External Secrets o SOPS; non committare mai segreti.
- **GUI esposta solo in HTTPS** (Ingress o Gateway API verso il Service della
  GUI, `SESSION_COOKIE_SECURE=true`, con OAuth anche `OAUTH_REDIRECT_URI`).
- **Webhook**: instrada verso il bot solo `WEBHOOK_PATH`, mai `/metrics`.

## Profili di risorse

| Profilo | CPU req/limit | Memoria req/limit | Ephemeral storage req/limit | `/tmp` | Grace period | Uso |
|---|---|---|---|---|---|---|
| `small` | 100m / 300m | 256Mi / 512Mi | 512Mi / 3Gi | 2Gi | 60 s | clip brevi |
| `medium` (default) | 200m / 1000m | 512Mi / 1Gi | 1Gi / 5Gi | 4Gi | 180 s | video fino a ~100 MB |
| `large` | 500m / 2000m | 1Gi / 2Gi | 2Gi / 10Gi | 8Gi | 300 s | video fino a 500 MB |

```yaml
resourceProfile: large
# oppure valori espliciti:
resources:
  requests: {cpu: 250m, memory: 512Mi}
  limits: {cpu: 1500m, memory: 1536Mi}
terminationGracePeriodSeconds: 240   # WORKER_SHUTDOWN_TIMEOUT diventa 225
tmpVolume:
  sizeLimit: 6Gi
```

Il grace period lascia finire download e conversioni in corso: il chart
imposta `WORKER_SHUTDOWN_TIMEOUT` al grace period meno 15 secondi.

## Checklist

1. Secret del token: `kubectl create secret generic smr-bot --from-literal=BOT_TOKEN=...`
   (o la sua versione sigillata/cifrata).
2. Facoltativo: Secret della GUI con `SECRET_KEY` e `ADMIN_TOKEN` o le
   impostazioni OAuth (altrimenti il chart genera `SECRET_KEY` e `ADMIN_TOKEN`).
3. Facoltativo: Secret con `cookies.txt` (`ytdlpCookies.existingSecret`).
4. Chart OCI di una release, con la versione fissata
   (`helm upgrade --install smr oci://ghcr.io/lucaam/charts/social-media-reuploader --version X.Y.Z`):
   installa l'immagine della stessa versione, quindi `image.tag` resta vuoto.
   Valori: `secret.name`, `gui.existingSecret`, `env` (limiti, provider),
   `ingress`/`gatewayApi`.
5. Privacy mode del bot disattivata in @BotFather se lo usi nei gruppi.
6. Verifica: `kubectl logs deploy/<release>-social-media-reuploader`,
   `kubectl port-forward svc/<release>-social-media-reuploader 8080:80` e
   `curl http://127.0.0.1:8080/health`.

## Flux

`examples/flux/` contiene `OCIRepository` e `HelmRelease` di esempio (API
`source.toolkit.fluxcd.io/v1` e `helm.toolkit.fluxcd.io/v2`, Flux 2.8 o
successivo) che installano il chart OCI delle release, ne verificano la firma
cosign e prendono i segreti da Secret esistenti.
