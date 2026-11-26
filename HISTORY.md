# HISTORY

Questo file traccia le tappe principali del progetto in modo che sia facile riprenderlo in seguito.

## 2026-11-26 — Refactor: tutti i provider di yt-dlp, coda nel database, deploy rivisto

Revisione completa basata su una code review (bug critici di deploy, coda,
rate limit, GUI e CI). In sintesi:

- **Provider**: accettati tutti i siti supportati da yt-dlp tramite i suoi
  estrattori dedicati (mai l'estrattore generico), con `ALLOWED_PROVIDERS` /
  `BLOCKED_PROVIDERS`. Estrazione dei link dalle entità Telegram (offset
  UTF-16), link senza schema, punteggiatura finale, link brevi di condivisione
  risolti, normalizzazione per la deduplica; profili e playlist rifiutati salvo
  `ALLOW_COLLECTION_LINKS`.
- **Download e media**: una sola estrazione yt-dlp per link e download dal JSON
  salvato; selezione del formato H.264/AAC entro il limite di upload; errori
  tipizzati (privato, rimosso, geo-bloccato, diretta, troppo lungo, troppo
  grande, nessun media, timeout); kill dell'intero process group; cookie copiati
  per ogni richiesta; conversione/compressione con ffmpeg rispettando rotazione e
  aspect ratio; album per i post con più media.
- **Telegram**: client Bot API unico con retry su 429, timeout distinti,
  risposte che funzionano anche se il messaggio originale è stato cancellato,
  token oscurato nei log.
- **Database**: SQLite in modalità WAL con busy timeout, migrazioni versionate,
  timestamp UTC, transizioni di stato atomiche e un log degli eventi condiviso
  tra processi; nessun fallback silenzioso a un database in memoria.
- **Coda e limiti**: la coda vive nel database; rate limit a finestre
  scorrevoli (`RATE_LIMITS`) con accodamento differito
  (`RATE_LIMIT_MAX_DEFER_SECONDS`), `MAX_PENDING_PER_CHAT` che rifiuta solo il
  link in più, finestra anti-duplicati applicata anche ai link già pubblicati,
  avvisi testuali solo nelle chat private.
- **Bot e worker**: stesso dispatcher per polling e webhook (secret token, mai
  il token nell'URL), `MODE=polling` di default, `/health` e `/metrics` in
  entrambe le modalità, recupero delle richieste interrotte all'avvio,
  heartbeat per la GUI, spegnimento graceful.
- **GUI**: processo separato che legge tutto dal database; le azioni admin sono
  scritture nel database; `SECRET_KEY` robusta obbligatoria, login con token o
  OAuth2/OpenID Connect, aggiornamenti live dal log degli eventi.
- **Configurazione**: `src/config.py` unica fonte delle impostazioni, con parsing
  tollerante (un `5.24288e+07` non blocca più l'avvio) e avvisi all'avvio.
- **Deploy**:
  - immagine con `yt-dlp[default,curl-cffi,deno]==2026.11.23`, deno, ffmpeg,
    tini, utente 1000, compatibile con filesystem root in sola lettura;
  - Docker Compose con database condiviso tra bot e GUI, `BOT_TOKEN` e
    `SECRET_KEY` obbligatori, porte solo su 127.0.0.1;
  - chart Helm 0.2.0: numeri resi come stringhe, PVC di default con la GUI
    sullo stesso nodo del bot (podAffinity), strategia `Recreate` e una sola
    replica, segreti solo nei Secret (generati se mancano), Ingress/HTTPRoute
    verso la GUI, mappa `env` documentata con tutte le impostazioni, cookie da
    Secret, `extraEnv`/`extraVolumes`, emptyDir con limite per `/tmp`,
    ServiceMonitor, test helm-unittest.
- **CI e release**: test anche sui push a `main` e con ffmpeg; job di lint
  funzionante con versioni allineate tra `requirements-dev.txt` e pre-commit;
  helm-unittest e kubeconform; immagine pubblicata automaticamente a ogni
  release (workflow riutilizzabile); un solo flusso di release (rimosso
  release-please, che era bloccato); controllo che ogni impostazione sia
  documentata (`scripts/check_docs.py`).
- **Documentazione**: README, guide in `docs/` e note di deploy riscritte.

## 2026-05-12 — Inizializzazione
- Creato scaffold iniziale del progetto.
- Aggiunti: `src/main.py`, `src/worker.py`, `src/downloader.py`, `src/link_utils.py`, `src/telegram_api.py`.
- Dockerfile, Helm chart (`charts/social-media-reuploader`) e CI (GitHub Actions `ci.yml`) creati.
- Test base per rilevamento link aggiunti (`tests/test_link_detection.py`).
- Implementata integrazione base con `yt-dlp` e gestione temp file.
- Aggiunte metriche Prometheus e endpoint `/metrics`.

## Prossimi passi consigliati
- Fallback per i file oltre il limite di Telegram (Bot API server locale già
  supportato con `TELEGRAM_API_BASE`; in alternativa link a uno storage S3/MinIO).
- Riutilizzo dei media già inviati (`file_id`) per i link ripetuti in chat
  diverse.
- Abilitare il manager pre-commit di Renovate (e l'automerge delle patch di
  yt-dlp) per tenere allineati hook e dipendenze.
