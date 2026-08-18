"""Research digest resilience tests — hermetic (network mocked at _fetch_arxiv).

These lock in the fix for the real incident where a transient arXiv/proxy failure
was cached as an empty result and served as "fresh" for a full hour.
"""
import json

import pytest

import research


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the module's cache file at a temp path so tests never touch the real cache."""
    monkeypatch.setattr(research, "CACHE_FILE", tmp_path / "research_cache.json")
    yield


def _write_cache(cache):
    research.CACHE_FILE.write_text(json.dumps(cache))


def test_fresh_fetch_empty_with_stale_cache_returns_stale(monkeypatch):
    """Transient fetch failure must fall back to the stale cache, not go empty."""
    _write_cache({"cached_at": "2020-01-01T00:00:00+00:00",
                  "papers": [{"arxiv_id": "OLD", "title": "old"}], "total": 1})
    monkeypatch.setattr(research, "_fetch_arxiv", lambda *a, **k: [])
    d = research.get_research_digest()
    assert d["stale"] is True
    assert len(d["papers"]) == 1
    assert d["papers"][0]["arxiv_id"] == "OLD"


def test_fresh_fetch_empty_no_cache_does_not_poison(monkeypatch):
    """If there's nothing to fall back to, return empty but DO NOT write the cache,
    so the next call retries instead of pinning the empty state for an hour."""
    monkeypatch.setattr(research, "_fetch_arxiv", lambda *a, **k: [])
    d = research.get_research_digest()
    assert d["papers"] == []
    assert not research.CACHE_FILE.exists(), "empty result must not be cached"


def test_fresh_fetch_success_overrides_stale_cache(monkeypatch):
    _write_cache({"cached_at": "2020-01-01T00:00:00+00:00",
                  "papers": [{"arxiv_id": "OLD", "title": "old"}], "total": 1})
    monkeypatch.setattr(
        research, "_fetch_arxiv",
        lambda *a, **k: [{"arxiv_id": "NEW", "title": "fresh", "published": "2026-01-01",
                          "authors": "a", "summary": "s", "categories": ["cs.AI"],
                          "primary_category": "cs.AI"}],
    )
    d = research.get_research_digest()
    assert d["stale"] is False
    assert d["papers"][0]["arxiv_id"] == "NEW"
    # and it should have been persisted
    persisted = json.loads(research.CACHE_FILE.read_text())
    assert persisted["papers"][0]["arxiv_id"] == "NEW"


def test_fresh_valid_cache_served_without_fetch(monkeypatch):
    """A fresh cache with papers must be served without hitting the network."""
    _write_cache({"cached_at": "2026-08-18T13:00:00+00:00",
                  "papers": [{"arxiv_id": "CACHED", "title": "c"}], "total": 1, "stale": False})

    def _boom(*a, **k):
        raise AssertionError("should not fetch when a fresh cache has data")
    monkeypatch.setattr(research, "_fetch_arxiv", _boom)
    d = research.get_research_digest()
    assert d["papers"][0]["arxiv_id"] == "CACHED"


def test_corrupt_cache_is_ignored(monkeypatch):
    """A corrupt cache file must not crash the digest; it should be treated as absent."""
    research.CACHE_FILE.write_text("{ this is not json ]")
    monkeypatch.setattr(
        research, "_fetch_arxiv",
        lambda *a, **k: [{"arxiv_id": "NEW", "title": "fresh", "published": "2026-01-01",
                          "authors": "a", "summary": "s", "categories": ["cs.AI"],
                          "primary_category": "cs.AI"}],
    )
    d = research.get_research_digest()
    assert d["papers"][0]["arxiv_id"] == "NEW"


# --- Category breakdown + new-since-last-check (depth iteration on the digest) ---

def _paper(pid, cat, date):
    return {"arxiv_id": pid, "title": pid, "published": date,
            "authors": "x", "summary": "s", "categories": [cat],
            "primary_category": cat}


def test_category_breakdown_counts_and_order():
    papers = [
        {"arxiv_id": "1", "primary_category": "cs.AI"},
        {"arxiv_id": "2", "primary_category": "cs.LG"},
        {"arxiv_id": "3", "primary_category": "cs.LG"},
        {"arxiv_id": "4"},  # no primary_category -> bucketed as "unknown"
    ]
    # sorted by count desc, then name: cs.LG(2), then cs.AI(1) and unknown(1)
    assert research.category_breakdown(papers) == {
        "cs.LG": 2, "cs.AI": 1, "unknown": 1
    }


def test_category_breakdown_empty():
    assert research.category_breakdown([]) == {}


def test_new_papers_since_all_new_when_no_baseline():
    papers = [{"arxiv_id": "1"}, {"arxiv_id": "2"}]
    assert [p["arxiv_id"] for p in research.new_papers_since(papers, [])] == ["1", "2"]
    assert [p["arxiv_id"] for p in research.new_papers_since(papers, None)] == ["1", "2"]


def test_new_papers_since_deltas_only_new_ids():
    papers = [{"arxiv_id": "A"}, {"arxiv_id": "B"}, {"arxiv_id": "C"}]
    assert [p["arxiv_id"] for p in research.new_papers_since(papers, ["A", "B"])] == ["C"]


def test_first_fetch_marks_all_new_and_breakdown(monkeypatch):
    monkeypatch.setattr(
        research, "_fetch_arxiv",
        lambda *a, **k: [_paper("A", "cs.AI", "2026-01-01"), _paper("B", "cs.LG", "2026-01-02")],
    )
    d = research.get_research_digest()
    assert d["new_count"] == 2
    assert set(d["new_id_list"]) == {"A", "B"}
    assert d["categories"] == {"cs.AI": 1, "cs.LG": 1}
    assert set(d["current_ids"]) == {"A", "B"}


def test_fresh_serve_preserves_delta_without_refetch(monkeypatch):
    papers = [_paper("A", "cs.AI", "2026-01-01"), _paper("B", "cs.LG", "2026-01-02")]
    monkeypatch.setattr(research, "_fetch_arxiv", lambda *a, **k: papers)
    first = research.get_research_digest()

    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return papers

    monkeypatch.setattr(research, "_fetch_arxiv", counting)
    d = research.get_research_digest()
    assert calls["n"] == 0, "a fresh cache must be served without refetching"
    # the delta is preserved across serves of the same snapshot (not recomputed to 0)
    assert d["new_count"] == first["new_count"] == 2
    assert set(d["new_id_list"]) == {"A", "B"}


def test_refetch_flags_only_genuinely_new(monkeypatch):
    # establish a baseline snapshot containing B
    monkeypatch.setattr(research, "_fetch_arxiv",
                        lambda *a, **k: [_paper("B", "cs.LG", "2026-01-02")])
    research.get_research_digest()

    # age the cache past the TTL so the next call re-fetches
    cache = json.loads(research.CACHE_FILE.read_text())
    cache["cached_at"] = "2020-01-01T00:00:00+00:00"
    research.CACHE_FILE.write_text(json.dumps(cache))

    # arXiv now has a genuinely new paper C alongside the old B
    monkeypatch.setattr(
        research, "_fetch_arxiv",
        lambda *a, **k: [_paper("C", "cs.CV", "2026-01-03"), _paper("B", "cs.LG", "2026-01-02")],
    )
    d = research.get_research_digest()
    assert d["new_count"] == 1
    assert d["new_id_list"] == ["C"]
    assert set(d["current_ids"]) == {"B", "C"}
