"""Tests for the live arXiv search (research.search / /api/research/search).

All network is mocked so these run hermetically. The pure logic (validation,
field/sort normalization, max clamping, local fallback search) is asserted
directly; the endpoint is asserted through the Flask test client.
"""
import xml.etree.ElementTree as ET

import pytest

import research


# A minimal but real-shaped arXiv Atom feed (two namespaces + opensearch count).
ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>2</opensearch:totalResults>
  <entry>
    <title>Diffusion Models
for Image Generation</title>
    <summary>A summary about diffusion.</summary>
    <published>2026-07-01T00:00:00Z</published>
    <id>http://arxiv.org/abs/2401.11111v2</id>
    <author><name>Alice A.</name></author>
    <author><name>Bob B.</name></author>
    <category term="cs.CV"/>
    <category term="cs.LG"/>
    <arxiv:primary_category term="cs.CV"/>
  </entry>
  <entry>
    <title>Reinforcement Learning for Control</title>
    <summary>An RL paper.</summary>
    <published>2026-06-30T00:00:00Z</published>
    <id>http://arxiv.org/abs/2401.22222</id>
    <author><name>Carol C.</name></author>
    <category term="cs.LG"/>
    <arxiv:primary_category term="cs.LG"/>
  </entry>
</feed>
"""


def _paper():
    return {
        "title": "Transformer Networks", "arxiv_id": "2401.00001",
        "published": "2024-01-01", "authors": "A. Person",
        "summary": "about attention", "categories": ["cs.LG"],
        "primary_category": "cs.LG",
    }


# ---------------------------------------------------------------------------
# Shared parser (used by both the category digest and live search)
# ---------------------------------------------------------------------------

def test_parse_entries_extracts_full_paper_shape():
    root = ET.fromstring(ARXIV_FEED)
    papers = research._parse_entries(root)
    assert len(papers) == 2
    first = papers[0]
    assert first["arxiv_id"] == "2401.11111"  # version stripped
    assert first["published"] == "2026-07-01"  # date prefix only
    assert first["title"] == "Diffusion Models for Image Generation"  # newlines folded
    assert first["authors"] == "Alice A., Bob B."
    assert first["primary_category"] == "cs.CV"
    assert first["categories"] == ["cs.CV", "cs.LG"]
    assert papers[1]["arxiv_id"] == "2401.22222"


def test_total_results_reads_opensearch_count():
    root = ET.fromstring(ARXIV_FEED)
    assert research._total_results(root) == 2


def test_total_results_zero_when_absent():
    root = ET.fromstring("<feed xmlns='http://www.w3.org/2005/Atom'></feed>")
    assert research._total_results(root) == 0


# ---------------------------------------------------------------------------
# Pure search() logic (network mocked)
# ---------------------------------------------------------------------------

def test_search_rejects_empty_query():
    assert research.search("   ")["source"] == "none"
    assert research.search("a")["source"] == "none"


def test_search_live_success(monkeypatch):
    monkeypatch.setattr(research, "search_arxiv_live",
                        lambda *a, **k: ([_paper()], 42))
    r = research.search("transformer", max_results=5, field="ti", sort="date")
    assert r["source"] == "arxiv"
    assert r["total_results"] == 42
    assert r["field"] == "ti"
    assert r["sort"] == "date"
    assert len(r["papers"]) == 1
    assert r["papers"][0]["arxiv_id"] == "2401.00001"


def test_search_live_zero_hits_is_not_fallback(monkeypatch):
    monkeypatch.setattr(research, "search_arxiv_live", lambda *a, **k: ([], 0))
    r = research.search("zzzqqq", max_results=5)
    # A live "no matches" is a real answer, not a cache fallback.
    assert r["source"] == "arxiv"
    assert r["total_results"] == 0
    assert r["papers"] == []
    assert "No matches" in r["message"]


def test_search_falls_back_to_cache_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("arxiv down")
    monkeypatch.setattr(research, "search_arxiv_live", boom)
    monkeypatch.setattr(research, "get_research_digest",
                        lambda: {"papers": [_paper()]})
    r = research.search("transformer", max_results=5)
    assert r["source"] == "cache"
    assert r["total_results"] is None
    assert len(r["papers"]) == 1
    assert "unreachable" in r["message"].lower()


def test_search_fallback_with_empty_cache(monkeypatch):
    monkeypatch.setattr(research, "search_arxiv_live",
                        lambda *a, **k: (_ for _ in ()).throw(Exception("x")))
    monkeypatch.setattr(research, "get_research_digest", lambda: {"papers": []})
    r = research.search("transformer")
    assert r["source"] == "cache"
    assert r["papers"] == []


def test_search_clamps_max_to_cap(monkeypatch):
    captured = {}
    def fake(q, max_results, field, sort):
        captured["max"] = max_results
        return ([_paper()], 1)
    monkeypatch.setattr(research, "search_arxiv_live", fake)
    research.search("x y", max_results=9999)
    assert captured["max"] == research.MAX_RESULTS_CAP


def test_search_normalizes_field_and_sort(monkeypatch):
    captured = {}
    def fake(q, max_results, field, sort):
        captured["field"] = field
        captured["sort"] = sort
        return ([_paper()], 1)
    monkeypatch.setattr(research, "search_arxiv_live", fake)
    research.search("x y", field="BOGUS", sort="nope")
    assert captured["field"] == "all"
    assert captured["sort"] == "relevance"


def test_local_search_matches_all_fields_case_insensitive():
    papers = [
        {"title": "Diffusion Models", "authors": "Alice A.",
         "arxiv_id": "2401.11111", "categories": ["cs.CV"], "summary": "images"},
        {"title": "Unrelated Thing", "authors": "Nobody",
         "arxiv_id": "9999.99999", "categories": ["cs.AI"], "summary": "other"},
    ]
    assert [p["arxiv_id"] for p in research._local_search(papers, "diffusion")] == ["2401.11111"]
    assert [p["arxiv_id"] for p in research._local_search(papers, "alice")] == ["2401.11111"]
    assert [p["arxiv_id"] for p in research._local_search(papers, "cs.cv")] == ["2401.11111"]
    assert [p["arxiv_id"] for p in research._local_search(papers, "images")] == ["2401.11111"]
    assert research._local_search(papers, "zzzz") == []


# ---------------------------------------------------------------------------
# Endpoint (/api/research/search) via the test client
# ---------------------------------------------------------------------------

def test_api_research_search_requires_query(client):
    r = client.get("/api/research/search")
    assert r.status_code == 400
    assert r.get_json()["source"] == "none"


def test_api_research_search_rejects_short_query(client):
    r = client.get("/api/research/search?q=a")
    assert r.status_code == 400


def test_api_research_search_live(client, monkeypatch):
    monkeypatch.setattr(research, "search_arxiv_live",
                        lambda *a, **k: ([_paper(), _paper()], 7))
    r = client.get("/api/research/search?q=transformer&field=ti&sort=date&max=5")
    assert r.status_code == 200
    j = r.get_json()
    assert j["source"] == "arxiv"
    assert j["total_results"] == 7
    assert isinstance(j["papers"], list) and len(j["papers"]) == 2


def test_api_research_search_fallback(client, monkeypatch):
    monkeypatch.setattr(research, "search_arxiv_live",
                        lambda *a, **k: (_ for _ in ()).throw(Exception("down")))
    monkeypatch.setattr(research, "get_research_digest",
                        lambda: {"papers": [_paper()]})
    r = client.get("/api/research/search?q=transformer")
    assert r.status_code == 200
    j = r.get_json()
    assert j["source"] == "cache"
    assert j["total_results"] is None
    assert len(j["papers"]) == 1


def test_api_research_search_non_int_max_defaults(client, monkeypatch):
    monkeypatch.setattr(research, "search_arxiv_live",
                        lambda *a, **k: ([_paper()], 1))
    r = client.get("/api/research/search?q=transformer&max=abc")
    assert r.status_code == 200


def test_research_page_has_search_form(client, monkeypatch):
    # The page must expose the live-search form markup (contract with the JS).
    monkeypatch.setattr(research, "get_research_digest",
                        lambda: {"cached_at": "2026-01-01T00:00:00+00:00",
                                 "papers": [], "total": 0, "stale": False,
                                 "categories": {}, "new_papers": [], "new_count": 0,
                                 "new_id_list": [], "current_ids": []})
    html = client.get("/research").get_data(as_text=True)
    assert 'id="arxiv-search-form"' in html
    assert 'id="arxiv-q"' in html
    assert 'id="arxiv-results"' in html
    assert "Search arXiv" in html
