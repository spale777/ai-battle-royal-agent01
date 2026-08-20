"""Hermetic tests for the egress self-check (research.check_egress + /api/egress).

These encode the principle that a SILENT failure mode (the service can't reach
the internet so internet features quietly fall back to cache) must become VISIBLE
data. The probe's network access is injected as a callable so no real network
call happens in tests.
"""
import socket
import urllib.error

import pytest

import research


def _dns_failure():
    """Build the exact error urllib raises when a name won't resolve (the proxy
    -missing signature on this box)."""
    return urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))


# ---- pure check_egress (fetcher injected) ------------------------------------

def test_egress_success():
    res = research.check_egress(fetcher=lambda url, t: 204)
    assert res["reachable"] is True
    assert res["status"] == 204
    assert res["message"].startswith("Outbound HTTPS via urllib OK")


def test_egress_http_error_is_reachable():
    # A real server answering non-2xx still proves egress works (name resolved,
    # a proxy responded) — so it counts as reachable, with the status surfaced.
    res = research.check_egress(fetcher=lambda url, t: (_ for _ in ()).throw(
        urllib.error.HTTPError("u", 404, "not found", None, None)))
    assert res["reachable"] is True
    assert res["status"] == 404


def test_egress_dns_failure_no_proxy_is_pointed(monkeypatch):
    # The exact regression this box is prone to: name won't resolve AND no proxy
    # env vars -> the message must name the cause, not just dump the error.
    # Clear any ambient proxy vars so the "no proxy" branch is deterministic
    # regardless of the environment the tests run in (this box uses a proxy).
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    def fetch(url, t):
        raise _dns_failure()
    res = research.check_egress(fetcher=fetch)
    assert res["reachable"] is False
    assert res["status"] is None
    assert res["proxy_configured"] is False
    assert "proxy" in res["message"].lower()
    assert "http_proxy" in res["message"]
    assert "cache fallback" in res["message"]


def test_egress_dns_failure_with_proxy_is_generic(monkeypatch):
    # If a proxy IS configured but the probe still fails, the cause is probably
    # the proxy itself, not a missing env var — so the message stays generic.
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:3128")
    def fetch(url, t):
        raise _dns_failure()
    res = research.check_egress(fetcher=fetch)
    assert res["reachable"] is False
    assert res["proxy_configured"] is True
    assert "http_proxy/https_proxy to the service" not in res["message"]


def test_egress_non_dns_failure_is_generic():
    def fetch(url, t):
        raise urllib.error.URLError("connection refused")
    res = research.check_egress(fetcher=fetch)
    assert res["reachable"] is False
    assert res["message"].startswith("Outbound HTTPS probe failed")
    assert "connection refused" in res["message"]


def test_egress_proxy_configured_detection(monkeypatch):
    # proxy_configured reflects the process env, so it must be observable data.
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    assert research.check_egress(fetcher=lambda url, t: 204)["proxy_configured"] is False

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:3128")
    assert research.check_egress(fetcher=lambda url, t: 204)["proxy_configured"] is True


def test_egress_default_probe_exists_and_callable():
    # The default probe is the real urllib path; we only check it's wired so a
    # regression in the default (not the injected) path is caught at import time.
    assert callable(research._egress_default_probe)
    assert isinstance(research.EGRESS_PROBE_URL, str)
    assert research.EGRESS_PROBE_URL.startswith("https://")


# ---- endpoint / health wiring -------------------------------------------------

def test_api_egress_shape(client, network_mocks):
    resp = client.get("/api/egress")
    assert resp.status_code == 200
    data = resp.get_json()
    for key in ("reachable", "url", "status", "proxy_configured", "message"):
        assert key in data
    assert data["reachable"] is True  # mocked reachable in network_mocks


def test_egress_is_part_of_health(client, network_mocks):
    # The whole point: egress is a first-class, self-verified endpoint. If the
    # service loses its internet path, /api/health goes red and names it.
    data = client.get("/api/health").get_json()
    routes = [e["route"] for e in data["endpoints"]]
    assert any(r == "/api/egress" for r in routes)
    egress = next(e for e in data["endpoints"] if e["route"] == "/api/egress")
    assert egress["ok"] is True
