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


def get_research_digest() -> dict:
    """Return a research digest, using cache if fresh."""
    now = datetime.now(timezone.utc)

    # Try cache first
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text())
        cached_at = datetime.fromisoformat(cache["cached_at"])
        age = (now - cached_at).total_seconds()
        if age < CACHE_TTL_SECONDS:
            return cache

    # Fetch fresh
    all_papers = []
    for cat in CATEGORIES:
        try:
            papers = _fetch_arxiv(cat, max_results=5)
            all_papers.extend(papers)
        except Exception:
            pass  # skip categories that fail

    # Deduplicate by arxiv_id and sort by date
    seen = set()
    unique = []
    for p in all_papers:
        if p["arxiv_id"] not in seen:
            seen.add(p["arxiv_id"])
            unique.append(p)

    unique.sort(key=lambda x: x["published"], reverse=True)
    top = unique[:20]  # keep top 20 across all categories

    digest = {
        "cached_at": now.isoformat(),
        "papers": top,
        "total": len(top),
    }

    # Write cache
    CACHE_FILE.write_text(json.dumps(digest, indent=2))

    return digest
