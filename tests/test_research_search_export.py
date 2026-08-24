"""Tests for the live arXiv *search* result export (BibTeX / CSV / JSON).

Closes the gap the digest export already covered: a visitor who runs a live
search now gets "export what you see" — the SAME paper set the UI rendered, in
the same formats, from the same research.bibtex() single source of truth.

Hermetic: the endpoint calls research.search(), which (on the live path) calls
research.search_arxiv_live(); we monkeypatch the latter so no real network
happens. We also drive the *pure* path directly (export_papers over a known
paper list) to lock the serialization contract without any endpoint.
"""
import csv
import io
import json

import research

# A fixed, deterministic search result set (mirrors the shape _parse_entries
# produces). Two papers, with a comma/quote title and a newline summary so the
# CSV quoting + newline-fold paths are exercised end-to-end.
SEARCH_PAPERS = [
    {
        "arxiv_id": "2608.10001",
        "title": 'Diffusion "models", a survey',
        "published": "2026-08-01",
        "authors": "Ada A, Ben B",
        "author_names": ["Ada A", "Ben B"],
        "summary": "A survey of diffusion.",
        "categories": ["cs.LG", "cs.CV"],
        "primary_category": "cs.LG",
    },
    {
        "arxiv_id": "2608.10002",
        "title": "Multiline abstract",
        "published": "2026-08-02",
        "authors": "Cara C",
        "author_names": ["Cara C"],
        "summary": "Line one\nline two, with a comma.",
        "categories": ["cs.CV"],
        "primary_category": "cs.CV",
    },
]


def _client_search(monkeypatch, app_module, papers=SEARCH_PAPERS, total=None,
                   raise_on_live=False):
    """A test client whose live search is stubbed to return `papers` (total
    hits = `total`). If raise_on_live, the live fetch raises so research.search
    exercises its cache fallback (needs a digest too)."""
    import research as research_module
    if raise_on_live:
        def _boom(*a, **k):
            raise ConnectionError("arXiv unreachable (test)")
        monkeypatch.setattr(research_module, "search_arxiv_live", _boom)
        # A digest to fall back onto for the cache path.
        monkeypatch.setattr(
            research_module, "get_research_digest",
            lambda: {"cached_at": "2026-01-01T00:00:00+00:00",
                     "papers": list(papers), "total": len(papers), "stale": False,
                     "categories": {"cs.LG": 1, "cs.CV": 1}, "new_count": 0,
                     "new_id_list": [], "current_ids": [p["arxiv_id"] for p in papers]},
        )
    else:
        monkeypatch.setattr(
            research_module, "search_arxiv_live",
            lambda *a, **k: (list(papers), total if total is not None else len(papers)),
        )
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# --- The endpoint re-runs the SAME search and exports what you see -----------

def test_search_export_exports_exactly_the_searched_papers(monkeypatch, app_module):
    c = _client_search(monkeypatch, app_module)
    r = c.get("/api/research/search/export?q=diffusion&format=json")
    assert r.status_code == 200
    data = r.get_json()
    ids = [p["arxiv_id"] for p in data["papers"]]
    # Export == the papers the search returned (in order), nothing more.
    assert ids == [p["arxiv_id"] for p in SEARCH_PAPERS]


def test_search_export_bibtex_is_source_of_truth(monkeypatch, app_module):
    c = _client_search(monkeypatch, app_module)
    r = c.get("/api/research/search/export?q=diffusion&format=bibtex")
    assert r.status_code == 200
    assert r.content_type.startswith("application/x-bibtex")
    body = r.get_data(as_text=True)
    # One @article per searched paper, and it is EXACTLY the per-paper bibtex()
    # strings joined — the same source the per-paper copy buttons use.
    assert body.count("@article") == len(SEARCH_PAPERS)
    assert body == "\n".join(research.bibtex(p) for p in SEARCH_PAPERS)


def test_search_export_csv_roundtrips(monkeypatch, app_module):
    c = _client_search(monkeypatch, app_module)
    r = c.get("/api/research/search/export?q=diffusion&format=csv")
    assert r.status_code == 200
    assert r.content_type.startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert len(rows) == 2
    # Quoted+comma title survives a real CSV round-trip.
    assert rows[0]["title"] == 'Diffusion "models", a survey'
    # Newline in the abstract is folded to a space.
    assert "\n" not in rows[1]["abstract"]
    assert "Line one line two, with a comma." == rows[1]["abstract"]


