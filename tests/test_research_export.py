"""Tests for the research-digest export (BibTeX / CSV / JSON).

Covers the pure serializer (research.export_papers) and the /api/research/export
endpoint. Hermetic: the endpoint tests monkeypatch the digest (the route imports
get_research_digest from the research module inside the handler, so the monkeypatch
on research_module is picked up at call time).
"""
import csv
import io
import json

import pytest


# --- A small, fixed paper list with deliberate edge cases ---------------------
# Paper 1: an " and a , in the title (exercises CSV quoting).
# Paper 2: a real newline in the summary (exercises the newline-fold in CSV).
# Paper 3: a unicode author + title (exercises ensure_ascii=False in JSON).
PAPERS = [
    {
        "arxiv_id": "2601.00001",
        "title": 'Attention "heads", revisited',
        "published": "2026-01-01",
        "authors": "Ada A, Grace H",
        "author_names": ["Ada A", "Grace H"],
        "summary": "A study of attention.",
        "categories": ["cs.AI", "cs.LG"],
        "primary_category": "cs.AI",
    },
    {
        "arxiv_id": "2601.00002",
        "title": "Multiline abstract",
        "published": "2026-01-02",
        "authors": "Ben B",
        "author_names": ["Ben B"],
        "summary": "Line one\nline two, with a comma.",
        "categories": ["cs.CL"],
        "primary_category": "cs.CL",
    },
    {
        "arxiv_id": "2601.00003",
        "title": "Café learning",
        "published": "2026-01-03",
        "authors": "Zoë Z",
        "author_names": ["Zoë Z"],
        "summary": "Unicode everywhere.",
        "categories": ["cs.CV"],
        "primary_category": "cs.CV",
    },
]


# --- research.export_papers (pure) -------------------------------------------

def test_export_bibtex_is_bibtex_source_of_truth():
    import research
    out = research.export_papers(PAPERS, fmt="bibtex")
    # One @article per paper, in order.
    assert out.count("@article") == len(PAPERS)
    assert "2601.00001" in out and "2601.00003" in out
    # It is EXACTLY the per-paper bibtex() strings joined — the same source the
    # "copy citation" button uses, so what you download == what you copy.
    expected = "\n".join(research.bibtex(p) for p in PAPERS)
    assert out == expected


def test_export_csv_shape_and_roundtrip():
    import research
    out = research.export_papers(PAPERS, fmt="csv")
    reader = csv.DictReader(io.StringIO(out))
    assert reader.fieldnames == list(research._CSV_COLUMNS)
    rows = list(reader)
    assert len(rows) == 3
    assert rows[0]["arxiv_id"] == "2601.00001"
    assert rows[0]["url"] == "https://arxiv.org/abs/2601.00001"
    assert rows[0]["doi"] == "10.48550/arXiv.2601.00001"
    # A quoted+comma title survives a real CSV round-trip (RFC-4180 quoting).
    assert rows[0]["title"] == 'Attention "heads", revisited'
    # Newlines in the abstract are folded to spaces.
    assert "\n" not in rows[1]["abstract"]
    assert "Line one line two, with a comma." == rows[1]["abstract"]


def test_export_csv_quotes_fields_needing_it():
    import research
    out = research.export_papers(PAPERS, fmt="csv")
    first_row = out.splitlines()[1]
    # The id has no comma, so it is left unquoted (csv only quotes when needed).
    assert first_row.startswith("2601.00001,")
    # The comma/quote title is wrapped in double quotes, inner quotes doubled.
    assert 'Attention ""heads"", revisited' in first_row
    # The comma-in-authors field is also quoted.
    assert '"Ada A, Grace H"' in first_row


def test_export_json_shape_and_unicode():
    import research
    out = research.export_papers(PAPERS, fmt="json")
    data = json.loads(out)
    assert data["count"] == 3
    assert data["format"] == "json"
    assert len(data["papers"]) == 3
    # Curated field set only — no internal keys leak.
    keys = set(data["papers"][0].keys())
    assert keys == {"arxiv_id", "title", "published", "authors", "author_names",
                    "primary_category", "categories", "url", "doi", "abstract"}
    assert "bibtex" not in keys and "summary" not in keys
    # ensure_ascii=False keeps unicode readable (not \u-escaped).
    assert "Café" in out and "Zoë" in out
    assert data["papers"][2]["url"] == "https://arxiv.org/abs/2601.00003"


def test_export_category_filter_matches_page_filter_semantics():
    import research
    # Primary match, case-insensitive.
    ai = research.export_papers(PAPERS, fmt="json", cat="cs.AI")
    assert json.loads(ai)["count"] == 1
    # Secondary match: a non-primary listed category still matches.
    lg = research.export_papers(PAPERS, fmt="json", cat="cs.LG")
    assert json.loads(lg)["count"] == 1
    assert json.loads(lg)["papers"][0]["arxiv_id"] == "2601.00001"
    # Unknown category -> empty (not an error).
    assert json.loads(research.export_papers(PAPERS, fmt="json", cat="quant-ph"))["count"] == 0


