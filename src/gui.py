import asyncio
import datetime
import json
import logging
import os
import time

import aiohttp
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, ws_broadcast

app = FastAPI(title="Social media reuploader - Admin GUI")

# Session secret for cookie storage
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# OAuth client registration (generic)
oauth = OAuth()
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")
OAUTH_AUTHORIZE_URL = os.environ.get("OAUTH_AUTHORIZE_URL")
OAUTH_TOKEN_URL = os.environ.get("OAUTH_TOKEN_URL")
OAUTH_USERINFO_URL = os.environ.get("OAUTH_USERINFO_URL")
OAUTH_SCOPE = os.environ.get("OAUTH_SCOPE", "openid profile email")
OAUTH_SERVER_METADATA_URL = os.environ.get("OAUTH_SERVER_METADATA_URL")
OAUTH_JWKS_URI = os.environ.get("OAUTH_JWKS_URI")

# Optional mapping of OAuth groups to admin role. Comma-separated list.
# Example: OAUTH_ADMIN_GROUPS="authentik Admins,admins"
OAUTH_ADMIN_GROUPS = os.environ.get("OAUTH_ADMIN_GROUPS")
if OAUTH_ADMIN_GROUPS:
    OAUTH_ADMIN_GROUPS_SET = set(
        [g.strip() for g in OAUTH_ADMIN_GROUPS.split(",") if g.strip()]
    )
    OAUTH_ADMIN_GROUPS_LOWER = set(g.lower() for g in OAUTH_ADMIN_GROUPS_SET)
else:
    OAUTH_ADMIN_GROUPS_SET = set()
    OAUTH_ADMIN_GROUPS_LOWER = set()
if (
    OAUTH_CLIENT_ID
    and OAUTH_CLIENT_SECRET
    and ((OAUTH_AUTHORIZE_URL and OAUTH_TOKEN_URL) or OAUTH_SERVER_METADATA_URL)
):
    # Register provider. Support either explicit authorize/token endpoints
    # OR a discovery `server_metadata_url` for OIDC providers.
    register_kwargs = dict(
        name="provider",
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
        client_kwargs={"scope": OAUTH_SCOPE},
    )
    if OAUTH_SERVER_METADATA_URL:
        register_kwargs["server_metadata_url"] = OAUTH_SERVER_METADATA_URL
    else:
        register_kwargs["authorize_url"] = OAUTH_AUTHORIZE_URL
        register_kwargs["access_token_url"] = OAUTH_TOKEN_URL
    oauth.register(**register_kwargs)

