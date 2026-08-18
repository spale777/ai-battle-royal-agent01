"""Research digest — fetch and cache arXiv papers."""

import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).parent
CACHE_FILE = BASE / "research_cache.json"
CACHE_TTL_SECONDS = 3600  # 1 hour — arXiv is slow

CATEGORIES = [
    "cs.AI",
    "cs.LG",
    "cs.CL",
    "cs.CV",
]

NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _fetch_arxiv(category: str, max_results: int = 5) -> list[dict]:
    """Query arXiv API for recent papers in a category."""
    url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query=cat:{category}"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results}"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        root = ET.fromstring(resp.read())

    papers = []
    for entry in root.findall("a:entry", NS):
        title_t = entry.find("a:title", NS)
        summary_t = entry.find("a:summary", NS)
        published_t = entry.find("a:published", NS)
        id_t = entry.find("a:id", NS)

        if not (title_t is not None and title_t.text and summary_t is not None and summary_t.text and published_t is not None and id_t is not None):
            continue

        title = (title_t.text or "").strip().replace("\n", " ")
        summary = (summary_t.text or "").strip()[:250]
        published = (published_t.text or "")[:10]
        arxiv_id = (id_t.text or "").strip().split("/abs/")[-1].rsplit("v", 1)[0]

        author_names: list[str] = []
        for a in entry.findall("a:author", NS):
            name_el = a.find("a:name", NS)
            if name_el is not None and name_el.text:
                author_names.append(name_el.text)
        authors = ", ".join(author_names)[:100]

        cats = [c.get("term") for c in entry.findall("a:category", NS)]
        primary = entry.find("arxiv:primary_category", NS)
        if primary is not None:
            primary_cat = primary.get("term")
        else:
            primary_cat = cats[0] if cats else "unknown"

        papers.append({
            "title": title,
            "arxiv_id": arxiv_id,
            "published": published,
            "authors": authors,
            "summary": summary,
            "categories": cats[:5],
            "primary_category": primary_cat,
        })

    return papers


def category_breakdown(papers: list[dict]) -> dict:
    """Count papers per primary category, sorted by count desc then name.

    Pure and deterministic so it is easy to unit-test. Papers without a
    primary_category are bucketed under "unknown".
    """
    counts: dict[str, int] = {}
    for p in papers:
        cat = (p.get("primary_category") or "unknown").strip() or "unknown"
        counts[cat] = counts.get(cat, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def new_papers_since(papers: list[dict], previous_ids) -> list[dict]:
    """Papers not present in the previous snapshot (by arxiv_id).

    Pure. An empty/None previous_ids means "first run" -> every paper is new.
    """
    prev = set(previous_ids or [])
    return [p for p in papers if p.get("arxiv_id") and p["arxiv_id"] not in prev]


def _enrich(digest: dict, prev_ids) -> dict:
    """Fold a per-category breakdown and a "new since last check" delta into a
    digest, in place. `prev_ids` is the id set of the previous snapshot (papers
    whose ids are not in it are "new"). All added fields are JSON-serializable so
    the digest stays safe to return from /api/research. The baseline is stored as
    current_ids so the next fetch can diff against this snapshot. Returns the same
    dict for chaining."""
    papers = digest.get("papers", [])
    new = new_papers_since(papers, prev_ids)
    digest["categories"] = category_breakdown(papers)
    digest["new_papers"] = new
    digest["new_count"] = len(new)
    digest["new_id_list"] = [p["arxiv_id"] for p in new]
    digest["current_ids"] = [p["arxiv_id"] for p in papers]
    return digest


def _serve_enriched(digest: dict) -> dict:
    """Serve a previously-written digest, preserving the new-delta it was computed
    with at fetch time. A cache that predates this feature (has papers but no
    'categories') is backfilled with a zero-delta baseline."""
    if "categories" in digest:
        return digest
    return _enrich(digest, digest.get("current_ids", []))


def get_research_digest() -> dict:
    """Return a research digest, using cache if fresh.

    Resilience rules (added after a transient arXiv/proxy failure was cached as an
    empty result and served as "fresh" for an hour):
      - A fresh cache with papers is served as-is.
      - A fresh fetch is only cached if it actually returned papers.
      - If a fresh fetch fails or comes back empty, we fall back to a stale cache
        (stale-while-revalidate) rather than poisoning the cache with an empty list.
      - If there is no usable cache at all, we return the (possibly empty) digest
        but do NOT write it to cache, so the next call retries the fetch.
    """
    now = datetime.now(timezone.utc)

    existing_cache = None
    cache_is_fresh = False
    if CACHE_FILE.exists():
        try:
            existing_cache = json.loads(CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(existing_cache["cached_at"])
            cache_is_fresh = (now - cached_at).total_seconds() < CACHE_TTL_SECONDS
        except (ValueError, json.JSONDecodeError, KeyError):
            existing_cache = None  # corrupt cache — ignore it

    # 1) Fresh cache already has data — serve it, no fetch. Preserve the
    #    "new since last check" delta it was computed with at fetch time.
    if existing_cache and cache_is_fresh and existing_cache.get("papers"):
        return _serve_enriched(existing_cache)

    # 2) Otherwise attempt a fresh fetch.
    all_papers = []
    for cat in CATEGORIES:
        try:
            papers = _fetch_arxiv(cat, max_results=5)
            all_papers.extend(papers)
        except Exception:
            pass  # skip categories that fail (transient proxy blips happen)

    # Deduplicate by arxiv_id and sort by date
    seen = set()
    unique = []
    for p in all_papers:
        if p["arxiv_id"] not in seen:
            seen.add(p["arxiv_id"])
            unique.append(p)

    unique.sort(key=lambda x: x["published"], reverse=True)
    top = unique[:20]  # keep top 20 across all categories

    # 3) Fresh fetch succeeded with real data — cache and return it.
    if top:
        digest = {
            "cached_at": now.isoformat(),
            "papers": top,
            "total": len(top),
            "stale": False,
        }
        # The "new since last check" baseline is the previous snapshot's id set
        # (from the old cache). First run: no previous snapshot -> everything new.
        prev_ids = (existing_cache or {}).get("current_ids", [])
        # Fold in the breakdown + new-since-last-check delta BEFORE caching, so
        # the persisted snapshot carries the id baseline for the next run's delta.
        _enrich(digest, prev_ids)
        try:
            CACHE_FILE.write_text(json.dumps(digest, indent=2))
        except OSError:
            pass
        return digest

    # 4) Fresh fetch was empty. Fall back to a stale cache if it has data.
    if existing_cache and existing_cache.get("papers"):
        stale = dict(existing_cache)
        stale["stale"] = True  # honest signal: this is older than the TTL
        return _serve_enriched(stale)

    # 5) No usable data anywhere — return empty, but do NOT cache it, so the next
    #    call retries the fetch instead of pinning the empty state for an hour.
    return _enrich({"cached_at": now.isoformat(), "papers": [], "total": 0, "stale": False}, [])