def test_search_export_reflects_query_in_filename(monkeypatch, app_module):
    c = _client_search(monkeypatch, app_module)
    r = c.get("/api/research/search/export?q=diffusion+models&format=bibtex")
    assert r.status_code == 200
    cd = r.headers["Content-Disposition"]
    assert "arxiv-search-diffusion-models-p1.bib" in cd
    # Page is reflected too.
    r2 = c.get("/api/research/search/export?q=diffusion&format=json&page=3")
    assert "arxiv-search-diffusion-p3.json" in r2.headers["Content-Disposition"]


def test_search_export_honors_field_and_sort(monkeypatch, app_module):
    """The endpoint must pass field/sort through to the search (the UI sends
    them). We stub the live fetch to record what it received."""
    import research as research_module
    seen = {}

    def _live(query, max_results=10, field="all", sort="relevance", start=0):
        seen["query"], seen["field"], seen["sort"] = query, field, sort
        return (list(SEARCH_PAPERS), len(SEARCH_PAPERS))

    monkeypatch.setattr(research_module, "search_arxiv_live", _live)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as tc:
        tc.get("/api/research/search/export?q=protein&format=json&field=ti&sort=date")
    assert seen["query"] == "protein"
    assert seen["field"] == "ti"
    assert seen["sort"] == "date"


def test_search_export_invalid_format_400(monkeypatch, app_module):
    c = _client_search(monkeypatch, app_module)
    r = c.get("/api/research/search/export?q=diffusion&format=xml")
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"]
    assert set(body["formats"]) == {"bibtex", "csv", "json"}


def test_search_export_missing_query_400(monkeypatch, app_module):
    c = _client_search(monkeypatch, app_module)
    assert c.get("/api/research/search/export").status_code == 400
    # Too-short query is also a 400 (min 2 chars), not a live fetch.
    assert c.get("/api/research/search/export?q=a").status_code == 400


def test_search_export_max_clamped_to_cap(monkeypatch, app_module):
    """max is clamped to arXiv's per-page cap (30) — a single export can't
    request an unbounded page. We record the max_results the live fetch saw."""
    import research as research_module
    seen = {}

    def _live(query, max_results=10, field="all", sort="relevance", start=0):
        seen["max_results"] = max_results
        return (list(SEARCH_PAPERS), len(SEARCH_PAPERS))

    monkeypatch.setattr(research_module, "search_arxiv_live", _live)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as tc:
        tc.get("/api/research/search/export?q=diffusion&format=json&max=9999")
    assert seen["max_results"] == research.MAX_RESULTS_CAP


def test_search_export_cache_fallback_is_honest(monkeypatch, app_module):
    """When the live fetch fails, research.search degrades to a LOCAL substring
    search over the cached digest (source='cache'). The export should produce a
    valid file from exactly the papers that local search matched — here q only
    matches the 'diffusion' paper, so the export contains just that one. The
    JSON variant reports a paper list, so /api/health stays green on fallback."""
    c = _client_search(monkeypatch, app_module, raise_on_live=True)
    r = c.get("/api/research/search/export?q=diffusion&format=json")
    assert r.status_code == 200
    data = r.get_json()
    # Only the digest paper that actually contains "diffusion" is in the
    # fallback result — the export faithfully serializes the fallback set.
    assert [p["arxiv_id"] for p in data["papers"]] == ["2608.10001"]


def test_search_export_empty_result_is_valid_empty(monkeypatch, app_module):
    """A live search with zero hits exports an empty (but valid) set, not an
    error — consistent with how the search endpoint reports a real zero."""
    c = _client_search(monkeypatch, app_module, papers=[], total=0)
    r = c.get("/api/research/search/export?q=zzzzz&format=json")
    assert r.status_code == 200
    assert r.get_json()["count"] == 0
    # CSV with no rows still has a header.
    rc = c.get("/api/research/search/export?q=zzzzz&format=csv")
    assert research._CSV_COLUMNS[0] in rc.get_data(as_text=True)


# --- Cross-cutting: security headers + health wiring + page wiring -----------

def test_search_export_security_headers(monkeypatch, app_module):
    c = _client_search(monkeypatch, app_module)
    for fmt in ("bibtex", "csv", "json"):
        r = c.get(f"/api/research/search/export?q=diffusion&format={fmt}")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert "Content-Security-Policy" in r.headers
        assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"


def test_health_includes_search_export_check(app_module):
    assert any("api/research/search/export" in route
               for route, *_ in app_module._API_CHECKS)


def test_research_page_search_export_wiring(client, network_mocks):
    r = client.get("/research")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # The search-results export bar container is present (built by JS on a
    # result, hidden otherwise).
    assert 'id="arxiv-search-export"' in html
    # The JS that builds it targets the new endpoint.
    assert "renderSearchExport" in html
    assert "/api/research/search/export?" in html
