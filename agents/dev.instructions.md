# Dev agent instructions

Obiettivo: riprendere lo sviluppo delle funzionalità del bot social-media-reuploader.

1. Contesto rapido: il bot ascolta webhook POST su `/webhook/{token}` e usa `yt-dlp` per scaricare i contenuti. Il worker invia file con l'API HTTP di Telegram.
2. Verifiche iniziali:
   - Esegui `pytest`.
   - Lancia `python -m src.main` in locale (impostare `BOT_TOKEN` e `MODE=webhook` o `MODE=local`).
3. Task frequenti (prompt per l'agente):
   - "Aggiungi fallback S3/MinIO se il file supera il limite di Telegram".
   - "Implementa riuso dei file in cache per link già scaricati".
   - "Aggiungi aiogram per usare Bot API client ufficiale al posto di raw HTTP".
   5. Pushing & deployment

   - When you're ready to publish the cleaned repository, the expected remote is:

      `git@github.com:lucaam/social-media-reuploader.git`

   - The repo history has been rewritten for initial import; to publish, add the remote and push:

   ```bash
   git remote add origin git@github.com:lucaam/social-media-reuploader.git
   # verify then force-push the cleaned main branch
   git push --force --set-upstream origin main
   git push origin --tags
   ```

   Replace the remote URL with the target if different. The agent should NOT push
   without user confirmation.
4. Convenzioni commit: usa Conventional Commits (es. `feat:`, `fix:`, `chore:`).

