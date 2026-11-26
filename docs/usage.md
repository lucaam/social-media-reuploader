# Utilizzo

## Preparare il bot

1. Crea il bot con [@BotFather](https://t.me/BotFather) (`/newbot`) e copia il
   token: è il valore di `BOT_TOKEN`.
2. Per usarlo nei gruppi disattiva la **privacy mode** (`/setprivacy` →
   *Disable*): con la privacy mode attiva un bot non riceve i normali messaggi
   dei gruppi. Dopo averla cambiata, rimuovi e riaggiungi il bot ai gruppi in
   cui era già presente. In alternativa rendi il bot amministratore del gruppo.
3. Avvia il bot (vedi [Deploy](deploy.md)): in polling non serve esporre nulla
   su Internet.

## Cosa fa con un link

Il bot guarda il testo e le didascalie di ogni messaggio:

- riconosce i link scritti nel testo, quelli "nascosti" dietro una parola
  (text link), quelli senza `https://` (`youtu.be/...`) e ignora la
  punteggiatura finale (`guarda https://youtu.be/ID.`);
- elimina i duplicati nello stesso messaggio (`youtu.be/ID` e
  `youtube.com/watch?v=ID` sono lo stesso video);
- ignora i link a Telegram;
- risolve i link brevi di condivisione (`fb.watch`, `v.redd.it`, link
  `/share/` di Facebook e Instagram, `pin.it`, `on.soundcloud.com`, `b23.tv`,
  ...);
- accetta il link solo se un estrattore dedicato di yt-dlp lo riconosce, quindi
  ogni sito supportato da yt-dlp funziona (con i limiti del sito stesso);
- rifiuta profili, canali e playlist: va mandato il link del singolo post
  (`ALLOW_COLLECTION_LINKS=true` li accetta, pubblicando i primi
  `MAX_ITEMS_PER_LINK` elementi);
- rifiuta i siti esclusi dall'amministratore (`ALLOWED_PROVIDERS`,
  `BLOCKED_PROVIDERS`) e quelli che yt-dlp segnala come non funzionanti.

Se un messaggio viene modificato vengono elaborati solo i link nuovi.

Per ogni link accettato il bot mette la reazione 👀 sul messaggio, scarica il
media, lo converte se serve (MP4 H.264/AAC entro `TELEGRAM_MAX_FILE_SIZE`) e lo
invia in risposta al messaggio: video, audio, oppure un album per i post con
più media. La reazione diventa 🏆 a lavoro finito o 🍌 in caso di errore. Se il
messaggio originale è stato cancellato nel frattempo, il media viene inviato lo
stesso.

## Chat private e gruppi

Nelle **chat private** il bot spiega sempre cosa succede (in italiano):

- link non supportato: risposta con l'elenco dei principali siti supportati
  (configurabile con `UNSUPPORTED_LINK_REPLY`);
- link rifiutato per rate limit, per troppe richieste in coda o perché
  duplicato: messaggio con il motivo e, per il rate limit, quando riprovare;
- errore: contenuto privato o che richiede il login, rimosso, bloccato nel
  paese, diretta, troppo lungo, troppo grande, nessun video nel post, ...

Nei **gruppi e nei canali** il bot scrive il meno possibile, per non
disturbare:

- i link non supportati vengono ignorati (salvo
  `UNSUPPORTED_LINK_REPLY=always`) e i link rifiutati (rate limit, coda,
  duplicati) non ricevono messaggi;
