"""Admin GUI authentication / authorization (src.gui_auth + the auth routes)."""

import asyncio
import base64
import dataclasses
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import itsdangerous
import pytest
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.responses import RedirectResponse

from src import db, gui, gui_auth
from src.gui_auth import AuthSettings, OAuthSettings

TOKEN = "admin-token-0123456789abcdef"
SAME = {"Origin": "http://testserver"}
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "requests.db"
    monkeypatch.setenv("REQUESTS_DB", str(path))
    db.init_db()
    return path


def settings(**overrides):
    values = {"secret_key": "k" * 40, "admin_token": TOKEN}
    values.update(overrides)
    return AuthSettings(**values)


def client_for(app, **kwargs):
    return TestClient(app, **kwargs)


class FakeProvider:
    """Stand-in for an authlib client (authorize_redirect / token / userinfo)."""

    def __init__(self, claims=None, *, userinfo=None, token=None, error=None):
        self.claims = claims or {}
        self.userinfo_claims = userinfo
        self.token = token
        self.error = error
        self.redirect_uri = None
        self.userinfo_calls = 0

    async def authorize_redirect(self, request, redirect_uri):
        self.redirect_uri = str(redirect_uri)
        request.session["_state_provider_abc"] = {"data": {}, "exp": time.time() + 60}
        return RedirectResponse("https://idp.example.com/authorize?state=abc", 302)

    async def authorize_access_token(self, request):
        if self.error is not None:
            raise self.error
        if self.token is not None:
            return self.token
        return {"access_token": "at", "userinfo": dict(self.claims)}

    async def userinfo(self, token):
        self.userinfo_calls += 1
        if isinstance(self.userinfo_claims, BaseException):
            raise self.userinfo_claims
        return dict(self.userinfo_claims or {})


def oauth_app(provider, **overrides):
    registry = SimpleNamespace(provider=provider)
    return gui.create_app(settings(**overrides), oauth_registry=registry)


def oauth_login(app, provider_or_client=None):
    client = provider_or_client or TestClient(app)
    assert client.get("/login", follow_redirects=False).status_code == 302
    response = client.get("/auth", follow_redirects=False)
    return client, response


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, "", "   ", "dev-secret-change-me"])
def test_unusable_secret_key_becomes_random_per_process(value):
    env = {} if value is None else {"SECRET_KEY": value}
    first = AuthSettings.from_env(env)
    second = AuthSettings.from_env(env)
    assert first.secret_key_ephemeral is True
    assert first.secret_key != second.secret_key
    assert len(first.secret_key) >= 32
    assert first.secret_key != "dev-secret-change-me"
    assert any("SECRET_KEY" in warning for warning in first.warnings)


def test_short_secret_key_is_used_with_a_warning():
    loaded = AuthSettings.from_env({"SECRET_KEY": "short-key"})
    assert loaded.secret_key == "short-key"
    assert loaded.secret_key_ephemeral is False
    assert any("shorter than 32" in warning for warning in loaded.warnings)
    strong = AuthSettings.from_env({"SECRET_KEY": "x" * 40})
    assert strong.warnings == ()


def test_settings_parsing():
    loaded = AuthSettings.from_env(
        {
            "SECRET_KEY": "x" * 40,
            "ADMIN_TOKEN": " short ",
            "OAUTH_ADMIN_GROUPS": "Admins, ops ,,",
            "SESSION_COOKIE_SECURE": "true",
            "SESSION_MAX_AGE_SECONDS": "99999999",
            "GUI_TRUSTED_ORIGINS": "https://Admin.Example.com:443, nonsense",
        }
    )
    assert loaded.admin_token == "short"
    assert loaded.admin_groups == frozenset({"admins", "ops"})
    assert loaded.cookie_secure is True
    assert loaded.session_max_age == gui_auth.MAX_SESSION_MAX_AGE
    assert loaded.trusted_origins == frozenset({"https://admin.example.com"})
    text = " ".join(loaded.warnings)
    assert "ADMIN_TOKEN is shorter" in text
    assert "SESSION_MAX_AGE_SECONDS" in text
    assert "nonsense" in text
    defaults = AuthSettings.from_env(
        {"SECRET_KEY": "x" * 40, "SESSION_COOKIE_SECURE": "maybe"}
    )
    assert defaults.cookie_secure is False
    assert defaults.session_max_age == gui_auth.DEFAULT_SESSION_MAX_AGE
    assert defaults.admin_token is None
    assert any("not a boolean" in w for w in defaults.warnings)