# static SPA directory
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _check_admin(request: Request) -> bool:
    # Admin token allowed only when OAuth is not configured
    admin_token = os.environ.get("ADMIN_TOKEN")
    if admin_token and not _oauth_enabled():
        auth = request.headers.get("Authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            if token == admin_token:
                return True
        q = request.query_params.get("token")
        if q and q == admin_token:
            return True

    # OAuth session fallback: check DB for user role
    session = request.session
    # Session-level admin override: allow granting admin for the current
    # session after a successful OAuth login (see /api/session/grant_admin).
    try:
        if session and session.get("is_admin"):
            return True
    except Exception:
        pass
    # Option A: treat any authenticated user as admin (session contains user info)
    try:
        if session and session.get("user"):
            return True
    except Exception:
        pass
    # Optional: map OAuth groups to admin role when configured
    if OAUTH_ADMIN_GROUPS_SET and session and session.get("user"):
        try:
            user = session.get("user")
            if isinstance(user, dict):
                groups = (
                    user.get("groups") or user.get("memberOf") or user.get("member_of")
                )
                if groups:
                    if isinstance(groups, str):
                        groups_list = [
                            g.strip() for g in groups.split(",") if g.strip()
                        ]
                    elif isinstance(groups, (list, tuple)):
                        groups_list = list(groups)
                    else:
                        groups_list = [str(groups)]
                    for g in groups_list:
                        if (
                            g in OAUTH_ADMIN_GROUPS_SET
                            or g.lower() in OAUTH_ADMIN_GROUPS_LOWER
                        ):
                            return True
        except Exception:
            pass
        user = session.get("user")
        # try to extract email
        email = None
        if isinstance(user, dict):
            email = (
                user.get("email") or user.get("preferred_username") or user.get("sub")
            )
        if email:
            try:
                u = db.get_user_by_email(email)
                if u and u[3] == "admin":
                    return True
            except Exception:
                # If DB is not available (tests/CI), do not raise here.
                pass

    return False


def _get_oauth_provider():
    """Return a provider client in a way that works across authlib versions.

    Some authlib versions expose registered clients as attribute access
    (e.g. ``oauth.provider``). Newer versions encourage using
    ``oauth.create_client(name)``. Tests may also monkeypatch
    ``oauth.provider`` directly. Try several fallbacks and return the
    first callable-like provider.
    """
    # prefer attribute access if present
    try:
        if hasattr(oauth, "provider") and getattr(oauth, "provider") is not None:
            return getattr(oauth, "provider")
    except Exception:
        pass
    # try authlib factory method
    try:
        if hasattr(oauth, "create_client"):
            client = oauth.create_client("provider")
            if client is not None:
                return client
    except Exception:
        pass
    # finally, fall back to raw _clients dict (used in some tests)
    try:
        raw = getattr(oauth, "_clients", None)
        if raw and "provider" in raw:
            # Some authlib versions store client instances directly in the
            # internal _clients dict (e.g. authlib 1.7.x). Return the stored
            # client rather than attempting attribute access which may not
            # be present.
            try:
                return raw["provider"]
            except Exception:
                return getattr(oauth, "provider", None)
    except Exception:
        pass
    return None


def _oauth_enabled() -> bool:
    """Return True when an OAuth provider is configured and usable."""
    # Determine OAuth availability from configuration environment variables
    try:
        client_ok = bool(OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET)
        # Either explicit authorize/token URLs or discovery URL must be present
        endpoints_ok = bool(
            (OAUTH_AUTHORIZE_URL and OAUTH_TOKEN_URL) or OAUTH_SERVER_METADATA_URL
        )
        return bool(client_ok and endpoints_ok)
    except Exception:
        return False


def _check_admin_ws(websocket: WebSocket) -> bool:
    # same as _check_admin but for WebSocket scope
    admin_token = os.environ.get("ADMIN_TOKEN")
    if admin_token and not _oauth_enabled():
        q = websocket.query_params.get("token")
        auth = websocket.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            if token == admin_token:
                return True
        if q and q == admin_token:
            return True
    # session via scope
    session = websocket.session if hasattr(websocket, "session") else None
    # Session-level admin override (for WebSocket scope as well)
    try:
        if session and session.get("is_admin"):
            return True
    except Exception:
        pass
    # Option A: treat any authenticated websocket session as admin
    try:
        if session and session.get("user"):
            return True
    except Exception:
        pass
    # Optional: map OAuth groups to admin role when configured (WebSocket)
    if OAUTH_ADMIN_GROUPS_SET and session and session.get("user"):
        try:
            user = session.get("user")
            if isinstance(user, dict):
                groups = (
                    user.get("groups") or user.get("memberOf") or user.get("member_of")
                )
                if groups:
                    if isinstance(groups, str):
                        groups_list = [
                            g.strip() for g in groups.split(",") if g.strip()
                        ]
                    elif isinstance(groups, (list, tuple)):
                        groups_list = list(groups)
                    else:
                        groups_list = [str(groups)]
                    for g in groups_list:
                        if (
                            g in OAUTH_ADMIN_GROUPS_SET
                            or g.lower() in OAUTH_ADMIN_GROUPS_LOWER
                        ):
                            return True
        except Exception:
            pass

    if session and session.get("user"):
        user = session.get("user")
        email = None
        if isinstance(user, dict):
            email = (
                user.get("email") or user.get("preferred_username") or user.get("sub")
            )
        if email:
            try:
                u = db.get_user_by_email(email)
                if u and u[3] == "admin":
                    return True
            except Exception:
                # If DB is not available (tests/CI), do not raise here.
                pass
    return False


def _session_has_persistent_entitlement(session) -> bool:
    """Return True if the given session's user is persistently entitled to admin

    This checks OAuth group membership (when configured) and the DB role for
    the user's email. It deliberately does NOT consider the transient
    `session['is_admin']` flag.
    """
    if not session or not session.get("user"):
        return False
    try:
        user = session.get("user")
        # check groups if configured
        if OAUTH_ADMIN_GROUPS_SET and isinstance(user, dict):
            groups = user.get("groups") or user.get("memberOf") or user.get("member_of")
            if groups:
                if isinstance(groups, str):
                    groups_list = [g.strip() for g in groups.split(",") if g.strip()]
                elif isinstance(groups, (list, tuple)):
                    groups_list = list(groups)
                else:
                    groups_list = [str(groups)]
                for g in groups_list:
                    if (
                        g in OAUTH_ADMIN_GROUPS_SET
                        or g.lower() in OAUTH_ADMIN_GROUPS_LOWER
                    ):
                        return True
        # check DB role by email
        email = None
        if isinstance(user, dict):
            email = (
                user.get("email") or user.get("preferred_username") or user.get("sub")
            )
        if email:
            try:
                u = db.get_user_by_email(email)
                if u and u[3] == "admin":
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


@app.on_event("startup")
async def startup_event():
    # ensure DB exists
    try:
        db.init_db()
    except Exception:
        pass
    # set ws_broadcast loop
    try:
        ws_broadcast.loop = asyncio.get_event_loop()
    except Exception:
        pass

    # Start a background poller that watches the DB `updates` table for
    # new rows inserted by other processes and re-broadcasts them to
    # connected websocket clients. This enables real-time UI updates when
    # the worker runs in a separate process from the GUI.
    async def _db_updates_poller(poll_interval: float = 1.0):
        last_id = 0
        try:
            conn = db._connect()
            cur = conn.cursor()
            cur.execute("SELECT MAX(id) FROM updates")
            r = cur.fetchone()
            try:
                last_id = int(r[0]) if r and r[0] is not None else 0
            except Exception:
                last_id = 0
            try:
                conn.close()
            except Exception:
                pass
        except Exception:
            last_id = 0

        while True:
            try:
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

            try:
                conn = db._connect()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, raw, created_at FROM updates WHERE id > ? ORDER BY id ASC",
                    (last_id,),
                )
                rows = cur.fetchall()
                try:
                    conn.close()
                except Exception:
                    pass
            except Exception:
                rows = []

            for r in rows:
                try:
                    uid, raw, created = r
                except Exception:
                    continue
                try:
                    last_id = int(uid)
                except Exception:
                    pass
                # try to decode JSON payloads to re-emit structured events
                parsed = None
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
                try:
                    if isinstance(parsed, dict) and parsed.get("type"):
                        try:
                            await ws_broadcast.broadcast(parsed)
                        except Exception:
                            pass
                    else:
                        try:
                            await ws_broadcast.broadcast(
                                {
                                    "type": "update_created",
                                    "id": uid,
                                    "raw": raw,
                                    "created_at": created,
                                }
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

    try:
        asyncio.create_task(
            _db_updates_poller(float(getattr(config, "GUI_DB_POLL_SECONDS", 1.0)))
        )
    except Exception:
        pass
    # suppress uvicorn access logs for frequent /health probes unless debug enabled
    try:

        class _HealthProbeFilter(logging.Filter):
            def filter(self, record):
                try:
                    msg = record.getMessage()
                    if "/health" in msg:
                        if "kube-probe" in msg.lower() or "get /health" in msg.lower():
                            return False
                except Exception:
                    pass
                return True

        if not getattr(config, "HEALTH_DEBUG", False):
            logging.getLogger("uvicorn.access").addFilter(_HealthProbeFilter())
    except Exception:
        pass

    # Enforce that at least one authentication method is configured. If
    # neither OAuth nor ADMIN_TOKEN are available, fail fast to avoid
    # starting an unsecured admin GUI.
    try:
        if not _oauth_enabled() and not os.environ.get("ADMIN_TOKEN"):
            logging.error(
                "No authentication configured for GUI: set OAuth variables or ADMIN_TOKEN"
            )
            raise RuntimeError(
                "Admin GUI requires OAuth or ADMIN_TOKEN to be configured"
            )
    except Exception:
        # propagate to stop startup
        raise


@app.get("/")
async def index(request: Request):
    # serve SPA index (static)
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Content Security Policy: restrict external connections to avoid
            # third-party injected requests (e.g. extension CDNs). Allow our
            # own CDN used for scripts/styles (cdn.jsdelivr.net). Also permit
            # HTTPS connections for resources like source-maps from CDNs so
            # devtools can fetch them without CSP violations.
            csp = "default-src 'self' https://cdn.jsdelivr.net; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self' https://cdn.jsdelivr.net https:; object-src 'none'; base-uri 'self'"
            return HTMLResponse(
                content,
                media_type="text/html",
                headers={"Content-Security-Policy": csp},
            )
        except Exception:
            return FileResponse(index_path, media_type="text/html")
    # fallback minimal page
    html = "<html><body><h1>Admin GUI</h1><p>Static UI missing.</p></body></html>"
    return HTMLResponse(html)


@app.get("/config")
async def gui_config(request: Request):
    # Reuse the richer /api/config view so the SPA's "Mostra" button
    # returns the same runtime information (worker settings, env, etc.).
    try:
        return await api_config(request)
    except Exception:
        # fallback minimal view
        oauth_conf = _oauth_enabled()
        admin_token_present = bool(os.environ.get("ADMIN_TOKEN"))
        admin_token_effective = admin_token_present and not oauth_conf
        return JSONResponse(
            {"oauth_configured": oauth_conf, "admin_token_set": admin_token_effective}
        )


@app.get("/api/me")
async def api_me(request: Request):
    # returns the session user if logged in via OAuth
    session = request.session
    user = session.get("user") if session else None
    is_admin = False
    try:
        is_admin = bool(session.get("is_admin")) if session else False
    except Exception:
        is_admin = False
    return JSONResponse({"user": user, "is_admin": is_admin})


@app.get("/health")
async def health(request: Request):
    # Minimal probe response by default. Expose diagnostics only when enabled.
    try:
        if not getattr(config, "HEALTH_DEBUG", False):
            return JSONResponse({"ok": True})
    except Exception:
        return JSONResponse({"ok": True})

    # Diagnostic payload
    try:
        try:
            db.init_db()
            total_requests = db.count_requests()
            db_ok = True
        except Exception:
            total_requests = None
            db_ok = False
        ws_conns = getattr(ws_broadcast, "_connections", None)
        ws_count = len(ws_conns) if ws_conns is not None else 0
        payload = {
            "ok": True,
            "db_ok": db_ok,
            "total_requests": total_requests,
            "ws_clients": ws_count,
        }
        return JSONResponse(payload)
    except Exception:
        return JSONResponse({"ok": True})


@app.get("/login")
async def login(request: Request):
    # If OAuth is configured (by env vars), use the provider-based flow.
    if _oauth_enabled():
        provider = _get_oauth_provider()
        if not provider:
            raise HTTPException(status_code=400, detail="OAuth provider not available")
        redirect_uri = request.url_for("auth")
        return await provider.authorize_redirect(request, redirect_uri)

    # Otherwise, if ADMIN_TOKEN is configured, show a simple token form.
    admin_token = os.environ.get("ADMIN_TOKEN")
    if admin_token:
        html = """
        <!doctype html>
        <html lang="it">
            <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>Login Admin — Social media reuploader</title>
                <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
                <style>
                    body { background:#f8f9fa; }
                    .overlay { display:flex; align-items:center; justify-content:center; min-height:100vh; padding:2rem; }
                    .login-card { max-width:520px; width:100%; background:#fff; padding:1.5rem; border-radius:12px; box-shadow:0 8px 30px rgba(0,0,0,0.08); }
                    .brand { font-weight:700; color:#0d6efd; }
                    .small-muted { color:#6c757d; font-size:0.95rem; }
                </style>
            </head>
            <body>
                <div class="overlay">
                    <div class="login-card">
                        <div class="d-flex justify-content-between align-items-start mb-3">
                            <div>
                                <div class="brand">Social media reuploader</div>
                                <div class="small-muted">Local admin token login</div>
                            </div>
                            <div><small class="small-muted">Secured</small></div>
                        </div>
                        <p class="text-muted">Inserisci il token amministrativo locale per autenticarti e accedere alla console di amministrazione.</p>
                        <form method="post" action="/login" class="mb-2">
                            <div class="mb-3">
                                <label class="form-label">Admin token</label>
                                <input autofocus class="form-control form-control-lg" type="password" name="token" />
                            </div>
                            <div class="d-grid">
                                <button class="btn btn-primary btn-lg" type="submit">Accedi</button>
                            </div>
                        </form>
                        <div class="mt-3 small-muted">Se preferisci usare OAuth, configura le variabili OAUTH_* e riavvia il servizio.</div>
                    </div>
                </div>
            </body>
        </html>
        """
        return HTMLResponse(html)

    raise HTTPException(status_code=400, detail="No login method available")


@app.post("/login")
async def login_post(request: Request):
    """Process local admin token login when OAuth is not available.

    This endpoint accepts a form field `token` and, if it matches the
    configured `ADMIN_TOKEN`, grants `session['is_admin'] = True` for the
    ongoing session and redirects to the index.
    """
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token or _oauth_enabled():
        raise HTTPException(status_code=400, detail="Local admin login not available")
    # Parse form without relying on python-multipart (might not be installed).
    token = None
    try:
        ct = (request.headers.get("content-type") or "").lower()
        body = await request.body()
        if b"=" in body and ("application/x-www-form-urlencoded" in ct or ct == ""):
            # parse urlencoded body
            from urllib.parse import parse_qs

            parsed = parse_qs(body.decode("utf-8", errors="ignore"))
            token = parsed.get("token", [None])[0]
        else:
            # try json fallback
            try:
                j = await request.json()
                token = j.get("token")
            except Exception:
                token = None
    except Exception:
        token = None
    if token and token == admin_token:
        try:
            request.session["is_admin"] = True
        except Exception:
            pass
        return RedirectResponse(url="/", status_code=303)
        html = """
        <!doctype html>
        <html lang="it">
            <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>Login Admin — Social media reuploader</title>
                <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
                <style>
                    body { background:#f8f9fa; }
                    .overlay { display:flex; align-items:center; justify-content:center; min-height:100vh; padding:2rem; }
                    .login-card { max-width:520px; width:100%; background:#fff; padding:1.5rem; border-radius:12px; box-shadow:0 8px 30px rgba(0,0,0,0.08); }
                    .brand { font-weight:700; color:#0d6efd; }
                    .small-muted { color:#6c757d; font-size:0.95rem; }
                </style>
            </head>
            <body>
                <div class="overlay">
                    <div class="login-card">
                        <div class="d-flex justify-content-between align-items-start mb-3">
                            <div>
                                <div class="brand">Social media reuploader</div>
                                <div class="small-muted">Local admin token login</div>
                            </div>
                            <div><small class="small-muted">Secured</small></div>
                        </div>
                        <p class="text-danger">Token non valido</p>
                        <form method="post" action="/login" class="mb-2">
                            <div class="mb-3">
                                <label class="form-label">Admin token</label>
                                <input autofocus class="form-control form-control-lg" type="password" name="token" />
                            </div>
                            <div class="d-grid">
                                <button class="btn btn-primary btn-lg" type="submit">Accedi</button>
                            </div>
                        </form>
                        <div class="mt-3 small-muted">Se preferisci usare OAuth, configura le variabili OAUTH_* e riavvia il servizio.</div>
                    </div>
                </div>
            </body>
        </html>
        """
        return HTMLResponse(html, status_code=403)


@app.get("/auth")
async def auth(request: Request):
    provider = _get_oauth_provider()
    if not provider:
        raise HTTPException(status_code=400, detail="OAuth provider not configured")
    try:
        token = await provider.authorize_access_token(request)
    except OAuthError as err:
        raise HTTPException(status_code=400, detail=str(err))

    # Try to obtain userinfo. Prefer fetching from configured userinfo URL,
    # otherwise fall back to any userinfo returned in the token payload.
    access_token = token.get("access_token")
    userinfo = None
    if OAUTH_USERINFO_URL and access_token:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {access_token}"}
            async with session.get(OAUTH_USERINFO_URL, headers=headers) as resp:
                try:
                    userinfo = await resp.json()
                except Exception:
                    userinfo = None

    if userinfo is None:
        # Some providers return user info in the token response under
        # various keys; accept a few common fallbacks.
        userinfo = (
            token.get("userinfo") or token.get("user") or token.get("id_token_claims")
        )

    # Save user in session only when we have meaningful identity information.
    # Avoid storing a placeholder like {'sub': None} which makes the UI
    # mistakenly treat the session as authenticated.
    try:
        if (
            userinfo
            and isinstance(userinfo, dict)
            and (
                userinfo.get("email")
                or userinfo.get("preferred_username")
                or userinfo.get("sub")
                or userinfo.get("name")
            )
        ):
            request.session["user"] = userinfo
        else:
            # ensure we don't leave a stale/placeholder user in session
            request.session.pop("user", None)
    except Exception:
        # fallback: avoid breaking the auth flow
        request.session.pop("user", None)

    # Auto-grant session admin when the authenticated user belongs to one of
    # the configured OAuth admin groups, or when their email maps to an
    # admin user in the local DB. This is intentionally session-scoped.
    try:
        user = request.session.get("user")
        if user and isinstance(user, dict):
            groups = user.get("groups") or user.get("memberOf") or user.get("member_of")
            if groups:
                if isinstance(groups, str):
                    groups_list = [g.strip() for g in groups.split(",") if g.strip()]
                elif isinstance(groups, (list, tuple)):
                    groups_list = list(groups)
                else:
                    groups_list = [str(groups)]
                for g in groups_list:
                    if (
                        g in OAUTH_ADMIN_GROUPS_SET
                        or g.lower() in OAUTH_ADMIN_GROUPS_LOWER
                    ):
                        request.session["is_admin"] = True
                        break
        # fallback: map email to DB role
        if not request.session.get("is_admin") and user and isinstance(user, dict):
            email = (
                user.get("email") or user.get("preferred_username") or user.get("sub")
            )
            if email:
                u = db.get_user_by_email(email)
                if u and u[3] == "admin":
                    request.session["is_admin"] = True
    except Exception:
        # don't fail the auth flow if admin mapping fails
        pass

    return RedirectResponse(url="/")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


@app.get("/requests")
async def get_requests(
    request: Request, limit: int = 50, offset: int = 0, status: str = None
):
    # JSON API for requests (protected) with server-side pagination and optional status filter
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    conn = db._connect()
    cur = conn.cursor()
    try:
        if status:
            cur.execute("SELECT COUNT(*) FROM requests WHERE status = ?", (status,))
        else:
            cur.execute("SELECT COUNT(*) FROM requests")
        total = int(cur.fetchone()[0] or 0)
    finally:
        conn.close()
    rows = db.list_requests(limit=limit, offset=offset) if not status else None
    if status:
        # perform filtered query when status provided
        conn = db._connect()
        cur = conn.cursor()
        cur.execute(
            """SELECT id, chat_id, url, status, created_at, description,
                       original_message_id, original_size, final_size, compressed,
                       processing_started_at, processing_finished_at, processing_duration_seconds
                       FROM requests WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?""",
            (status, limit, offset),
        )
        rows = cur.fetchall()
        conn.close()
    results = []
    for r in rows:
        # id, chat_id, url, status, created_at, description,
        # original_message_id, original_size, final_size, compressed,
        # processing_started_at, processing_finished_at, processing_duration_seconds
        (
            rid,
            chat_id,
            url,
            status,
            created_at,
            description,
            original_message_id,
            original_size,
            final_size,
            compressed,
            processing_started_at,
            processing_finished_at,
            processing_duration_seconds,
        ) = r

        def _mb(b):
            try:
                return round(float(b) / (1024 * 1024), 1)
            except Exception:
                return None

        events = []
        try:
            ev_rows = db.get_request_events(rid, limit=20)
            for ev in ev_rows:
                events.append(
                    {
                        "id": ev[0],
                        "type": ev[1],
                        "details": ev[2],
                        "duration_seconds": ev[3],
                        "created_at": ev[4],
                    }
                )
        except Exception:
            events = []
        results.append(
            {
                "id": rid,
                "chat_id": chat_id,
                "url": url,
                "status": status,
                "created_at": created_at,
                "description": description,
                "original_message_id": original_message_id,
                "original_size_bytes": original_size,
                "original_size_mb": _mb(original_size),
                "final_size_bytes": final_size,
                "final_size_mb": _mb(final_size),
                "compressed": bool(compressed) if compressed is not None else None,
                "processing_started_at": processing_started_at,
                "processing_finished_at": processing_finished_at,
                "processing_duration_seconds": processing_duration_seconds,
                "events": events,
            }
        )
    return JSONResponse(
        {"total": total, "offset": offset, "limit": limit, "items": results}
    )


@app.get("/stats")
async def api_stats(request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    # compute simple aggregates: averages and counts
    conn = db._connect()
    cur = conn.cursor()

    def _safe_avg(col):
        try:
            cur.execute(f"SELECT AVG({col}) FROM requests WHERE {col} IS NOT NULL")
            r = cur.fetchone()
            return float(r[0]) if r and r[0] is not None else None
        except Exception:
            return None

    total = db.count_requests()
    avg_orig = _safe_avg("original_size")
    avg_final = _safe_avg("final_size")
    avg_proc = _safe_avg("processing_duration_seconds")
    # how many requests had compress/redownload events
    try:
        cur.execute(
            "SELECT COUNT(DISTINCT request_id) FROM request_events WHERE event_type IN ('compress','redownload')"
        )
        need_proc = int(cur.fetchone()[0] or 0)
    except Exception:
        need_proc = 0
    # additional totals
    try:
        cur = conn.cursor()
        # total downloads started
        cur.execute(
            "SELECT COUNT(*) FROM requests WHERE processing_started_at IS NOT NULL"
        )
        total_downloaded = int(cur.fetchone()[0] or 0)
    except Exception:
        total_downloaded = 0
    try:
        # total successfully sent/completed
        cur.execute(
            "SELECT COUNT(*) FROM requests WHERE status IN ('done','finished','completed')"
        )
        total_sent = int(cur.fetchone()[0] or 0)
    except Exception:
        total_sent = 0
    try:
        # total rate_limited files
        cur.execute("SELECT COUNT(*) FROM requests WHERE status = 'rate_limited'")
        total_rate_limited = int(cur.fetchone()[0] or 0)
    except Exception:
        total_rate_limited = 0
    try:
        # total distinct chats that have persisted rate_limited rows
        cur.execute(
            "SELECT COUNT(DISTINCT chat_id) FROM requests WHERE status = 'rate_limited'"
        )
        total_chats_rate_limited = int(cur.fetchone()[0] or 0)
    except Exception:
        total_chats_rate_limited = 0
    try:
        # total duplicates captured
        cur.execute("SELECT COUNT(*) FROM requests WHERE status = 'duplicate'")
        total_duplicates = int(cur.fetchone()[0] or 0)
    except Exception:
        total_duplicates = 0

    # total bytes (downloaded/uploaded/processed)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(original_size) FROM requests WHERE original_size IS NOT NULL"
        )
        total_original_bytes = int(cur.fetchone()[0] or 0)
    except Exception:
        total_original_bytes = 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT SUM(final_size) FROM requests WHERE final_size IS NOT NULL")
        total_final_bytes = int(cur.fetchone()[0] or 0)
    except Exception:
        total_final_bytes = 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(original_size) FROM requests WHERE processing_started_at IS NOT NULL AND original_size IS NOT NULL"
        )
        total_processed_bytes = int(cur.fetchone()[0] or 0)
    except Exception:
        total_processed_bytes = 0

    conn.close()

    # format and return simple stats
    def mb(v):
        try:
            return round(float(v) / (1024 * 1024), 2) if v is not None else None
        except Exception:
            return None

    # compute currently limited chats from in-process worker when available
    current_limited = []
    try:
        from . import worker as worker_mod

        w = getattr(worker_mod, "active_worker", None)
        now = time.time()
        if w:
            try:
                for cid, next_ts in getattr(w, "_last_rate_limited_next", {}).items():
                    try:
                        if next_ts and float(next_ts) > now:
                            current_limited.append(
                                {
                                    "chat_id": cid,
                                    "next_in_seconds": int(
                                        max(0, float(next_ts) - now)
                                    ),
                                }
                            )
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

    return JSONResponse(
        {
            "total_requests": total,
            "avg_original_size_bytes": avg_orig,
            "avg_original_size_mb": mb(avg_orig),
            "avg_final_size_bytes": avg_final,
            "avg_final_size_mb": mb(avg_final),
            "avg_processing_seconds": avg_proc,
            "requests_need_processing": need_proc,
            "total_downloaded": total_downloaded,
            "total_sent": total_sent,
            "total_original_bytes": total_original_bytes,
            "total_final_bytes": total_final_bytes,
            "total_processed_bytes": total_processed_bytes,
            "total_original_mb": mb(total_original_bytes),
            "total_final_mb": mb(total_final_bytes),
            "total_processed_mb": mb(total_processed_bytes),
            "total_rate_limited_files": total_rate_limited,
            "total_chats_rate_limited": total_chats_rate_limited,
            "total_duplicates": total_duplicates,
            "currently_limited": current_limited,
        }
    )


