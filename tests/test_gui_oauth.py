import pytest

# Skip whole module if FastAPI is not installed in the environment where tests
# are executed. This keeps CI/local runs resilient when dependencies are not
# available; CI should install requirements before running tests.
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

from src import gui


def test_config_and_login_not_configured(monkeypatch):
    # ensure provider absent for this test
    orig = dict(gui.oauth._clients)
    gui.oauth._clients.pop("provider", None)

    client = TestClient(gui.app)
    r = client.get("/config")
    assert r.status_code == 200
    data = r.json()
    assert data.get("oauth_configured") is False

    # /login should return 400 when provider not configured
    r2 = client.get("/login")
    assert r2.status_code == 400

    # restore
    gui.oauth._clients.clear()
    gui.oauth._clients.update(orig)


def test_login_and_auth_flow_sets_session(monkeypatch):
    # Register a fake provider and monkeypatch provider methods
    gui.oauth._clients["provider"] = {}

    class Provider:
        async def authorize_redirect(self, request, redirect_uri):
            return RedirectResponse(url="/auth")

        async def authorize_access_token(self, request):
            # simulate token return
            return {"access_token": "tok123"}

    monkeypatch.setattr(gui.oauth, "provider", Provider())

    client = TestClient(gui.app)

    # /login should return a redirect response (307/302)
    r = client.get("/login", allow_redirects=False)
    assert r.status_code in (302, 307)

    # Calling /auth should set the session user and redirect to '/'
    r2 = client.get("/auth", allow_redirects=False)
    assert r2.status_code in (302, 307)

    # Now /api/me should return a user in the session
    r3 = client.get("/api/me")
    assert r3.status_code == 200
    j = r3.json()
    assert "user" in j