def test_partial_oauth_configuration_is_disabled_with_a_warning():
    loaded = AuthSettings.from_env({"SECRET_KEY": "x" * 40, "OAUTH_CLIENT_ID": "id"})
    assert loaded.oauth is None
    assert any("OAuth is disabled" in w for w in loaded.warnings)


def test_oauth_settings_are_passed_to_authlib():
    env = {
        "SECRET_KEY": "x" * 40,
        "OAUTH_CLIENT_ID": "cid",
        "OAUTH_CLIENT_SECRET": "csecret",
        "OAUTH_AUTHORIZE_URL": "https://idp.example.com/authorize",
        "OAUTH_TOKEN_URL": "https://idp.example.com/token",
        "OAUTH_USERINFO_URL": "https://idp.example.com/userinfo",
        "OAUTH_JWKS_URI": "https://idp.example.com/jwks",
        "OAUTH_ISSUER": "https://idp.example.com",
        "OAUTH_REDIRECT_URI": "https://gui.example.com/auth",
        "OAUTH_TIMEOUT_SECONDS": "7",
    }
    loaded = AuthSettings.from_env(env)
    assert loaded.oauth.redirect_uri == "https://gui.example.com/auth"
    assert loaded.oauth.timeout == 7.0
    # the redirect URI's origin is trusted for CSRF checks
    assert "https://gui.example.com" in loaded.trusted_origins
    client = gui_auth.get_oauth_client(gui_auth.build_oauth_registry(loaded))
    assert client is not None
    assert client.server_metadata["jwks_uri"] == "https://idp.example.com/jwks"
    assert client.server_metadata["userinfo_endpoint"] == (
        "https://idp.example.com/userinfo"
    )
    assert client.server_metadata["issuer"] == "https://idp.example.com"
    assert client.client_kwargs["timeout"] == 7.0
    assert not any("ID tokens" in w for w in loaded.warnings)


def test_openid_scope_without_jwks_or_discovery_warns():
    env = {
        "SECRET_KEY": "x" * 40,
        "OAUTH_CLIENT_ID": "cid",
        "OAUTH_CLIENT_SECRET": "csecret",
        "OAUTH_AUTHORIZE_URL": "https://idp.example.com/authorize",
        "OAUTH_TOKEN_URL": "https://idp.example.com/token",
        "OAUTH_REDIRECT_URI": "not a url",
    }
    loaded = AuthSettings.from_env(env)
    assert loaded.oauth is not None
    assert loaded.oauth.redirect_uri is None
    text = " ".join(loaded.warnings)
    assert "ID tokens" in text
    assert "OAUTH_REDIRECT_URI" in text


# ---------------------------------------------------------------------------
# Cookies cannot be forged into admin
# ---------------------------------------------------------------------------
def _signed_cookie(key, payload):
    data = base64.b64encode(json.dumps(payload).encode())
    return itsdangerous.TimestampSigner(key).sign(data).decode()


@pytest.mark.parametrize("cookie_name", ["session", gui_auth.SESSION_COOKIE_NAME])
def test_cookie_forged_with_the_old_public_default_key_is_rejected(cookie_name):
    app = gui.create_app(AuthSettings.from_env({"ADMIN_TOKEN": TOKEN}))
    client = TestClient(app)
    forged = _signed_cookie("dev-secret-change-me", {"is_admin": True})
    client.cookies.set(cookie_name, forged)
    assert client.get("/api/requests").status_code == 401
    assert client.get("/api/me").json()["is_admin"] is False
    response = client.post("/api/history/clear", headers=SAME)
    assert response.status_code == 401


def test_authorization_is_never_read_from_the_cookie():
    # Even with the real key, a cookie that just *claims* admin is anonymous.
    app = gui.create_app(settings())
    client = TestClient(app)
    payload = {"is_admin": True, "user": {"email": "x@example.com"}}
    client.cookies.set(gui_auth.SESSION_COOKIE_NAME, _signed_cookie("k" * 40, payload))
    me = client.get("/api/me")
    assert me.json()["is_admin"] is False
    # the stale legacy session is dropped
    assert gui_auth.SESSION_COOKIE_NAME + "=null" in me.headers["set-cookie"]
    assert client.get("/api/requests").status_code == 401


# ---------------------------------------------------------------------------
# Bearer token
# ---------------------------------------------------------------------------
def test_bearer_token_matrix():
    client = TestClient(gui.create_app(settings()))
    assert client.get("/api/requests").status_code == 401
    ok = client.get("/api/requests", headers=BEARER)
    assert ok.status_code == 200
    assert client.get("/api/me", headers=BEARER).json()["auth"] == "bearer"
    wrong = client.get("/api/requests", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "Token non valido"}
    assert wrong.headers["www-authenticate"] == "Bearer"
    empty = client.get("/api/requests", headers={"Authorization": "Bearer "})
    assert empty.status_code == 401
    # a wrong bearer token never falls back to a valid session cookie
    client.post("/login", json={"token": TOKEN}, headers=SAME)
    assert client.get("/api/requests").status_code == 200
    mixed = client.get("/api/requests", headers={"Authorization": "Bearer nope"})
    assert mixed.status_code == 401


