import asyncio
import logging
import os

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
if OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and OAUTH_AUTHORIZE_URL and OAUTH_TOKEN_URL:
    # support optional OIDC discovery URL (server_metadata_url) so authlib
    # can fetch `jwks_uri` and other endpoints automatically. If you run
    # against an OpenID Connect provider, set `OAUTH_SERVER_METADATA_URL`
    # (e.g. https://<issuer>/.well-known/openid-configuration).
    register_kwargs = dict(
        name="provider",
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
        authorize_url=OAUTH_AUTHORIZE_URL,
        access_token_url=OAUTH_TOKEN_URL,
        client_kwargs={"scope": OAUTH_SCOPE},
    )
    if OAUTH_SERVER_METADATA_URL:
        register_kwargs["server_metadata_url"] = OAUTH_SERVER_METADATA_URL
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
async def gui_config():
    oauth_conf = _oauth_enabled()
    admin_token_present = bool(os.environ.get("ADMIN_TOKEN"))
    # admin token is only effective when OAuth is NOT configured
    admin_token_effective = admin_token_present and not oauth_conf
    return JSONResponse(
        {
            "oauth_configured": oauth_conf,
            "admin_token_set": admin_token_effective,
        }
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
    conn.close()

    # format and return simple stats
    def mb(v):
        try:
            return round(float(v) / (1024 * 1024), 2) if v is not None else None
        except Exception:
            return None

    return JSONResponse(
        {
            "total_requests": total,
            "avg_original_size_bytes": avg_orig,
            "avg_original_size_mb": mb(avg_orig),
            "avg_final_size_bytes": avg_final,
            "avg_final_size_mb": mb(avg_final),
            "avg_processing_seconds": avg_proc,
            "requests_need_processing": need_proc,
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
