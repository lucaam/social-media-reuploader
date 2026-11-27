# Social Media Reuploader

Bot Telegram che, quando in una chat viene incollato il link di un post social,
scarica il media con [yt-dlp](https://github.com/yt-dlp/yt-dlp) e lo ripubblica
nella chat come video, audio o album.

- **Tutti i siti supportati da yt-dlp**: YouTube (anche Shorts), TikTok,
  Instagram, Facebook, X (Twitter), Reddit, Vimeo, Twitch, Dailymotion,
  SoundCloud, Bilibili e oltre 800 altri siti. L'estrattore generico non viene
  mai usato.
- **Media pronti per Telegram**: formato H.264/AAC MP4 entro il limite di
  upload (50 MB con la Bot API cloud), conversione e compressione con ffmpeg
  quando servono, album per i post con più media.
- **Coda persistente** in SQLite: le richieste sopravvivono ai riavvii e la GUI
  le vede in tempo reale.
- **Limiti per chat**: rate limit a finestre scorrevoli, massimo di richieste in
  coda e finestra anti-duplicati, con messaggi chiari nelle chat private e
  nessun rumore nei gruppi.
- **GUI di amministrazione** (FastAPI): richieste, coda, stato del worker,
  limiti per chat, utenti; login con token o OAuth2/OpenID Connect.
- **Osservabilità**: `/health` e metriche Prometheus su `/metrics`.
- Immagine Docker multi-arch (amd64/arm64) con ffmpeg, deno e curl_cffi; chart
  Helm.

## Architettura

```text
             Telegram
                │  update (polling o webhook)
                ▼
┌──────────────────────────────┐        ┌──────────────────────────┐
│ bot  (python -m src.bot)     │        │ GUI (uvicorn src.gui:app)│
│ - estrae e valida i link     │        │ - richieste, coda, limiti│
│ - coda + worker yt-dlp/ffmpeg│        │ - azioni admin = scritture│
│ - /health, /metrics          │        │   nel DB                 │
└──────────────┬───────────────┘        └─────────────┬────────────┘
               │          SQLite (WAL), stesso file   │
               └──────────────► requests.db ◄─────────┘
```

Bot e GUI sono due processi separati che condividono solo il database: devono
usare lo stesso file `REQUESTS_DB` su un filesystem locale (stesso host o stesso
nodo Kubernetes).

## Da dove iniziare

- [Utilizzo](usage.md): come si usa il bot nelle chat private e nei gruppi,
  limiti e messaggi.
- [Configurazione](configuration.md): tutte le variabili d'ambiente.
- [Deploy](deploy.md): Docker, Docker Compose, Kubernetes con Helm e Flux.
- [OAuth](oauth.md): login alla GUI con un provider OAuth2/OpenID Connect.
- [Sviluppo e test in locale](local-testing.md).
- [Immagini, chart e release](ghcr.md): artefatti pubblicati, flusso di release, verifica delle firme.