def test_query_string_token_is_not_accepted():
    client = TestClient(gui.create_app(settings()))
    assert client.get(f"/api/requests?token={TOKEN}").status_code == 401
    assert client.get(f"/requests?token={TOKEN}").status_code == 401


def test_other_authorization_schemes_are_ignored():
    client = TestClient(gui.create_app(settings()))
    basic = {"Authorization": "Basic dXNlcjpwYXNz"}
    assert client.get("/api/requests", headers=basic).status_code == 401
    client.post("/login", json={"token": TOKEN}, headers=SAME)
    # e.g. a proxy adding Basic auth does not break the cookie session
    assert client.get("/api/requests", headers=basic).status_code == 200


def test_bearer_header_is_ignored_when_token_auth_is_disabled():
    provider = FakeProvider(
        {"sub": "u", "email": "a@example.com", "email_verified": True}
    )
    db.add_user(email="a@example.com", role="admin")
    app = oauth_app(provider, admin_token=None)
    client, _ = oauth_login(app)
    assert client.get("/api/requests", headers=BEARER).status_code == 200


def test_failed_token_attempts_are_throttled():
    app = gui.create_app(settings())
    app.state.gui.limiter = gui_auth.FailureLimiter(max_failures=3, window_seconds=60)
    client = TestClient(app)
    for _ in range(3):
        assert (
            client.post("/login", json={"token": "bad"}, headers=SAME).status_code
            == 401
        )
    blocked = client.post("/login", json={"token": TOKEN}, headers=SAME)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    bearer = client.get("/api/requests", headers=BEARER)
    assert bearer.status_code == 429
    app.state.gui.limiter.reset("testclient")
    assert client.post("/login", json={"token": TOKEN}, headers=SAME).status_code == 200


def test_failure_limiter_window():
    now = [1000.0]
    limiter = gui_auth.FailureLimiter(
        max_failures=2, window_seconds=10, max_keys=2, clock=lambda: now[0]
    )
    limiter.record_failure("a")
    assert limiter.retry_after("a") is None
    limiter.record_failure("a")
    assert limiter.retry_after("a") == pytest.approx(10)
    now[0] += 10.5
    assert limiter.retry_after("a") is None
    for key in ("b", "c", "d"):
        limiter.record_failure(key)
    assert len(limiter._failures) == 2  # bounded memory


