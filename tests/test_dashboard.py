"""Dashboard regression tests.

Two real defects lived here:

* `GET /` returned 500 because the route used the pre-0.55 Starlette
  `TemplateResponse("dashboard.html", context)` signature, which no longer
  exists. Jinja2 now raises at render time, so the page was dead on arrival.
* `/api/stats` exposed customer tier counts and raw `bot.log` lines with no
  authorization at all. Binding to localhost is not an authorization control:
  any local process can reach it and a browser can be pointed at it via DNS
  rebinding.

Both are invisible to the import smoke tests, so they get their own file.
"""
import importlib
import sys
import warnings

import pytest

# Starlette/FastAPI deprecation noise is not what these tests are about.
warnings.filterwarnings("ignore")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient over a freshly reloaded dashboard with auth enabled.

    DASHBOARD_TOKEN and the template/static paths are resolved at import time,
    so the module has to be reloaded for the env changes to take effect.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/dash.db")
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token-abcdef123456")

    for name in ("dashboard", "database"):
        sys.modules.pop(name, None)
    dashboard = importlib.import_module("dashboard")

    from fastapi.testclient import TestClient
    yield dashboard, TestClient(dashboard.app)

    # Leave the session with the normal, un-reloaded modules.
    for name in ("dashboard", "database"):
        sys.modules.pop(name, None)


def test_index_renders(client):
    """GET / must return 200, not the Starlette-signature 500."""
    _, c = client
    resp = c.get("/", headers={"x-dashboard-token": "test-token-abcdef123456"})
    assert resp.status_code == 200, resp.text
    assert "<html" in resp.text.lower()


def test_api_stats_renders(client):
    """The AJAX endpoint must work with auth too, not just 401."""
    _, c = client
    resp = c.get("/api/stats", headers={"x-dashboard-token": "test-token-abcdef123456"})
    assert resp.status_code == 200, resp.text
    assert "user_stats" in resp.json()


def test_index_requires_token(client):
    """No credential, no page."""
    _, c = client
    assert c.get("/").status_code == 401
    assert c.get("/").status_code == 401


def test_api_stats_requires_token(client):
    """The stats endpoint was the unauthenticated one."""
    _, c = client
    assert c.get("/api/stats").status_code == 401


def test_wrong_token_is_rejected(client):
    _, c = client
    resp = c.get("/", headers={"x-dashboard-token": "not-the-token"})
    assert resp.status_code == 401


def test_wrong_token_via_query_is_rejected(client):
    """The ?token= form must be checked too, not silently accepted."""
    _, c = client
    assert c.get("/?token=guess").status_code == 401


def test_token_accepted_via_query(client):
    """The ?token= form is the documented convenience path."""
    _, c = client
    resp = c.get("/?token=test-token-abcdef123456")
    assert resp.status_code == 200, resp.text


def test_refuses_to_start_without_token(monkeypatch):
    """`python dashboard.py` must fail closed rather than serve unauthenticated."""
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    sys.modules.pop("dashboard", None)
    dashboard = importlib.import_module("dashboard")
    monkeypatch.setattr(dashboard, "DASHBOARD_TOKEN", "", raising=False)
    with pytest.raises(SystemExit) as exc:
        dashboard.main()
    assert "DASHBOARD_TOKEN" in str(exc.value)
    sys.modules.pop("dashboard", None)