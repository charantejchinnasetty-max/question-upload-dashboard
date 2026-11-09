"""Login tests: mobile number + shared password from APP_PASSWORD."""
import importlib

import pytest


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "test-pass-123")
    monkeypatch.delenv("ALLOWED_MOBILES", raising=False)
    import auth, app as appmod
    importlib.reload(auth); importlib.reload(appmod)
    auth._attempts.clear()
    return appmod.app.test_client()


def login(c, mobile, pw):
    return c.post("/login", data={"mobile": mobile, "password": pw, "next": "/student-ai-agent"})


def test_pages_and_api_require_login(client):
    r = client.get("/student-ai-agent")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert client.get("/api/students/agent/status").status_code == 401
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/login").status_code == 200


def test_valid_login_redirects_and_logout(client):
    r = login(client, "+91 98765 43210", "test-pass-123")
    assert r.status_code == 302 and r.headers["Location"].endswith("/student-ai-agent")
    assert client.get("/api/students/agent/status").status_code == 200
    client.get("/logout")
    assert client.get("/api/students/agent/status").status_code == 401


def test_wrong_password_and_bad_mobile_rejected(client):
    assert login(client, "9876543210", "wrong").status_code == 401
    assert login(client, "12345", "test-pass-123").status_code == 401


def test_allowlist(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_MOBILES", "9876543210, 9000000001")
    assert login(client, "9123456789", "test-pass-123").status_code == 401
    assert login(client, "09876543210", "test-pass-123").status_code == 302


def test_throttle_after_repeated_failures(client):
    for _ in range(8):
        login(client, "9876543210", "nope")
    assert login(client, "9876543210", "test-pass-123").status_code == 429


def test_open_redirect_blocked(client):
    r = client.post("/login", data={"mobile": "9876543210", "password": "test-pass-123", "next": "//evil.example"})
    assert r.headers["Location"] == "/"


def test_locked_when_password_not_configured(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    import auth, app as appmod
    importlib.reload(auth); importlib.reload(appmod)
    c = appmod.app.test_client()
    assert login(c, "9876543210", "anything").status_code == 503