# ---------------------------------------------------------------------------
# Token sessions
# ---------------------------------------------------------------------------
def test_token_login_json_session_and_logout():
    client = TestClient(gui.create_app(settings()))
    response = client.post("/login", json={"token": TOKEN}, headers=SAME)
    assert response.status_code == 200
    assert response.json()["me"]["is_admin"] is True
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(gui_auth.SESSION_COOKIE_NAME + "=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert f"max-age={gui_auth.DEFAULT_SESSION_MAX_AGE}" in cookie
    assert "secure" not in cookie
    me = client.get("/api/me").json()
    assert me == {
        "authenticated": True,
        "is_admin": True,
        "auth": "token",
        "role": None,
        "user": {"name": "Amministratore (token)"},
    }
    assert client.get("/api/requests").status_code == 200
    logout = client.post("/logout", headers=SAME, follow_redirects=False)
    assert logout.status_code == 303
    assert logout.headers["location"] == "./"
    assert client.get("/api/me").json()["authenticated"] is False
    assert client.get("/api/requests").status_code == 401


def test_logout_json_and_get_not_allowed():
    client = TestClient(gui.create_app(settings()))
    client.post("/login", json={"token": TOKEN}, headers=SAME)
    assert client.get("/logout").status_code == 405
    assert client.get("/api/me").json()["is_admin"] is True
    response = client.post("/logout", json={}, headers=SAME)
    assert response.json() == {"ok": True}
    assert client.get("/api/me").json()["is_admin"] is False


def test_token_login_form_flow():
    client = TestClient(gui.create_app(settings()))
    page = client.get("/login")
    assert page.status_code == 200
    assert 'name="token"' in page.text
    wrong = client.post("/login", data={"token": "nope"}, headers=SAME)
    assert wrong.status_code == 401
    assert "Token non valido" in wrong.text
    assert "text/html" in wrong.headers["content-type"]
    ok = client.post(
        "/login", data={"token": TOKEN}, headers=SAME, follow_redirects=False
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "./"
    assert client.get("/api/me").json()["is_admin"] is True


def test_wrong_or_missing_token_login_is_401_json():
    client = TestClient(gui.create_app(settings()))
    for body in ({"token": "wrong"}, {}, {"token": 123}):
        response = client.post("/login", json=body, headers=SAME)
        assert response.status_code == 401
        assert response.json() == {"detail": "Token non valido"}
    json_headers = {**SAME, "Content-Type": "application/json"}
    # a lone surrogate (valid JSON escape) and broken JSON are just wrong tokens
    for raw in (b'{"token": "\\ud800x"}', b"{not json"):
        response = client.post("/login", content=raw, headers=json_headers)
        assert response.status_code == 401
    huge = client.post(
        "/login",
        content=b"token=" + b"a" * 10000,
        headers={**SAME, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert huge.status_code == 413


def test_token_login_unavailable_without_admin_token():
    app = oauth_app(FakeProvider({"sub": "x"}), admin_token=None)
    client = TestClient(app)
    response = client.post("/login", json={"token": TOKEN}, headers=SAME)
    assert response.status_code == 400
    assert client.get("/config").json() == {
        "oauth_configured": True,
        "admin_token_set": False,
    }


def test_rotating_the_admin_token_invalidates_token_sessions():
    first = gui.create_app(settings(admin_token="first-token-0123456789"))
    client = TestClient(first)
    assert (
        client.post(
            "/login", json={"token": "first-token-0123456789"}, headers=SAME
        ).status_code
        == 200
    )
    cookie = client.cookies.get(gui_auth.SESSION_COOKIE_NAME)
    same_token = TestClient(
        gui.create_app(settings(admin_token="first-token-0123456789"))
    )
    same_token.cookies.set(gui_auth.SESSION_COOKIE_NAME, cookie)
    assert same_token.get("/api/me").json()["is_admin"] is True
    rotated = TestClient(gui.create_app(settings(admin_token="second-token-012345678")))
    rotated.cookies.set(gui_auth.SESSION_COOKIE_NAME, cookie)
    assert rotated.get("/api/me").json()["is_admin"] is False
    assert rotated.get("/api/requests").status_code == 401


def test_session_lifetime_is_bounded():
    config = settings(session_max_age=600)
    fingerprint = gui_auth.token_fingerprint(config)
    fresh = {"v": 1, "auth": "token", "fp": fingerprint, "iat": int(time.time())}
    principal = gui_auth.session_principal(fresh, config, oauth_enabled=False)
    assert principal.is_admin is True
    old = dict(fresh, iat=int(time.time()) - 700)
    assert (
        gui_auth.session_principal(old, config, oauth_enabled=False).is_admin is False
    )
    future = dict(fresh, iat=int(time.time()) + 3600)
    assert (
        gui_auth.session_principal(future, config, oauth_enabled=False).is_admin
        is False
    )
    assert gui_auth.session_principal(
        dict(fresh, v=2), config, oauth_enabled=False
    ) == (gui_auth.ANONYMOUS)
    response = TestClient(gui.create_app(config)).post(
        "/login", json={"token": TOKEN}, headers=SAME
    )
    assert "max-age=600" in response.headers["set-cookie"].lower()


def test_secure_cookie_flag_is_configurable():
    client = TestClient(gui.create_app(settings(cookie_secure=True)))
    response = client.post("/login", json={"token": TOKEN}, headers=SAME)
    assert "; secure" in response.headers["set-cookie"].lower()


def test_secure_cookie_over_plain_http_is_reported(caplog):
    app = gui.create_app(settings(cookie_secure=True))
    lan = TestClient(app, base_url="http://gui.lan:8081")
    response = lan.post("/login", json={"token": TOKEN}, headers=lan_origin())
    assert response.status_code == 200
    assert "SESSION_COOKIE_SECURE=true" in caplog.text
    assert "plain HTTP" in caplog.text
    caplog.clear()
    behind_tls = {
        "Origin": "https://gui.example.com",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "gui.example.com",
    }
    assert lan.post("/login", json={"token": TOKEN}, headers=behind_tls).is_success
    plain = TestClient(gui.create_app(settings()), base_url="http://gui.lan:8081")
    assert plain.post("/login", json={"token": TOKEN}, headers=lan_origin()).is_success
    assert "plain HTTP" not in caplog.text


def lan_origin():
    return {"Origin": "http://gui.lan:8081"}


# ---------------------------------------------------------------------------
# CSRF / Origin checks
# ---------------------------------------------------------------------------
def _logged_in_client(app=None):
    client = TestClient(app or gui.create_app(settings()))
    assert client.post("/login", json={"token": TOKEN}, headers=SAME).status_code == 200
    return client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example.com"},
        {"Origin": "null"},
        {"Origin": "http://testserver.evil.com"},
        {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
        {"Referer": "https://evil.example.com/page"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cookie_authenticated_unsafe_requests_must_be_same_origin(headers):
    client = _logged_in_client()
    db.create_request(1, "https://example.com/v/1")
    response = client.post("/api/queue/cancel", json={}, headers=headers)
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]
    assert db.count_requests(status="queued") == 1
    assert client.delete("/api/users/1", headers=headers).status_code == 403
    assert client.post("/logout", headers=headers).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        SAME,
        {"Origin": "http://testserver:80"},
        {"Referer": "http://testserver/#queue"},
        {"Sec-Fetch-Site": "same-origin"},
        {"Origin": "https://gui.example.com", "X-Forwarded-Host": "gui.example.com"},
    ],
)
def test_same_origin_unsafe_requests_are_allowed(headers):
    client = _logged_in_client()
    response = client.post("/api/queue/cancel", json={}, headers=headers)
    assert response.status_code == 200


def test_trusted_origin_is_allowed():
    app = gui.create_app(
        settings(trusted_origins=frozenset({"https://gui.example.com"}))
    )
    client = _logged_in_client(app)
    headers = {"Origin": "https://gui.example.com"}
    assert client.post("/api/queue/cancel", json={}, headers=headers).status_code == 200


def test_bearer_requests_are_exempt_from_csrf():
    client = TestClient(gui.create_app(settings()))
    headers = {**BEARER, "Origin": "https://evil.example.com"}
    assert client.post("/api/queue/cancel", json={}, headers=headers).status_code == 200


def test_json_endpoints_refuse_text_plain_bodies():
    client = _logged_in_client()
    response = client.post(
        "/api/rate_limits/unlimit",
        content=json.dumps({"chat_id": 1}),
        headers={**SAME, "Content-Type": "text/plain"},
    )
    assert response.status_code == 422


def test_origin_allowed_unit():
    config = settings()

    def allowed(**headers):
        return gui_auth.origin_allowed(Headers(headers), config)

    assert allowed(host="gui.example.com", origin="https://gui.example.com")
    assert allowed(host="gui.example.com:8443", origin="https://gui.example.com:8443")
    assert allowed(host="gui.example.com:443", origin="https://gui.example.com")
    assert not allowed(host="gui.example.com", origin="https://gui.example.com:8443")
    assert not allowed(host="gui.example.com", origin="https://other.example.com")
    assert not allowed(host="gui.example.com")
    assert not allowed(host="gui.example.com", origin="javascript:alert(1)")


# ---------------------------------------------------------------------------
# OAuth / OIDC
# ---------------------------------------------------------------------------
def test_oauth_redirect_uses_the_configured_redirect_uri():
    provider = FakeProvider({"sub": "u"})
    oauth = OAuthSettings(
        client_id="c", client_secret="s", redirect_uri="https://gui.example.com/auth"
    )
    app = oauth_app(provider, oauth=oauth)
    response = TestClient(app).get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert provider.redirect_uri == "https://gui.example.com/auth"


def test_oauth_redirect_uri_honours_proxy_headers():
    provider = FakeProvider({"sub": "u"})
    client = TestClient(oauth_app(provider))
    headers = {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "gui.example.com",
        "X-Forwarded-Prefix": "/admin/",
    }
    client.get("/login", headers=headers, follow_redirects=False)
    assert provider.redirect_uri == "https://gui.example.com/admin/auth"
    client.get(
        "/login",
        headers={"Forwarded": 'for=1.2.3.4;proto=https;host="gui2.example.com"'},
        follow_redirects=False,
    )
    assert provider.redirect_uri == "https://gui2.example.com/auth"
    client.get(
        "/login",
        headers={"X-Forwarded-Proto": "gopher", "X-Forwarded-Host": "bad host/x"},
        follow_redirects=False,
    )
    assert provider.redirect_uri == "http://testserver/auth"


def test_oauth_login_replaces_the_previous_identity():
    provider = FakeProvider({"sub": "m1", "email": "mallory@example.com"})
    app = oauth_app(provider)
    client = TestClient(app)
    client.post("/login", json={"token": TOKEN}, headers=SAME)
    assert client.get("/api/me").json()["is_admin"] is True
    client, response = oauth_login(app, client)
    assert response.status_code == 302
    me = client.get("/api/me").json()
    assert me["auth"] == "oauth"
    assert me["user"]["email"] == "mallory@example.com"
    assert me["is_admin"] is False
    assert client.get("/api/requests").status_code == 403


def test_oauth_group_mapping():
    provider = FakeProvider(
        {"sub": "g", "email": "g@example.com", "memberOf": "staff, Admins"}
    )
    app = oauth_app(provider, admin_groups=frozenset({"admins"}))
    client, _ = oauth_login(app)
    me = client.get("/api/me").json()
    assert me["is_admin"] is True
    assert me["user"]["groups"] == ["Admins"]
    assert client.get("/api/requests").status_code == 200
    other = oauth_app(
        FakeProvider({"sub": "h", "groups": ["staff"]}),
        admin_groups=frozenset({"admins"}),
    )
    other_client, _ = oauth_login(other)
    assert other_client.get("/api/requests").status_code == 403


def test_oauth_db_role_mapping_is_rechecked_on_every_request():
    provider = FakeProvider(
        {"sub": "a", "email": "Carol@Example.com", "email_verified": True}
    )
    client, _ = oauth_login(oauth_app(provider))
    assert client.get("/api/requests").status_code == 403
    uid = db.add_user(email="carol@example.com", role="admin")
    assert client.get("/api/requests").status_code == 200
    assert client.get("/api/me").json()["role"] == "admin"
    db.set_user_role(uid, "user")
    assert client.get("/api/requests").status_code == 403
    db.set_user_role(uid, "admin")
    assert client.get("/api/requests").status_code == 200
    db.delete_user(uid)
    assert client.get("/api/requests").status_code == 403


@pytest.mark.parametrize("assume", [False, True])
@pytest.mark.parametrize(
    "claim", [{"email_verified": False}, {"email_verified": "false"}, {"verified": 0}]
)
def test_unverified_email_never_maps_to_a_db_role(claim, assume):
    db.add_user(email="root@example.com", role="admin")
    claims = {"sub": "evil", "email": "root@example.com", **claim}
    app = oauth_app(FakeProvider(claims), assume_email_verified=assume)
    client, _ = oauth_login(app)
    assert client.get("/api/requests").status_code == 403


@pytest.mark.parametrize(
    "claim",
    [{"email_verified": True}, {"email_verified": "true"}, {"verified": True}],
)
def test_verified_email_maps_to_a_db_role(claim):
    db.add_user(email="root@example.com", role="admin")
    claims = {"sub": "ok", "email": "root@example.com", **claim}
    client, _ = oauth_login(oauth_app(FakeProvider(claims)))
    assert client.get("/api/requests").status_code == 200


def test_email_without_a_verification_claim_needs_the_opt_in(caplog):
    # e.g. a plain OAuth2 userinfo whose e-mail the user typed in unverified
    db.add_user(email="boss@example.com", role="admin")
    claims = {"id": 1, "email": "boss@example.com"}
    client, _ = oauth_login(oauth_app(FakeProvider(claims), admin_token=None))
    me = client.get("/api/me").json()
    assert me["authenticated"] is True
    assert me["is_admin"] is False
    assert me["role"] is None
    assert client.get("/api/users").status_code == 403
    assert "OAUTH_ASSUME_EMAIL_VERIFIED" in caplog.text
    opted_in = oauth_app(FakeProvider(claims), assume_email_verified=True)
    client, _ = oauth_login(opted_in)
    assert client.get("/api/me").json()["role"] == "admin"
    assert client.get("/api/users").status_code == 200
    loaded = AuthSettings.from_env({"OAUTH_ASSUME_EMAIL_VERIFIED": "true"})
    assert loaded.assume_email_verified is True
    assert AuthSettings.from_env({}).assume_email_verified is False


def test_username_or_subject_never_maps_to_a_db_role():
    db.add_user(email="root@example.com", role="admin")
    for claims in (
        {"sub": "evil", "preferred_username": "root@example.com"},
        {"sub": "root@example.com"},
    ):
        client, _ = oauth_login(oauth_app(FakeProvider(claims)))
        assert client.get("/api/me").json()["authenticated"] is True
        assert client.get("/api/requests").status_code == 403


def test_userinfo_completes_the_id_token_claims():
    provider = FakeProvider(
        {"sub": "s1"},
        userinfo={"sub": "s1", "email": "u@example.com", "name": "Utente"},
    )
    oauth = OAuthSettings(
        client_id="c", client_secret="s", userinfo_url="https://idp.example.com/ui"
    )
    client, response = oauth_login(oauth_app(provider, oauth=oauth))
    assert response.status_code == 302
    assert provider.userinfo_calls == 1
    me = client.get("/api/me").json()
    assert me["user"]["email"] == "u@example.com"
    assert me["user"]["name"] == "Utente"


def test_userinfo_is_fetched_without_an_id_token_github_style():
    provider = FakeProvider(
        token={"access_token": "at"},
        userinfo={"id": 42, "login": "octo", "email": "octo@example.com"},
    )
    oauth = OAuthSettings(
        client_id="c", client_secret="s", userinfo_url="https://api.github.com/user"
    )
    client, _ = oauth_login(oauth_app(provider, oauth=oauth))
    me = client.get("/api/me").json()
    assert me["user"]["sub"] == "42"
    assert me["user"]["username"] == "octo"
    # GitHub only returns verified addresses but does not say so: opt in.
    db.add_user(email="octo@example.com", role="admin")
    assert client.get("/api/me").json()["is_admin"] is False
    app = oauth_app(provider, oauth=oauth, assume_email_verified=True)
    client, _ = oauth_login(app)
    assert client.get("/api/me").json()["is_admin"] is True


def test_userinfo_subject_mismatch_is_rejected():
    provider = FakeProvider({"sub": "s1"}, userinfo={"sub": "someone-else"})
    oauth = OAuthSettings(client_id="c", client_secret="s", userinfo_url="https://x/ui")
    client, response = oauth_login(oauth_app(provider, oauth=oauth))
    assert response.status_code == 400
    assert "non corrisponde" in response.text
    assert client.get("/api/me").json()["authenticated"] is False


@pytest.mark.parametrize(
    ("error", "status", "text"),
    [
        (OAuthError(error="access_denied"), 400, "Accesso non riuscito"),
        (MismatchingStateError(), 400, "Accesso non riuscito"),
        (httpx.ConnectError("down"), 502, "provider"),
        (RuntimeError('Missing "jwks_uri" in metadata'), 500, "OAUTH_JWKS_URI"),
        (ValueError("not json"), 502, "non valida"),
    ],
)
def test_oauth_callback_errors_render_an_error_page(error, status, text):
    client, response = oauth_login(oauth_app(FakeProvider(error=error)))
    assert response.status_code == status
    assert text in response.text
    assert "text/html" in response.headers["content-type"]
    assert response.headers["content-security-policy"] == gui.CONTENT_SECURITY_POLICY
    assert client.get("/api/me").json()["authenticated"] is False


def test_userinfo_http_error_renders_an_error_page():
    request = httpx.Request("GET", "https://idp.example.com/ui")
    error = httpx.HTTPStatusError(
        "401", request=request, response=httpx.Response(401, request=request)
    )
    provider = FakeProvider({"sub": "s"}, userinfo=error)
    oauth = OAuthSettings(client_id="c", client_secret="s", userinfo_url="https://x")
    _, response = oauth_login(oauth_app(provider, oauth=oauth))
    assert response.status_code == 502


def test_oauth_provider_timeout_renders_an_error_page():
    class Slow(FakeProvider):
        async def authorize_access_token(self, request):
            await asyncio.sleep(5)

    oauth = OAuthSettings(client_id="c", client_secret="s", timeout=0.05)
    _, response = oauth_login(oauth_app(Slow(), oauth=oauth))
    assert response.status_code == 504


def test_oauth_without_identity_is_refused():
    _, response = oauth_login(oauth_app(FakeProvider({"name": "Nobody"})))
    assert response.status_code == 400
    assert "identità" in response.text


def test_oauth_callback_without_provider():
    response = TestClient(gui.create_app(settings())).get("/auth")
    assert response.status_code == 400
    assert "OAuth" in response.text


def test_login_start_failure_renders_an_error_page():
    class Broken(FakeProvider):
        async def authorize_redirect(self, request, redirect_uri):
            raise httpx.ConnectError("no discovery")

    response = TestClient(oauth_app(Broken())).get("/login")
    assert response.status_code == 502
    assert "accedi con il token amministratore" in response.text
    assert 'href="login?method=token"' in response.text
    # without ADMIN_TOKEN there is no token login to suggest
    oauth_only = TestClient(oauth_app(Broken(), admin_token=None)).get("/login")
    assert oauth_only.status_code == 502
    assert "token" not in oauth_only.text.lower()


def test_the_token_form_is_reachable_when_oauth_is_configured():
    provider = FakeProvider({"sub": "u"})
    client = TestClient(oauth_app(provider))
    assert client.get("/login", follow_redirects=False).status_code == 302
    form = client.get("/login?method=token", follow_redirects=False)
    assert form.status_code == 200
    assert 'name="token"' in form.text
    ok = client.post(
        "/login", data={"token": TOKEN}, headers=SAME, follow_redirects=False
    )
    assert ok.status_code == 303
    assert client.get("/api/me").json()["auth"] == "token"
    oauth_only = TestClient(oauth_app(provider, admin_token=None))
    refused = oauth_only.get("/login?method=token")
    assert refused.status_code == 400
    assert 'name="token"' not in refused.text
    assert "ADMIN_TOKEN" in refused.text
    noscript = client.get("/").text.split("<noscript>", 1)[1].split("</noscript>")[0]
    assert 'href="login?method=token"' in noscript


def test_oauth_sessions_need_oauth_to_stay_enabled():
    provider = FakeProvider({"sub": "a", "email": "a@example.com", "email_verified": 1})
    db.add_user(email="a@example.com", role="admin")
    app = oauth_app(provider)
    client, _ = oauth_login(app)
    assert client.get("/api/requests").status_code == 200
    app.state.gui.oauth = SimpleNamespace()  # OAuth turned off
    assert client.get("/api/requests").status_code == 401


def test_identity_from_claims():
    identity = gui_auth.OAuthIdentity.from_claims(
        {
            "sub": "s",
            "iss": "https://idp",
            "email": " a@example.com ",
            "email_verified": "TRUE",
            "nickname": "nick",
            "groups": ["a", 3, None, {"x": 1}, " b "],
        }
    )
    assert identity.email == "a@example.com"
    assert identity.email_verified is True
    assert identity.username == "nick"
    assert identity.groups == ("a", "3", "b")
    assert gui_auth.OAuthIdentity.from_claims({"name": "only a name"}) is None
    big = gui_auth.OAuthIdentity.from_claims(
        {"sub": "s", "groups": [f"group-{i}" for i in range(500)]}
    )
    assert len(big.groups) == 200
    data = big.session_data(settings(admin_groups=frozenset({"group-7"})))
    assert data["groups"] == ["group-7"]


def test_real_authlib_oidc_flow_verifies_the_id_token_with_jwks():
    """Full OIDC flow through authlib with explicit URLs + OAUTH_JWKS_URI."""
    from authlib.integrations.starlette_client import OAuth
    from joserfc import jwt
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    issued = {}

    def handler(request):
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["code"] == ["the-code"]
            now = int(time.time())
            claims = {
                "iss": "https://idp.example.com",
                "sub": "oidc-user",
                "aud": "cid",
                "iat": now,
                "exp": now + 300,
                "nonce": issued["nonce"],
                "email": "OIDC@example.com",
                "email_verified": True,
                "groups": ["admins"],
            }
            id_token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
            return httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "token_type": "Bearer",
                    "id_token": id_token,
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=KeySet([key]).as_dict(private=False))
        if request.url.path == "/userinfo":
            assert request.headers["authorization"] == "Bearer at"
            return httpx.Response(200, json={"sub": "oidc-user", "name": "OIDC User"})
        return httpx.Response(404)

    oauth_settings = OAuthSettings(
        client_id="cid",
        client_secret="secret",
        authorize_url="https://idp.example.com/authorize",
        token_url="https://idp.example.com/token",
        userinfo_url="https://idp.example.com/userinfo",
        jwks_uri="https://idp.example.com/jwks",
        issuer="https://idp.example.com",
        redirect_uri="http://testserver/auth",
    )
    kwargs = oauth_settings.register_kwargs()
    kwargs["client_kwargs"]["transport"] = httpx.MockTransport(handler)
    registry = OAuth()
    registry.register(**kwargs)
    app = gui.create_app(
        settings(oauth=oauth_settings, admin_groups=frozenset({"admins"})),
        oauth_registry=registry,
    )
    client = TestClient(app)
    start = client.get("/login", follow_redirects=False)
    assert start.status_code == 302
    location = urlsplit(start.headers["location"])
    params = parse_qs(location.query)
    assert location.netloc == "idp.example.com"
    assert params["redirect_uri"] == ["http://testserver/auth"]
    issued["nonce"] = params["nonce"][0]
    callback = client.get(
        "/auth",
        params={"code": "the-code", "state": params["state"][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text
    me = client.get("/api/me").json()
    assert me["is_admin"] is True
    assert me["user"]["sub"] == "oidc-user"
    assert me["user"]["email"] == "OIDC@example.com"
    assert me["user"]["name"] == "OIDC User"
    # replaying the callback (state already consumed) is refused
    replay = client.get(
        "/auth", params={"code": "the-code", "state": params["state"][0]}
    )
    assert replay.status_code == 400


def test_dataclass_settings_are_immutable():
    config = settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.admin_token = "x"  # type: ignore[misc]
