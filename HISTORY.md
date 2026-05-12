# HISTORY

Questo file traccia le tappe principali del progetto in modo che sia facile riprenderlo in seguito.

## 2026-05-12 — Inizializzazione
- Creato scaffold iniziale del progetto.
- Aggiunti: `src/main.py`, `src/worker.py`, `src/downloader.py`, `src/link_utils.py`, `src/telegram_api.py`.
- Dockerfile, Helm chart (`charts/social-media-reuploader`) e CI (GitHub Actions `ci.yml`) creati.
- Test base per rilevamento link aggiunti (`tests/test_link_detection.py`).
- Implementata integrazione base con `yt-dlp` e gestione temp file.
- Aggiunte metriche Prometheus e endpoint `/metrics`.

## Prossimi passi consigliati
- Aggiungere fallback S3/MinIO per file grandi.
- Aggiungere integrazione `aiogram` o alternativa per gestire limiti avanzati.
- Raffinare gestione errori e policy anti-abuse.

