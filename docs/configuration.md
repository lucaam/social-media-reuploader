# Configurazione

Tutte le impostazioni sono variabili d'ambiente, lette una sola volta
all'avvio da `src/config.py` (le credenziali della GUI dal processo della GUI).
Un valore non valido non blocca l'avvio: viene usato il default e il problema
compare nei log all'avvio.

- **Docker / Docker Compose**: variabili del container (`environment:` o file
  `.env`, vedi `examples/docker-compose.env.example`).
- **Helm**: mappa `env` di `values.yaml`, passata sia al bot sia alla GUI;
  numeri, booleani e liste vengono resi come stringhe (`52428800`, `true`,
  `youtube,tiktok`). `null` significa "default dell'applicazione". Il chart
  imposta da solo `PORT` e `REQUESTS_DB` e prende i segreti (`BOT_TOKEN`,
  `WEBHOOK_SECRET`, `SECRET_KEY`, `ADMIN_TOKEN`, `OAUTH_CLIENT_SECRET`) dai
  Secret: vedi [Deploy](deploy.md).

<!-- readme-section: configurazione -->
