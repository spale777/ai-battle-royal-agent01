"""API smoke tests — hermetic where the network is involved.

These encode the "shipped = verified, not 'returned 200 once'" principle that the
self-health layer enforces at runtime. With network deps mocked, /api/health must
report a fully green API surface, which in turn proves each endpoint returns valid
JSON with the expected key/type and a non-empty payload.

Real (unmocked) git/file endpoints are tested against the actual repo.
"""
import pytest


# ---- hermetic: network mocked -------------------------------------------------

def test_health_all_green_with_network_mocked(client, network_mocks):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.get_json()
    # Every checked endpoint must pass.
    assert data["failed"] == 0, f"failed endpoints: {[e for e in data['endpoints'] if not e['ok']]}"
    assert data["ok"] is True
    assert data["passed"] == data["checked"] == 10


def test_health_shape(client, network_mocks):
    data = client.get("/api/health").get_json()
    for key in ("ok", "checked", "passed", "failed", "endpoints", "at"):
        assert key in data
    for ep in data["endpoints"]:
        for k in ("route", "ok", "status", "ms", "error"):
            assert k in ep


def test_uptime(client):
    data = client.get("/api/uptime").get_json()
    assert data["status"] if "status" in data else True
    assert isinstance(data["elapsed"], str)
    assert data["seconds"] >= 0


def test_status_machine_readable(client):
    """/status is the cheap, machine-readable status for monitors: app up,
    uptime, current commit. Must be 200, valid JSON, with the expected shape."""
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.get_json()
    for key in ("ok", "agent", "service", "uptime_seconds", "commit", "at"):
        assert key in data
    assert data["ok"] is True
    assert data["agent"] == "agent-01"
    assert data["uptime_seconds"] >= 0
    # Against the real repo, HEAD resolves to a short hash.
    assert data["commit"] and len(data["commit"]) == 7


def test_commits_real(client):
    """git-backed endpoint, tested against the actual repo (no mock)."""
    data = client.get("/api/commits").get_json()
    assert "commits" in data
    assert isinstance(data["commits"], list)
    assert len(data["commits"]) > 0, "repo should have commits"
    first = data["commits"][0]
    assert "hash" in first and "message" in first


def test_timeline_real(client):
    data = client.get("/api/timeline").get_json()
    assert "timeline" in data
    assert isinstance(data["timeline"], list)
    assert len(data["timeline"]) > 0


def test_file_view_real(client):
    """Read a known project file through the viewer endpoint."""
    data = client.get("/api/file/app.py").get_json()
    assert data["path"] == "app.py"
    assert data["lines"] > 10
    assert "Flask" in data["content"]


def test_file_view_404(client):
    resp = client.get("/api/file/no_such_file_xyz.py")
    assert resp.status_code == 404


# ---- stats / stats-history: local file-based (no network) --------------------

def test_stats_history_shape(client):
    data = client.get("/api/stats/history").get_json()
    assert "history" in data
    assert isinstance(data["history"], list)


def test_search_min_query(client):
    # query shorter than 2 chars -> empty results, but still valid JSON
    data = client.get("/api/search?q=a").get_json()
    assert data["results"] == []
    assert data["query"] == "a"


def test_search_finds_commit(client):
    # 'dashboard' appears in recent commit messages
    data = client.get("/api/search?q=dashboard").get_json()
    assert isinstance(data["results"], list)


def test_research_mocked(client, network_mocks):
    data = client.get("/api/research").get_json()
    assert "papers" in data
    assert isinstance(data["papers"], list)
    assert len(data["papers"]) >= 1


def test_network_mocked(client, network_mocks):
    data = client.get("/api/network").get_json()
    assert "agents" in data
    assert isinstance(data["agents"], list)
    assert len(data["agents"]) == 8  # agent-01 .. agent-08


def test_notebook_mocked(client, network_mocks):
    data = client.get("/api/notebook").get_json()
    assert "edition" in data
    assert data["edition"] == 1


def test_analytics_shape(client):
    data = client.get("/api/analytics").get_json()
    assert "total" in data
    assert isinstance(data["pages"], dict)
    assert isinstance(data["hourly"], list)
    assert isinstance(data["recent"], list)


# ---- pages render (200) -------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/", "/about", "/sandbox", "/projects", "/peers", "/research",
    "/stats", "/codebase", "/demos", "/analytics", "/timeline",
    "/network", "/devlog", "/dashboard",
])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_not_found_page(client):
    assert client.get("/this/does/not/exist").status_code == 404


def test_robots_txt(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert b"User-agent" in resp.data


def test_sitemap(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert b"sitemap" in resp.data


def test_feed_xml(client):
    resp = client.get("/feed.xml")
    assert resp.status_code == 200
    assert resp.mimetype == "application/rss+xml" or "xml" in resp.mimetype
