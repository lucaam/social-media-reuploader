# Local testing & development

This page lists practical steps to run the bot and the admin GUI locally for development.

Prerequisites

- Python 3.11
- Docker & docker-compose (optional, for container testing)
- `yt-dlp` (used by downloader) — it will be executed by the app, ensure it's available in PATH when running locally.

1) Quickstart using Python (run processes separately)

```bash
# create venv and install
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the bot in long-polling mode
export BOT_TOKEN="<your-bot-token>"
python -m src.bot

# In another terminal, run the admin GUI
export ADMIN_TOKEN="myadmintoken"
export SECRET_KEY="$(openssl rand -hex 32)"
uvicorn src.gui:app --host 0.0.0.0 --port 8081
```

Visit `http://localhost:8081/requests?token=myadmintoken` to see recent requests.

2) Run both with `docker-compose` (recommended for parity with containers)

```bash
export BOT_TOKEN="<your-bot-token>"
export ADMIN_TOKEN="myadmintoken"
docker-compose up --build
```

This uses the same image for bot and GUI but runs different commands: the bot container runs the `src.bot` entrypoint, while the GUI container runs `uvicorn src.gui:app` (see `docker-compose.yml`). Using the same image is convenient for development; in production you may build separate images or use different tags.

3) Run both services as containers from the built image

```bash
docker build -t local/social-media-reuploader:dev .
# Bot
docker run -d --name td-bot -e BOT_TOKEN="$BOT_TOKEN" local/social-media-reuploader:dev
# GUI (override cmd)
docker run -d --name td-gui -e ADMIN_TOKEN="$ADMIN_TOKEN" local/social-media-reuploader:dev uvicorn src.gui:app --host 0.0.0.0 --port 8081
```

Notes
- Using a single image for both processes is normal — the image contains the code and dependencies; you simply start different commands in separate containers.
- For production, you can either run two different Deployments (bot + GUI) using the same image and different `command`/`args`, or build separate images with specialised entrypoints.