def test_export_invalid_format_raises():
    import research
    with pytest.raises(ValueError):
        research.export_papers(PAPERS, fmt="xml")
    with pytest.raises(ValueError):
        research.export_papers(PAPERS, fmt="")


def test_export_missing_fields_degrade_not_crash():
    import research
    sparse = [{"arxiv_id": "9999.99999"}]  # everything else missing
    bib = research.export_papers(sparse, fmt="bibtex")
    assert "9999.99999" in bib
    csv_out = research.export_papers(sparse, fmt="csv")
    assert "9999.99999" in csv_out
    data = json.loads(research.export_papers(sparse, fmt="json"))
    assert data["count"] == 1
    assert data["papers"][0]["title"] == ""


def test_export_empty_list():
    import research
    assert json.loads(research.export_papers([], fmt="json"))["count"] == 0
    # CSV with no rows still has a header.
    assert research._CSV_COLUMNS[0] in research.export_papers([], fmt="csv")


# --- /api/research/export endpoint -------------------------------------------

def _client_with_digest(monkeypatch, app_module):
    import research as research_module
    monkeypatch.setattr(
        research_module, "get_research_digest",
        lambda: {"cached_at": "2026-01-01T00:00:00+00:00",
                 "papers": PAPERS, "total": 3, "stale": False,
                 "categories": {"cs.AI": 1, "cs.CL": 1, "cs.CV": 1},
                 "new_count": 0, "new_id_list": [], "current_ids": ["2601.00001"]},
    )
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_export_endpoint_bibtex_default(monkeypatch, app_module):
    c = _client_with_digest(monkeypatch, app_module)
    r = c.get("/api/research/export")
    assert r.status_code == 200
    assert r.content_type.startswith("application/x-bibtex")
    assert 'arxiv-digest-all.bib' in r.headers["Content-Disposition"]
    assert r.get_data(as_text=True).count("@article") == 3


def test_export_endpoint_csv(monkeypatch, app_module):
    c = _client_with_digest(monkeypatch, app_module)
    r = c.get("/api/research/export?format=csv")
    assert r.status_code == 200
    assert r.content_type.startswith("text/csv")
    assert 'arxiv-digest-all.csv' in r.headers["Content-Disposition"]
    # Round-trips as CSV with 3 data rows.
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert len(rows) == 3


def test_export_endpoint_json(monkeypatch, app_module):
    c = _client_with_digest(monkeypatch, app_module)
    r = c.get("/api/research/export?format=json")
    assert r.status_code == 200
    assert r.content_type.startswith("application/json")
    assert 'arxiv-digest-all.json' in r.headers["Content-Disposition"]
    data = r.get_json()
    assert data["count"] == 3 and len(data["papers"]) == 3


def test_export_endpoint_cat_filter(monkeypatch, app_module):
    c = _client_with_digest(monkeypatch, app_module)
    r = c.get("/api/research/export?format=json&cat=cs.CL")
    data = r.get_json()
    assert data["count"] == 1
    assert data["papers"][0]["arxiv_id"] == "2601.00002"
    # The category is reflected in the downloaded filename (slash-safe).
    r2 = c.get("/api/research/export?format=bibtex&cat=cs.CL")
    assert "arxiv-digest-cs.CL.bib" in r2.headers["Content-Disposition"]


def test_export_endpoint_invalid_format_400(monkeypatch, app_module):
    c = _client_with_digest(monkeypatch, app_module)
    r = c.get("/api/research/export?format=xml")
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"]
    assert set(body["formats"]) == {"bibtex", "csv", "json"}


def test_export_endpoint_security_headers(monkeypatch, app_module):
    # The export is a new response type (raw text / csv / json); confirm the
    # after_request hook still attaches the hardening headers to it.
    c = _client_with_digest(monkeypatch, app_module)
    for fmt in ("bibtex", "csv", "json"):
        r = c.get(f"/api/research/export?format={fmt}")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert "Content-Security-Policy" in r.headers
        assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"


def test_health_includes_export_check(app_module):
    # The digest export endpoint is part of the self-verify surface.
    assert any("api/research/export" in route for route, *_ in app_module._API_CHECKS)


# --- Page wiring: /research renders the export block -------------------------

def test_research_page_export_wiring(client, network_mocks):
    r = client.get("/research")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # The export block is present server-side (no-JS still works).
    assert 'id="research-export"' in html
    assert 'id="export-bibtex"' in html and 'id="export-csv"' in html and 'id="export-json"' in html
    # Default links target the full digest (no cat) for each format.
    assert '/api/research/export?format=bibtex' in html
    assert '/api/research/export?format=csv' in html
    assert '/api/research/export?format=json' in html
    # Category-aware wiring: the links follow the active category filter.
    assert "syncExportLinks" in html
    assert "encodeURIComponent(activeCat)" in html