- gli errori sono indicati dalla reazione 🍌, tranne "troppo grande" (vedi
  [Limiti](#limiti)): il bot risponde con il motivo e il link originale, così
  il contenuto si può aprire lo stesso.

Se Telegram rifiuta le reazioni (reazioni disattivate o limitate nella chat,
messaggio originale già cancellato), in qualsiasi chat, gruppi compresi, il bot
risponde al link con "In lavorazione 👀": il messaggio viene cancellato quando
il media arriva e diventa la spiegazione dell'errore se la richiesta fallisce.
Dopo un rifiuto il bot non riprova le reazioni in quella chat per
`REACTION_DISABLE_SECONDS` (5 minuti).

Gli avvisi ripetuti alla stessa chat sono limitati
(`NOTIFY_RATE_THROTTLE_SECONDS`, `NOTIFY_DUPLICATE_THROTTLE_SECONDS`).

## Limiti

| Limite | Default | Variabile |
|---|---|---|
| Dimensione di un upload | 50 MB (fino a 2 GB con un Bot API server locale) | `TELEGRAM_MAX_FILE_SIZE`, `TELEGRAM_API_BASE` |
| Dimensione scaricata prima della compressione | 500 MB | `MAX_DOWNLOAD_BYTES` |
| Durata | 1 ora (le dirette non sono supportate) | `MAX_DURATION_SECONDS` |
| Media per post multiplo | 10 (un album) | `MAX_ITEMS_PER_LINK` |
| Rate limit per chat | 1 ogni 10 s, 3 al minuto, 5 all'ora, 20 ogni 30 giorni | `RATE_LIMITS` |
| Attesa massima di un link accodato per rate limit | 2 minuti | `RATE_LIMIT_MAX_DEFER_SECONDS` |
| Richieste in coda o in lavorazione per chat | 3 | `MAX_PENDING_PER_CHAT` |
| Finestra anti-duplicati (stessa chat, stesso link) | 90 minuti | `DUPLICATE_WINDOW_SECONDS` |

Come funziona il rate limit: quando un link arriva e le finestre della chat sono
piene, il bot calcola il prossimo momento libero. Se è entro
`RATE_LIMIT_MAX_DEFER_SECONDS` il link viene accettato e messo in coda per quel
momento (verrà elaborato da solo); altrimenti viene rifiutato con l'indicazione
di quando riprovare. I link rifiutati non consumano quota e non bloccano gli
altri link della chat. Gli amministratori possono azzerare i limiti di una chat
dalla GUI.

Alcuni contenuti funzionano solo con un account: per quelli serve un file di
cookie esportato dal browser (`YTDLP_COOKIES_PATH`, vedi [Deploy](deploy.md)).
Con yt-dlp 2026.11.23 non sono supportati, ad esempio, Threads e i post di sole
foto di TikTok.

## GUI di amministrazione

La GUI mostra le richieste (con filtro e ricerca), la coda, lo stato del worker,
le chat limitate e gli utenti, e permette di:

- annullare le richieste in coda;
- rimettere in coda richieste fallite, annullate o rifiutate;
- azzerare i limiti di una chat o di tutte;
- gestire gli utenti amministratori;
- ripulire lo storico (le richieste attive non vengono toccate).

Ogni azione è una scrittura nel database: il bot la vede al controllo successivo
della coda (`WORKER_POLL_SECONDS`).

Accesso:

- **token**: imposta `ADMIN_TOKEN` e accedi con il token dalla pagina di login;
  negli script usa l'header `Authorization: Bearer <token>` per le API della
  GUI. Non mettere mai il token negli URL (finirebbe nei log e nella
  cronologia del browser).
- **OAuth2 / OpenID Connect**: vedi [OAuth](oauth.md).

La GUI richiede sempre una `SECRET_KEY` robusta (firma i cookie di sessione).
Servita in HTTPS, imposta `SESSION_COOKIE_SECURE=true`.

## Health check e metriche

- `GET /health` sul bot (porta `PORT`, default 8080) e sulla GUI.
- `GET /metrics` sul bot: metriche Prometheus (prefisso
  `telegram_downloader_`: link elaborati, download falliti, file inviati, file
  troppo grandi, ...). Va raccolto all'interno della rete, non esposto su
  Internet (il chart offre un `ServiceMonitor`).