@app.post("/api/db/clear")
async def api_db_clear(request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        db.clear_history()
        return JSONResponse({"ok": True})
    except Exception:
        raise HTTPException(status_code=500, detail="failed to clear database")


@app.get("/config")
async def api_config(request: Request):
    # lightweight view of runtime configuration for the SPA
    try:
        cfg = {
            "oauth_configured": _oauth_enabled(),
            "admin_token_set": bool(os.environ.get("ADMIN_TOKEN")),
            "MAX_PENDING_PER_CHAT": getattr(config, "MAX_PENDING_PER_CHAT", None),
            "DUPLICATE_WINDOW_SECONDS": getattr(
                config, "DUPLICATE_WINDOW_SECONDS", None
            ),
            "TELEGRAM_MAX_FILE_SIZE": getattr(config, "TELEGRAM_MAX_FILE_SIZE", None),
            "WORKERS": getattr(config, "WORKERS", None),
            "SIMPLE_YTDLP_ONLY": getattr(config, "SIMPLE_YTDLP_ONLY", None),
            "WORKER_GENERATE_THUMBNAIL": getattr(
                config, "WORKER_GENERATE_THUMBNAIL", None
            ),
            "TMP_DIR": getattr(config, "TMP_DIR", None),
        }
    except Exception:
        cfg = {}
    # include in-memory worker details when available
    try:
        from . import worker as worker_mod

        w = getattr(worker_mod, "active_worker", None)
        if w:
            try:
                cfg["rate_limits"] = list(getattr(w, "_rate_limits", []))
                cfg["in_memory_queue_length"] = len(
                    list(getattr(w._queue, "_queue", []))
                )
            except Exception:
                pass
    except Exception:
        pass

    return JSONResponse(cfg)


@app.post("/api/unlimit_chat")
async def api_unlimit_chat(request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        body = await request.json()
    except Exception:
        body = {}
    chat_id = body.get("chat_id")
    requeue = bool(body.get("requeue", False))
    if chat_id is None:
        raise HTTPException(status_code=400, detail="chat_id required")

    updated = 0
    try:
        # clear in-memory markers when worker present
        from . import worker as worker_mod

        w = getattr(worker_mod, "active_worker", None)
        if w:
            try:
                w._last_rate_limited.pop(chat_id, None)
            except Exception:
                pass
            try:
                w._last_rate_limited_next.pop(chat_id, None)
            except Exception:
                pass
            try:
                w._last_rate_warning.pop(chat_id, None)
            except Exception:
                pass
    except Exception:
        pass

    # optionally requeue persisted requests
    try:
        conn = db._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, url, description, original_message_id FROM requests WHERE chat_id = ? AND status = 'rate_limited' ORDER BY created_at ASC",
            (chat_id,),
        )
        rows = cur.fetchall()
        for rid, url, desc, orig_msg in rows:
            try:
                if requeue:
                    db.update_request_status(rid, "queued")
                    try:
                        db.add_request_event(
                            rid, "unlimited", details="unlimited by admin (requeued)"
                        )
                    except Exception:
                        pass
                    # push into in-memory queue if worker available
                    try:
                        from . import worker as worker_mod2

                        w2 = getattr(worker_mod2, "active_worker", None)
                        if w2:
                            item = {
                                "chat_id": chat_id,
                                "url": url,
                                "description": desc,
                                "original_message_id": orig_msg,
                                "enqueued_at": time.time(),
                                "request_id": rid,
                            }
                            try:
                                w2._queue.put_nowait(item)
                            except Exception:
                                try:
                                    asyncio.create_task(w2._queue.put(item))
                                except Exception:
                                    pass
                            try:
                                if getattr(
                                    config, "WORKER_REHYDRATE_ON_UNLIMIT", False
                                ):
                                    try:
                                        getattr(w2, "trigger_rehydrate", lambda: None)()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            # If worker supports a trigger method, ask it to pick up
                            # persisted queued items immediately. This is a noop
                            # when GUI/worker are different processes but helps
                            # when they run together.
                            try:
                                if getattr(
                                    config, "WORKER_REHYDRATE_ON_UNLIMIT", False
                                ):
                                    try:
                                        getattr(w2, "trigger_rehydrate", lambda: None)()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except Exception:
                        pass
                else:
                    # mark event for operator visibility
                    try:
                        # mark status to indicate the rate limit was cleared
                        try:
                            db.update_request_status(rid, "unlimited")
                        except Exception:
                            pass
                        db.add_request_event(
                            rid, "unlimited", details="unlimited by admin (no requeue)"
                        )
                    except Exception:
                        pass
                updated += 1
            except Exception:
                pass
        try:
            conn.commit()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    except Exception:
        pass

    # notify websocket clients to refresh
    try:
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            import asyncio as _asyncio

            _asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast({"type": "unlimited", "chat_id": chat_id}),
                ws_broadcast.loop,
            )
    except Exception:
        pass

    # persist a DB-level update so other processes (worker) pick up the
    # unlimit event even when GUI and worker run separately.
    try:
        db.add_update(json.dumps({"type": "unlimited", "chat_id": chat_id}))
    except Exception:
        pass

    return JSONResponse({"ok": True, "updated": updated})


