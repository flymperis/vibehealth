"""Host allowlist (DNS-rebinding defence) and the Sec-Fetch-Site check."""

import logging

import pytest
from fastapi.testclient import TestClient

from app import security, settings_store
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def get(client, host, path="/api/health", method="get", headers=None, **kw):
    return getattr(client, method)(path, headers={"Host": host, **(headers or {})}, **kw)


@pytest.mark.parametrize("host", [
    "192.0.2.100:5001",          # a LAN address (RFC 5737 documentation range)
    "192.0.2.100",
    "127.0.0.1:5001", "127.0.0.1", "localhost", "localhost:5001", "LOCALHOST:8000",
    "[::1]:5001", "[::1]", "[fe80::1]:8000", "[2001:db8::1]",
    "198.51.100.5:80", "203.0.113.34:5001",
    "vibehealth", "vibehealth:5001", "paperless_web:8000", "nas",
    "x.tail1234.ts.net", "my-box.tail1234.ts.net:5001",
    "printer.local", "nas.lan:5001", "router.home.arpa", "svc.internal", "a.b.c.local",
    "testserver",
    "localhost.",                   # a trailing dot is the same name
])
def test_allowed_hosts(client, host):
    assert get(client, host).status_code == 200, host


@pytest.mark.parametrize("host", [
    "evil.com", "evil.com:5001", "EVIL.COM", "health.example.com",
    "192.0.2.100.evil.com", "localhost.evil.com", "evil.com.local.evil.com", "local.evil.com",
    "evil.ts.net.evil.com", "internal.evil.com", "lan.evil.com",
    "evil.com@192.0.2.100", "192.0.2.100@evil.com", "evil.com/192.0.2.100", "a b", "evil.com:99999",
    "[::1", "evil.com:5001:80", "1.2.3", "evil.com#x",
])
def test_refused_hosts(client, host):
    r = get(client, host)
    assert r.status_code == 400, host
    assert r.json() == {"detail": "invalid host header"}
    assert "evil" not in r.text


def test_a_missing_or_empty_host_is_refused(client):
    assert client.get("/api/health", headers={"Host": ""}).status_code == 400


@pytest.mark.parametrize("method", ["get", "post", "put", "delete", "patch", "head", "options"])
def test_every_method_and_path_is_checked(client, method):
    for path in ("/api/health", "/api/documents", "/", "/assets/x.js", "/documents/1", "/api/nope"):
        assert get(client, "evil.com", path, method=method).status_code == 400, (method, path)


def test_refusal_comes_before_reads_on_an_open_instance(client):
    assert security.password_hash() == ""
    assert get(client, "evil.com", "/api/documents").status_code == 400
    assert get(client, "192.0.2.100:5001", "/api/documents").status_code == 200


def test_health_check_works_for_container_probes(client):
    for host in ("127.0.0.1:8000", "localhost:8000", "127.0.0.1", "[::1]:8000"):
        r = get(client, host)
        assert r.status_code == 200 and r.json() == {"ok": True}


def test_rebinding_style_pairs_are_refused(client):
    # the browser is on evil.com whose DNS now points at the app: Host and Origin both say evil.com
    r = get(client, "evil.com:5001", "/api/auth/logout", method="post",
            headers={"Origin": "http://evil.com:5001"})
    assert r.status_code == 400
    # a page on evil.com posting to the app by its address: Host is fine, Origin is not
    r = get(client, "192.0.2.100:5001", "/api/auth/logout", method="post",
            headers={"Origin": "http://evil.com"})
    assert r.status_code == 403
    # both ours
    r = get(client, "192.0.2.100:5001", "/api/auth/logout", method="post",
            headers={"Origin": "http://192.0.2.100:5001"})
    assert r.status_code == 200
    r = get(client, "x.tail1234.ts.net:80", "/api/auth/logout", method="post",
            headers={"Origin": "http://x.tail1234.ts.net"})
    assert r.status_code == 200


# --- configured extras ------------------------------------------------------------------------


def test_env_list_adds_hosts(client, monkeypatch):
    assert get(client, "health.example.com").status_code == 400
    monkeypatch.setenv(
        "VIBEHEALTH_ALLOWED_HOSTS",
        " Health.Example.com , other.example.org:8443,https://third.example.net/x ",
    )
    for host in ("health.example.com", "health.example.com:443", "other.example.org", "third.example.net"):
        assert get(client, host).status_code == 200, host
    assert get(client, "evil.com").status_code == 400
    assert get(client, "example.com").status_code == 400  # no wildcard by suffix


def test_the_setting_adds_hosts(client):
    settings_store.update("general", {"allowed_hosts": ["Health.example.com"]})
    assert get(client, "health.example.com:8080").status_code == 200
    assert get(client, "evil.com").status_code == 400
    settings_store.update("general", {"allowed_hosts": None})
    assert get(client, "health.example.com").status_code == 400


def test_trusted_origins_add_their_host(client):
    settings_store.update("general", {"trusted_origins": ["https://proxy.example.com:8443"]})
    assert get(client, "proxy.example.com").status_code == 200
    assert get(client, "proxy.example.com:8443").status_code == 200
    assert get(client, "other.example.com").status_code == 400


def test_a_broken_env_entry_does_not_open_anything(client, monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_ALLOWED_HOSTS", ",, a b , http://, @@@ ,")
    assert get(client, "evil.com").status_code == 400


def test_refusal_is_logged_once_a_minute(client, caplog):
    caplog.set_level(logging.WARNING, logger="vibehealth")
    security._last_host_warning = -1e9
    get(client, "evil.com")
    get(client, "evil2.com")
    warnings = [r for r in caplog.records if "refused a request with Host" in r.getMessage()]
    assert len(warnings) == 1 and "VIBEHEALTH_ALLOWED_HOSTS" in warnings[0].getMessage()


@pytest.mark.parametrize("value,expected", [
    ("Example.com:8080", "example.com"), ("[::1]:80", "::1"), ("host.", "host"), ("a.b", "a.b"),
    ("a@b", None), ("", None), (".", None), ("a:b", None), ("a b", None), ("h:65536", None), ("h:65535", "h"),
])
def test_split_host(value, expected):
    assert security.split_host(value) == expected


# --- Sec-Fetch-Site ------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_cross_site_fetch_metadata_is_refused_for_state_changes(client, method):
    r = client.request(method, "/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and r.json() == {"detail": "cross-origin request refused"}


@pytest.mark.parametrize("value", ["same-origin", "same-site", "none", ""])
def test_other_fetch_metadata_values_pass(client, value):
    assert client.post("/api/auth/logout", headers={"Sec-Fetch-Site": value}).status_code == 200


def test_cross_site_reads_are_not_blocked_by_the_fetch_check(client):
    assert client.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_cross_site_state_change_never_reaches_the_handler(client):
    r = client.put("/api/settings/general", json={"language": "el"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert client.get("/api/settings/general").json()["values"]["language"] == "en"
