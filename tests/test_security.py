"""Security hardening tests: browser-security headers, the strict CSP, the
CSP report sink, and the /api/security self-observability endpoint.

These encode the "shipped = verified" principle for the security layer: the
headers must actually be present on every response class (page, API, 404), the
CSP must be the strict one (and, crucially, must NOT include 'unsafe-eval'),
and the report sink must log without ever returning 5xx.
"""
import json


def test_security_headers_on_page(client):
    """Every browser-security header + CSP present on an HTML page."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in resp.headers
    assert resp.headers.get("Content-Security-Policy")


def test_security_headers_on_api(client):
    """API responses get the same headers (not just HTML pages)."""
    resp = client.get("/api/uptime")
    assert resp.status_code == 200
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in resp.headers


def test_security_headers_on_404(client):
    """Error pages (handled by errorhandler) also carry the headers."""
    resp = client.get("/this/does/not/exist")
    assert resp.status_code == 404
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in resp.headers


def test_csp_is_strict_and_no_unsafe_eval(client, app_module):
    """The shipped CSP is strict and does NOT permit eval / new Function.
    (The codebase was audited: no eval(), no new Function, no dynamic
    <script> injection — so 'unsafe-eval' would be unnecessary risk.)"""
    csp = client.get("/").headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "connect-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "report-uri /csp-report" in csp
    # The important negative assertion:
    assert "unsafe-eval" not in csp
    # The module constant the header is built from is the single source of truth:
    assert "unsafe-eval" not in app_module.CONTENT_SECURITY_POLICY


def test_api_security_shape(client):
    """/api/security exposes the active posture + violation count."""
    data = client.get("/api/security").get_json()
    for key in ("ok", "headers", "content_security_policy",
                "csp_violations_logged", "report_endpoint", "at"):
        assert key in data
    assert data["ok"] is True
    assert data["report_endpoint"] == "/csp-report"
    assert data["headers"]["X-Content-Type-Options"] == "nosniff"
    assert isinstance(data["csp_violations_logged"], int)


def test_csp_report_logs_and_returns_204(client, monkeypatch, app_module, tmp_path):
    """A POST violation report is accepted (204) and increments the logged count."""
    log = tmp_path / "csp_reports.jsonl"
    monkeypatch.setattr(app_module, "CSP_REPORT_PATH", log)
    assert app_module.get_csp_report_count() == 0

    violation = {"csp-report": {
        "document-uri": "https://agent-01.sklopocija.com/",
        "violated-directive": "script-src",
        "blocked-uri": "https://evil.example/track.js",
    }}
    resp = client.post("/csp-report", json=violation)
    assert resp.status_code == 204

    assert app_module.get_csp_report_count() == 1
    line = log.read_text().strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["csp-report"]["violated-directive"] == "script-src"
    # surfaced through /api/security
    assert client.get("/api/security").get_json()["csp_violations_logged"] == 1


def test_csp_report_malformed_returns_204(client, monkeypatch, app_module, tmp_path):
    """A non-JSON / malformed body must still return 204, never 5xx, and be logged."""
    log = tmp_path / "csp_reports.jsonl"
    monkeypatch.setattr(app_module, "CSP_REPORT_PATH", log)
    resp = client.post("/csp-report", data=b"not json at all",
                       content_type="text/plain")
    assert resp.status_code == 204
    assert app_module.get_csp_report_count() == 1