@app.post("/api/unlimit_all")
async def api_unlimit_all(request: Request):
    """Clear rate-limit markers for all chats. Optional JSON body: {"requeue": true} to requeue persisted items."""
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        body = await request.json()
    except Exception:
        body = {}
    requeue = bool(body.get("requeue", False))

    updated = 0
    try:
        # clear in-memory markers when worker present
        from . import worker as worker_mod

        w = getattr(worker_mod, "active_worker", None)
        if w:
            try:
                w._last_rate_limited.clear()
            except Exception:
                pass
            try:
                w._last_rate_limited_next.clear()
            except Exception:
                pass
            try:
                w._last_rate_warning.clear()
            except Exception:
                pass
    except Exception:
        pass

    # optionally requeue persisted requests (or just mark as unlimited)
    try:
        conn = db._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, chat_id, url, description, original_message_id FROM requests WHERE status = 'rate_limited' ORDER BY created_at ASC"
        )
        rows = cur.fetchall()
        for rid, chat_id, url, desc, orig_msg in rows:
            try:
                if requeue:
                    db.update_request_status(rid, "queued")
                    try:
                        db.add_request_event(
                            rid,
                            "unlimited",
                            details="unlimited by admin (requeued all)",
                        )
                    except Exception:
                        pass
                    # push into in-memory queue if worker available
                    try:
                        from . import worker as worker_mod2

                        w2 = getattr(worker_mod2, "active_worker", None)
                        if w2:
                            item = {
                                "chat_id": chat_id,
                                "url": url,
                                "description": desc,
                                "original_message_id": orig_msg,
                                "enqueued_at": time.time(),
                                "request_id": rid,
                            }
                            try:
                                w2._queue.put_nowait(item)
                            except Exception:
                                try:
                                    asyncio.create_task(w2._queue.put(item))
                                except Exception:
                                    pass
                    except Exception:
                        pass
                else:
                    try:
                        try:
                            db.update_request_status(rid, "unlimited")
                        except Exception:
                            pass
                        db.add_request_event(
                            rid, "unlimited", details="unlimited by admin (no requeue)"
                        )
                    except Exception:
                        pass
                updated += 1
            except Exception:
                pass
        try:
            conn.commit()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    except Exception:
        pass

    # notify websocket clients to refresh
    try:
        from . import ws_broadcast

        if getattr(ws_broadcast, "loop", None):
            import asyncio as _asyncio

            _asyncio.run_coroutine_threadsafe(
                ws_broadcast.broadcast({"type": "unlimited_all"}), ws_broadcast.loop
            )
    except Exception:
        pass

    # persist a DB-level update so other processes (worker) pick up the
    # unlimit_all event even when GUI and worker run separately.
    try:
        db.add_update(json.dumps({"type": "unlimited_all"}))
    except Exception:
        pass

    return JSONResponse({"ok": True, "updated": updated})


