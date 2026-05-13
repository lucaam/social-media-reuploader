# Usage

Quickstart (development):

1. Create a virtualenv and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Export settings and run:

```bash
export BOT_TOKEN="<your-bot-token>"
export MODE=webhook
export WEBHOOK_URL="https://example.com/webhook/$BOT_TOKEN"
python -m src.main
```

3. For Docker, build the image and run (see Deploy page for full details).

Admin GUI

The project includes a small admin GUI (FastAPI) to inspect queued requests. It is protected by an `ADMIN_TOKEN` environment variable.

Run the GUI locally:

```bash
export ADMIN_TOKEN="<your-admin-token>"
uvicorn src.gui:app --host 0.0.0.0 --port 8081
```

Then visit `http://localhost:8081/requests?token=<your-admin-token>` to list recent queued requests.

Accessing the GUI with `ADMIN_TOKEN` (examples)

You can provide the admin token either as a query parameter (handy for quick browser checks) or as an Authorization header (recommended for scripts):

- Browser: open `http://localhost:8081/requests?token=<your-admin-token>`
- Curl (Authorization header):

```bash
export ADMIN_TOKEN="your-token"
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8081/requests
```

If you want the HTML view in a browser but don't want to expose the token in the URL, use an OAuth login instead (see OAuth setup in `oauth.md`) or open the page behind a secure tunnel.
