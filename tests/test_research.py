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