@app.post("/api/clear_queue")
async def api_clear_queue(request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    only_chat = body.get("chat_id")

    # Run the heavy clear operation in a background thread to avoid
    # blocking the main event loop (which causes the SPA to hang/white-screen).
    def _clear_queue_sync(only_chat_local):
        cleared_local = 0
        try:
            # clear in-memory queue when worker present
            from . import worker as worker_mod

            w = getattr(worker_mod, "active_worker", None)
            if w:
                try:
                    q = getattr(w._queue, "_queue", None)
                    if q is not None:
                        if only_chat_local is None:
                            cleared_local += len(q)
                            q.clear()
                        else:
                            remaining = []
                            for it in list(q):
                                try:
                                    if it.get("chat_id") == only_chat_local:
                                        cleared_local += 1
                                    else:
                                        remaining.append(it)
                                except Exception:
                                    remaining.append(it)
                            q.clear()
                            for it in remaining:
                                q.append(it)
                except Exception:
                    pass
        except Exception:
            pass

        # update DB: mark queued requests as 'cancelled' for visibility
        try:
            conn = db._connect()
            cur = conn.cursor()
            if only_chat_local is None:
                cur.execute("SELECT id FROM requests WHERE status = 'queued'")
            else:
                cur.execute(
                    "SELECT id FROM requests WHERE status = 'queued' AND chat_id = ?",
                    (only_chat_local,),
                )
            rows = cur.fetchall()
            ids = [r[0] for r in rows]
            for rid in ids:
                try:
                    cur.execute(
                        "UPDATE requests SET status = ? WHERE id = ?",
                        ("cancelled", rid),
                    )
                    try:
                        db.add_request_event(
                            rid, "cancelled", details="cancelled by admin clear_queue"
                        )
                    except Exception:
                        pass
                    cleared_local += 1
                except Exception:
                    pass
            conn.commit()
            try:
                conn.close()
            except Exception:
                pass
        except Exception:
            pass

        # broadcast event so GUIs refresh
        try:
            from . import ws_broadcast

            if getattr(ws_broadcast, "loop", None):
                ws_broadcast.publish_sync({"type": "queue_cleared"})
        except Exception:
            pass

        return cleared_local

    # schedule the blocking work on a thread and return immediately
    try:
        asyncio.create_task(asyncio.to_thread(_clear_queue_sync, only_chat))
    except Exception:
        # fallback: run synchronously if scheduling fails (best-effort)
        try:
            cleared_now = _clear_queue_sync(only_chat)
            return JSONResponse({"ok": True, "cleared": cleared_now})
        except Exception:
            return JSONResponse({"ok": False, "cleared": 0})

    return JSONResponse({"ok": True, "cleared": "scheduled"})


@app.post("/api/queue/start")
async def api_queue_start(request: Request):
    """Manually trigger the worker to ingest/process persisted queued items.

    Optional JSON body: { "rehydrate": true } (default true).
    """
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        body = await request.json()
    except Exception:
        body = {}
    rehydrate = bool(body.get("rehydrate", True))

    try:
        from . import worker as worker_mod

        w = getattr(worker_mod, "active_worker", None)
        if w and rehydrate:
            try:
                getattr(w, "trigger_rehydrate", lambda: None)()
            except Exception:
                pass
    except Exception:
        pass

    return JSONResponse({"ok": True})

    # also expose richer aggregates via a single endpoint


@app.get("/api/aggregates")
async def api_aggregates(request: Request, top_limit: int = 10):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    conn = db._connect()
    cur = conn.cursor()
    # status counts
    status_counts = {}
    try:
        cur.execute("SELECT status, COUNT(*) FROM requests GROUP BY status")
        for s, c in cur.fetchall():
            status_counts[s or "unknown"] = int(c or 0)
    except Exception:
        pass

    # top chats
    top_chats = []
    try:
        cur.execute(
            "SELECT chat_id, COUNT(*) as cnt FROM requests GROUP BY chat_id ORDER BY cnt DESC LIMIT ?",
            (top_limit,),
        )
        for chat_id, cnt in cur.fetchall():
            top_chats.append({"chat_id": chat_id, "count": int(cnt or 0)})
    except Exception:
        pass

    # processing duration histogram buckets
    buckets = [(0, 1), (1, 5), (5, 10), (10, 30), (30, 60), (60, None)]
    labels = ["0-1s", "1-5s", "5-10s", "10-30s", "30-60s", "60s+"]
    counts = []
    try:
        for low, high in buckets:
            if high is None:
                cur.execute(
                    "SELECT COUNT(*) FROM requests WHERE processing_duration_seconds IS NOT NULL AND processing_duration_seconds >= ?",
                    (low,),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM requests WHERE processing_duration_seconds IS NOT NULL AND processing_duration_seconds >= ? AND processing_duration_seconds < ?",
                    (low, high),
                )
            counts.append(int(cur.fetchone()[0] or 0))
    except Exception:
        counts = [0] * len(labels)

    conn.close()
    return JSONResponse(
        {
            "status_counts": status_counts,
            "top_chats": top_chats,
            "duration_histogram": {"labels": labels, "counts": counts},
        }
    )


@app.get("/api/queue")
async def api_queue(request: Request, limit: int = 50):
    """Return a snapshot of the current WorkerPool queue and running tasks."""
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        # import worker module lazily to avoid startup import cycles
        from . import worker as worker_mod

        w = getattr(worker_mod, "active_worker", None)
        if not w:
            # No in-process worker: fall back to persisted requests in DB
            try:
                conn = db._connect()
                cur = conn.cursor()
                # queued items
                cur.execute(
                    "SELECT id, chat_id, url, status, created_at FROM requests WHERE status = 'queued' ORDER BY created_at ASC LIMIT ?",
                    (limit,),
                )
                queued_rows = cur.fetchall()
                # running items: only treat as running if processing_started_at is recent
                window = int(os.environ.get("GUI_PROCESSING_WINDOW_SECONDS", "1800"))
                cutoff = (
                    datetime.datetime.utcnow() - datetime.timedelta(seconds=window)
                ).isoformat()
                cur.execute(
                    "SELECT id, chat_id, url, status, processing_started_at FROM requests WHERE status IN ('processing','sending') AND processing_started_at >= ? ORDER BY processing_started_at DESC LIMIT ?",
                    (cutoff, limit),
                )
                running_rows = cur.fetchall()
                try:
                    conn.close()
                except Exception:
                    pass
            except Exception:
                queued_rows = []
                running_rows = []

            queued = []
            running = []
            for r in queued_rows:
                try:
                    rid, chat_id, url, status, created_at = r
                    queued.append(
                        {
                            "request_id": rid,
                            "chat_id": chat_id,
                            "url": url,
                            "enqueued_at": created_at,
                        }
                    )
                except Exception:
                    pass
            for r in running_rows:
                try:
                    rid, chat_id, url, status, started_at = r
                    running.append(
                        {
                            "request_id": rid,
                            "chat_id": chat_id,
                            "url": url,
                            "enqueued_at": started_at,
                        }
                    )
                except Exception:
                    pass

            return JSONResponse(
                {
                    "queue_length": len(queued_rows) + len(running_rows),
                    "queued": queued,
                    "running": running,
                }
            )

        # queued items (internal asyncio.Queue deque)
        try:
            queued_raw = list(getattr(w._queue, "_queue", []))
        except Exception:
            queued_raw = []
        queued = []
        for it in queued_raw[:limit]:
            try:
                queued.append(
                    {
                        "chat_id": it.get("chat_id"),
                        "url": it.get("url"),
                        "enqueued_at": it.get("enqueued_at"),
                    }
                )
            except Exception:
                pass

        running = []
        try:
            tasks = list(getattr(w, "_tasks", set()))
            for t in tasks:
                try:
                    if t.done():
                        continue
                except Exception:
                    pass
                item = getattr(t, "_item", None)
                if item:
                    running.append(
                        {
                            "chat_id": item.get("chat_id"),
                            "url": item.get("url"),
                            "enqueued_at": item.get("enqueued_at"),
                        }
                    )
        except Exception:
            running = []

        return JSONResponse(
            {
                "queue_length": len(queued_raw),
                "queued": queued,
                "running": running,
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rate_limits")
async def api_rate_limits(request: Request, per_chat_limit: int = 200):
    """Return current in-memory rate-limit state and recent rate-limited requests.

    Exposes per-chat counters for configured windows and lists recent
    `requests` rows that were persisted with status='rate_limited'.
    """
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        from . import worker as worker_mod

        w = getattr(worker_mod, "active_worker", None)

        # If no active worker is present (GUI running as a separate process),
        # still return persisted recent rate_limited/duplicate rows from DB.
        if not w:
            limits = []
            per_chat = []
        else:
            limits = list(getattr(w, "_rate_limits", []))
            now = time.time()

            # collect chat ids known in memory
            chat_ids = set()
            try:
                chat_ids.update(getattr(w, "_chat_timestamps", {}).keys())
            except Exception:
                pass
            try:
                chat_ids.update(getattr(w, "_last_rate_limited", {}).keys())
            except Exception:
                pass
            try:
                chat_ids.update(getattr(w, "_last_rate_warning", {}).keys())
            except Exception:
                pass

            per_chat = []
            for cid in list(chat_ids)[:per_chat_limit]:
                try:
                    ts = list(getattr(w, "_chat_timestamps", {}).get(cid, []))
                except Exception:
                    ts = []
                counters = []
                next_available_seconds = None
                try:
                    for limit, window in limits:
                        cnt = sum(1 for t in ts if t >= now - window)
                        # compute next free slot if limit reached
                        next_in = 0
                        if cnt >= limit:
                            relevant = [t for t in ts if t >= now - window]
                            if relevant:
                                oldest = min(relevant)
                                next_in = int(max(0, (oldest + window) - now))
                        counters.append(
                            {
                                "limit": limit,
                                "window_seconds": window,
                                "count": int(cnt),
                                "next_in_seconds": next_in,
                            }
                        )
                        if next_in and (
                            next_available_seconds is None
                            or next_in > next_available_seconds
                        ):
                            next_available_seconds = next_in
                except Exception:
                    counters = []

                last_l = None
                try:
                    last_l = getattr(w, "_last_rate_limited", {}).get(cid)
                except Exception:
                    last_l = None
                last_next = None
                try:
                    last_next = getattr(w, "_last_rate_limited_next", {}).get(cid)
                except Exception:
                    last_next = None
                last_warn = None
                try:
                    last_warn = getattr(w, "_last_rate_warning", {}).get(cid)
                except Exception:
                    last_warn = None

                per_chat.append(
                    {
                        "chat_id": cid,
                        "counters": counters,
                        "next_available_seconds": next_available_seconds,
                        "last_rate_limited_at": last_l,
                        "last_rate_limited_next": last_next,
                        "last_rate_warning_at": last_warn,
                    }
                )

        # recent persisted rate_limited or duplicate requests
        recent = []
        try:
            conn = db._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, chat_id, url, status, created_at FROM requests WHERE status IN ('rate_limited','duplicate') ORDER BY created_at DESC LIMIT ?",
                (per_chat_limit,),
            )
            for r in cur.fetchall():
                recent.append(
                    {
                        "id": r[0],
                        "chat_id": r[1],
                        "url": r[2],
                        "status": r[3],
                        "created_at": r[4],
                    }
                )
            conn.close()
        except Exception:
            recent = []

        # persisted limited chats: distinct chat ids that have persisted
        # rate_limited rows. This allows the GUI to show a concise list of
        # blocked chats that can be unblocked individually.
        persisted_limited = []
        try:
            conn = db._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT chat_id, COUNT(*) as cnt FROM requests WHERE status = 'rate_limited' GROUP BY chat_id ORDER BY cnt DESC LIMIT ?",
                (per_chat_limit,),
            )
            for cid, cnt in cur.fetchall():
                persisted_limited.append({"chat_id": cid, "count": int(cnt or 0)})
            conn.close()
        except Exception:
            persisted_limited = []

        # compute currently limited chats from worker memory when available
        currently_limited = []
        try:
            now = time.time()
            for cid in list(getattr(w, "_last_rate_limited_next", {}).keys()):
                try:
                    next_ts = getattr(w, "_last_rate_limited_next", {}).get(cid)
                    if next_ts and float(next_ts) > now:
                        currently_limited.append(
                            {
                                "chat_id": cid,
                                "next_in_seconds": int(max(0, float(next_ts) - now)),
                            }
                        )
                except Exception:
                    pass
        except Exception:
            currently_limited = []

        return JSONResponse(
            {
                "limits": limits,
                "per_chat": per_chat,
                "recent_rate_limited": recent,
                "persisted_limited_chats": persisted_limited,
                "currently_limited": currently_limited,
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/requests/{request_id}")
async def get_request_detail(request: Request, request_id: int):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    conn = db._connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, chat_id, url, status, created_at, description, original_message_id, original_size, final_size, compressed, processing_started_at, processing_finished_at, processing_duration_seconds FROM requests WHERE id = ?",
            (request_id,),
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="not found")
        (
            rid,
            chat_id,
            url,
            status,
            created_at,
            description,
            original_message_id,
            original_size,
            final_size,
            compressed,
            processing_started_at,
            processing_finished_at,
            processing_duration_seconds,
        ) = r
    finally:
        conn.close()
    events = []
    try:
        evs = db.get_request_events(rid, limit=200)
        for ev in evs:
            events.append(
                {
                    "id": ev[0],
                    "type": ev[1],
                    "details": ev[2],
                    "duration_seconds": ev[3],
                    "created_at": ev[4],
                }
            )
    except Exception:
        events = []

    def _mb(b):
        try:
            return round(float(b) / (1024 * 1024), 1)
        except Exception:
            return None

    return JSONResponse(
        {
            "id": rid,
            "chat_id": chat_id,
            "url": url,
            "status": status,
            "created_at": created_at,
            "description": description,
            "original_size_mb": _mb(original_size),
            "final_size_mb": _mb(final_size),
            "compressed": bool(compressed) if compressed is not None else None,
            "processing_started_at": processing_started_at,
            "processing_finished_at": processing_finished_at,
            "processing_duration_seconds": processing_duration_seconds,
            "events": events,
        }
    )


@app.get("/api/updates")
async def api_updates(request: Request, limit: int = 50, offset: int = 0):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    total = db.count_updates()
    rows = db.list_updates(limit=limit, offset=offset)
    results = [{"id": r[0], "raw": r[1], "created_at": r[2]} for r in rows]
    return JSONResponse(
        {"total": total, "offset": offset, "limit": limit, "items": results}
    )


@app.get("/api/users")
async def api_list_users(request: Request, limit: int = 50, offset: int = 0):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    rows = db.list_users(limit=limit, offset=offset)
    results = [
        {"id": r[0], "username": r[1], "email": r[2], "role": r[3], "created_at": r[4]}
        for r in rows
    ]
    return JSONResponse({"items": results, "offset": offset, "limit": limit})


@app.post("/api/session/grant_admin")
async def api_session_grant_admin(request: Request):
    # Allow a logged-in OAuth session to grant itself admin rights for the
    # current session. This is intentionally session-scoped and does not
    # modify persistent DB state.
    session = request.session
    if not session or not session.get("user"):
        raise HTTPException(status_code=403, detail="not logged in")
    # Only allow granting admin for sessions that already have a persistent
    # entitlement (group membership or DB role). Do NOT allow any logged-in
    # user to self-elevate.
    if not _session_has_persistent_entitlement(session):
        raise HTTPException(status_code=403, detail="not entitled")
    try:
        session["is_admin"] = True
    except Exception:
        raise HTTPException(status_code=500, detail="failed to set session")
    return JSONResponse({"ok": True})


@app.post("/api/session/revoke_admin")
async def api_session_revoke_admin(request: Request):
    session = request.session
    try:
        if session and session.get("is_admin"):
            session.pop("is_admin", None)
    except Exception:
        pass
    return JSONResponse({"ok": True})


@app.post("/api/users")
async def api_create_user(request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    payload = await request.json()
    username = payload.get("username")
    email = payload.get("email")
    role = payload.get("role", "user")
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    user_id = db.add_user(username=username, email=email, role=role)
    return JSONResponse(
        {"id": user_id, "username": username, "email": email, "role": role}
    )


@app.put("/api/users/{user_id}/role")
async def api_set_user_role(user_id: int, request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    payload = await request.json()
    role = payload.get("role")
    if role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    db.set_user_role(user_id, role)
    return JSONResponse({"ok": True})


@app.delete("/api/users/{user_id}")
async def api_delete_user(user_id: int, request: Request):
    if not _check_admin(request):
        raise HTTPException(status_code=403, detail="forbidden")
    db.delete_user(user_id)
    return JSONResponse({"ok": True})


@app.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket):
    # require admin
    await websocket.accept()
    try:
        if not _check_admin_ws(websocket):
            await websocket.close(code=1008)
            return
    except Exception:
        await websocket.close(code=1008)
        return

    q: asyncio.Queue = asyncio.Queue()
    await ws_broadcast.register_queue(q)
    try:
        # send initial snapshot
        recent_requests = db.list_requests(limit=50, offset=0)
        reqs = []
        for r in recent_requests:
            (
                rid,
                chat_id,
                url,
                status,
                created_at,
                description,
                original_message_id,
                original_size,
                final_size,
                compressed,
                processing_started_at,
                processing_finished_at,
                processing_duration_seconds,
            ) = r

            def _mb(b):
                try:
                    return round(float(b) / (1024 * 1024), 1)
                except Exception:
                    return None

            reqs.append(
                {
                    "id": rid,
                    "chat_id": chat_id,
                    "url": url,
                    "status": status,
                    "created_at": created_at,
                    "description": description,
                    "original_message_id": original_message_id,
                    "original_size_mb": _mb(original_size),
                    "final_size_mb": _mb(final_size),
                    "compressed": bool(compressed) if compressed is not None else None,
                    "processing_started_at": processing_started_at,
                    "processing_finished_at": processing_finished_at,
                    "processing_duration_seconds": processing_duration_seconds,
                }
            )
        await websocket.send_json({"type": "initial", "requests": reqs})
        recent_updates = db.list_updates(limit=50, offset=0)
        await websocket.send_json(
            {
                "type": "initial_updates",
                "updates": [
                    {"id": r[0], "raw": r[1], "created_at": r[2]}
                    for r in recent_updates
                ],
            }
        )

        while True:
            try:
                msg = await q.get()
                await websocket.send_json(msg)
            except WebSocketDisconnect:
                break
            except Exception:
                # if sending fails, close
                break
    finally:
        await ws_broadcast.unregister_queue(q)
