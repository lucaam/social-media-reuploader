"""OAuth login flow against the module-level app (``src.gui.app``).

These tests keep the original monkeypatching approach: a fake provider client
is injected on ``gui.oauth`` (the app's authlib registry).
"""

import dataclasses

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

from src import db as _db
from src import gui


@pytest.fixture(autouse=True)
def init_db(tmp_path, monkeypatch):
    # Each test gets its own database file.
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "requests.db"))
    _db.init_db()


def _provider(userinfo):
    class Provider:
        async def authorize_redirect(self, request, redirect_uri):
            return RedirectResponse(url="/auth")

        async def authorize_access_token(self, request):
            # simulate token return with embedded (ID-token) userinfo
            return {"access_token": "tok123", "userinfo": dict(userinfo)}

    return Provider()


def _login(monkeypatch, userinfo):
    monkeypatch.setattr(gui.oauth, "provider", _provider(userinfo), raising=False)
    client = TestClient(gui.app)
    r = client.get("/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    r2 = client.get("/auth", follow_redirects=False)
    assert r2.status_code in (302, 307)
    return client


def test_config_and_login_not_configured(monkeypatch):
    # no provider client and (in the test environment) no ADMIN_TOKEN
    monkeypatch.delattr(gui.oauth, "provider", raising=False)
    client = TestClient(gui.app)
    r = client.get("/config")
    assert r.status_code == 200
    data = r.json()
    assert data.get("oauth_configured") is False
    assert set(data) == {"oauth_configured", "admin_token_set"}

    # /login should return 400 when no login method is configured
    if not gui.app.state.gui.settings.token_enabled:
        r2 = client.get("/login")
        assert r2.status_code == 400


def test_login_and_auth_flow_sets_session(monkeypatch):
    client = _login(
        monkeypatch,
        {"sub": "u1", "email": "me@example.com", "preferred_username": "tester"},
    )
    # Now /api/me should return a user in the session
    r3 = client.get("/api/me")
    assert r3.status_code == 200
    j = r3.json()
    assert j["authenticated"] is True
    assert j["auth"] == "oauth"
    assert j["user"]["email"] == "me@example.com"
    assert j["user"]["username"] == "tester"
    assert j["is_admin"] is False


def test_db_role_grants_and_revokes_admin_without_grant_endpoint(monkeypatch):
    client = _login(
        monkeypatch, {"sub": "u2", "email": "u2@example.com", "email_verified": True}
    )

    # not an admin yet: the API is forbidden and /api/me agrees
    assert client.get("/requests").status_code == 403
    assert client.get("/api/me").json()["is_admin"] is False
    # the old self-grant endpoint is gone (admin is derived on every request)
    bearer_free = client.post(
        "/api/session/grant_admin", headers={"Origin": "http://testserver"}
    )
    assert bearer_free.status_code == 404

    # add persistent admin mapping in DB for this user's email (case-insensitive)
    uid = _db.add_user(username="u2", email="U2@Example.com", role="admin")
    r3 = client.get("/requests")
    assert r3.status_code == 200
    assert "items" in r3.json()
    assert client.get("/api/me").json()["is_admin"] is True

    # demotion takes effect on the next request (no cached flag in the cookie)
    _db.set_user_role(uid, "user")
    assert client.get("/requests").status_code == 403
    assert client.get("/api/me").json()["is_admin"] is False


def test_oauth_group_auto_admin(monkeypatch):
    settings = gui.app.state.gui.settings
    monkeypatch.setattr(
        gui.app.state.gui,
        "settings",
        dataclasses.replace(settings, admin_groups=frozenset({"admins"})),
    )
    client = _login(
        monkeypatch,
        {"sub": "g1", "email": "g1@example.com", "groups": ["Admins", "staff"]},
    )
    r = client.get("/api/me")
    assert r.status_code == 200
    j = r.json()
    assert j.get("is_admin") is True
    # only the groups that grant admin are kept in the session cookie
    assert j["user"]["groups"] == ["Admins"]

    # requests endpoint should be accessible
    r2 = client.get("/requests")
    assert r2.status_code == 200

    # removing the group from the configuration revokes admin immediately
    monkeypatch.setattr(
        gui.app.state.gui,
        "settings",
        dataclasses.replace(settings, admin_groups=frozenset({"other"})),
    )
    assert client.get("/requests").status_code == 403
