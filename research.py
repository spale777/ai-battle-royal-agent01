"""Research digest — fetch and cache arXiv papers."""

import csv
import io
import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).parent
CACHE_FILE = BASE / "research_cache.json"
CACHE_TTL_SECONDS = 3600  # 1 hour — arXiv is slow
# Per-fetch socket timeout for every arXiv call. Lowered 15s -> 5s so that a
# single in-flight arXiv request — and by extension the /api/health call that
# re-runs it — is hard-bounded well under gunicorn's worker timeout. 5s is
# still generous for a healthy arXiv response (typical is <1s); a slow/dead
# arXiv degrades honestly to the cache fallback instead of holding a worker.
ARXIV_FETCH_TIMEOUT = 5.0

CATEGORIES = [
    "cs.AI",
    "cs.LG",
    "cs.CL",
    "cs.CV",
]

NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _parse_entries(root) -> list[dict]:
    """Parse an arXiv Atom feed into a list of paper dicts.

    Shared by the category digest fetcher and the live search so both render
    the exact same paper shape (title, arxiv_id, published, authors, summary,
    categories, primary_category). Python 3.13: test Element attributes/text
    explicitly — never an element's truth value (deprecated, always True)."""
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
            "author_names": list(author_names),  # full, untruncated — for BibTeX
            "summary": summary,
            "categories": cats[:5],
            "primary_category": primary_cat,
        })

    return papers


def _bibtex_key(paper: dict) -> str:
    """A stable BibTeX citation key: first author's surname + year + first word
    of the title. Deterministic and pure (no network)."""
    names = paper.get("author_names") or []
    # First author's surname (last token of their name); fall back to id.
    if names:
        first = names[0].strip().split()
        surname = (first[-1] if first else "anon")
    else:
        surname = (paper.get("authors") or "anon").strip().split()[-1] or "anon"
    surname = surname[0].upper() + surname[1:].lower()
    year = (paper.get("published") or "").split("-")
    year = year[0] if year and year[0].isdigit() else "n.d."
    # First meaningful word of the title (drop leading stopwords / punctuation).
    title_words = [w for w in (paper.get("title") or "").replace(",", " ").split()
                   if w and w[0].isalnum()]
    word = title_words[0].lower() if title_words else "paper"
    # Collapse to ascii alnum for a clean key.
    word = re.sub(r"[^a-z0-9]", "", word) or "paper"
    return f"{surname}{year}{word}"


def bibtex(paper: dict) -> str:
    """Build a BibTeX @article entry for an arXiv paper, server-side.

    This is the SINGLE SOURCE OF TRUTH for a paper's citation: the digest page,
    the live-search results, and the JSON API all call this, so what a visitor
    copies is exactly what the API returns — no client-side re-implementation
    that could drift from the data. Pure and deterministic (no network, no
    state); a malformed/missing field degrades to a placeholder, never an error.
    Only well-known, machine-verifiable fields are used (id, title, authors,
    year, arXiv URL) — no invented data.
    """
    pid = (paper.get("arxiv_id") or "unknown").strip()
    title = (paper.get("title") or "(untitled)").strip().replace("\n", " ")
    # Join the full author list; fall back to the (truncated) display string.
    names = paper.get("author_names") or []
    author_field = " and ".join(n.strip() for n in names if n.strip())
    if not author_field:
        author_field = (paper.get("authors") or "").strip() or "Unknown"
    year = (paper.get("published") or "").split("-")
    year = year[0] if year and year[0].isdigit() else "n.d."
    key = _bibtex_key(paper)
    primary = (paper.get("primary_category") or "").strip()
    note = f" arXiv preprint, arXiv:{pid}" + (f" ({primary})" if primary else "")
    return (
        "@article{" + key + ",\n"
        "  title         = {" + title + "},\n"
        "  author        = {" + author_field + "},\n"
        "  year          = {" + year + "},\n"
        "  eprint        = {" + pid + "},\n"
        "  archivePrefix = {arXiv},\n"
        "  primaryClass  = {" + (primary or "n/a") + "},\n"
        "  doi           = {10.48550/arXiv." + pid + "},\n"
        "  url           = {https://arxiv.org/abs/" + pid + "},\n"
        "  note          = {" + note + "}\n"
        "}\n"
    )


def _fetch_arxiv(category: str, max_results: int = 5) -> list[dict]:
    """Query arXiv API for recent papers in a category."""
    url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query=cat:{category}"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results}"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=ARXIV_FETCH_TIMEOUT) as resp:
        root = ET.fromstring(resp.read())
    return _parse_entries(root)


# --- Live arXiv search (outward-facing: lets a visitor query the corpus) ---
#
# This is read-only: it sends a query to arXiv and returns their results. No
# code runs, no data is stored, no personal information is involved. When arXiv
# is unreachable (transient proxy blips happen), it degrades honestly to a local
# search over the cached digest and says so via the `source` field.

_ARXIV_API = "https://export.arxiv.org/api/query"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

# Field prefixes the arXiv API supports for scoped queries. Kept explicit so a
# visitor cannot inject an arbitrary prefix.
ALLOWED_FIELDS = ("all", "ti", "au", "abs")
# Sort options exposed to the UI.
ALLOWED_SORTS = ("relevance", "date")

MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 200
MAX_RESULTS_CAP = 30  # arXiv allows up to 30 per page; keep the UI light.


def _total_results(root) -> int:
    """Read opensearch:totalResults from an arXiv feed (0 if absent)."""
    t = root.find(f"{{{_OPENSEARCH_NS}}}totalResults")
    if t is not None and t.text:
        try:
            return int(t.text)
        except ValueError:
            return 0
    return 0


def search_arxiv_live(query: str, max_results: int = 10,
                      field: str = "all", sort: str = "relevance",
                      start: int = 0):
    """Run one live query against the arXiv API.

    Returns (papers, total_results) on success. Raises on network/parse error
    so the caller can fall back to the cached digest. `field`/`sort` must be in
    the allow-lists (validated by `search`, but defensively re-checked here so
    this function is safe to call directly too).

    `start` is arXiv's zero-based result offset, used for pagination:
    page N of `max_results`-per-page is `start = (N-1) * max_results`. Without
    it a visitor sees only the top of a "1,259,403 total" count; with it they
    can page through the real results.
    """
    field = field if field in ALLOWED_FIELDS else "all"
    sort = sort if sort in ALLOWED_SORTS else "relevance"
    sort_param = "relevance" if sort == "relevance" else "submittedDate"
    start = max(0, int(start))

    url = (
        f"{_ARXIV_API}?search_query={field}:{urllib.parse.quote(query)}"
        f"&sortBy={sort_param}"
        f"&sortOrder=descending"
        f"&max_results={max_results}"
        f"&start={start}"
    )
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=ARXIV_FETCH_TIMEOUT) as resp:
        root = ET.fromstring(resp.read())
    return _parse_entries(root), _total_results(root)


def _local_search(papers: list[dict], q: str) -> list[dict]:
    """Case-insensitive substring search over a paper list's title, authors,
    id, categories, and summary. Pure and deterministic (easy to test)."""
    q = q.lower()
    out = []
    for p in papers:
        hay = " ".join([
            p.get("title", ""), p.get("authors", ""), p.get("arxiv_id", ""),
            " ".join(p.get("categories", [])), p.get("summary", ""),
        ]).lower()
        if q in hay:
            out.append(p)
    return out


def _page_meta(total, max_results, page):
    """Compute 1-based pagination metadata from a result total.

    Pure. `per_page` is the requested page size (already clamped), `page` is the
    1-based page requested, `total_pages` is the number of pages needed to cover
    `total` results (0 when there are none), and `next_page`/`prev_page` are the
    adjacent pages when they exist (else None). A page beyond total_pages is
    not an error — arXiv returns an empty result set and we surface that honestly.
    """
    total_pages = 0 if total is None else (max(0, total) + max_results - 1) // max_results
    prev = page - 1 if page > 1 else None
    # No "next" once the current page is the last full page (or beyond it).
    next_page = page + 1 if (total_pages and page < total_pages) else None
    return {
        "per_page": max_results,
        "page": page,
        "total_pages": total_pages,
        "next_page": next_page,
        "prev_page": prev,
    }


def search(query: str, max_results: int = 10, field: str = "all",
           sort: str = "relevance", page: int = 1) -> dict:
    """Search arXiv live; fall back to the cached digest when the live fetch
    fails. Always returns a JSON-safe dict with an honest `source`:

      - "arxiv"   -> results came from a live arXiv query (total_results set,
                     and `page`/`total_pages` metadata for pagination).
      - "cache"   -> live fetch failed; results are a local search over the
                     cached digest (up to `max_results`, total_results=None).
                     The digest is small so pagination is meaningless there —
                     the page metadata reports a single page.

    This is the outward-facing capability: a visitor can search the whole arXiv
    corpus, not just the 20 papers in the hourly digest, and page through the
    real results (arXiv `start=` offset). Read-only — no code runs, nothing is
    stored, no personal data is involved.
    """
    q = (query or "").strip()
    if len(q) < MIN_QUERY_CHARS:
        return {"query": q, "source": "none", "papers": [], "total_results": 0,
                "page": 1, "per_page": max_results, "total_pages": 0,
                "next_page": None, "prev_page": None,
                "message": "Query too short (min 2 chars)."}

    max_results = max(1, min(int(max_results), MAX_RESULTS_CAP))
    field = field if field in ALLOWED_FIELDS else "all"
    sort = sort if sort in ALLOWED_SORTS else "relevance"
    page = max(1, int(page))  # 1-based; negative/zero clamp to the first page
    start = (page - 1) * max_results

    try:
        papers, total = search_arxiv_live(q, max_results, field, sort, start)
        meta = _page_meta(total, max_results, page)
        if papers:
            return {"query": q, "source": "arxiv", "papers": papers,
                    "total_results": total, "field": field, "sort": sort, **meta}
        # Live succeeded but returned zero hits — that's a real answer, not a
        # fallback. A page past the end also lands here (arXiv returns empty).
        return {"query": q, "source": "arxiv", "papers": [], "total_results": total,
                "field": field, "sort": sort, **meta,
                "message": "No matches on arXiv for this query."}
    except Exception:
        pass

    # Fallback: search the cached digest locally, and be honest about it. The
    # digest is small, so the whole result fits on one page.
    try:
        digest = get_research_digest()
        cache_papers = digest.get("papers", [])
    except Exception:
        cache_papers = []
    hits = _local_search(cache_papers, q)[:max_results]
    return {
        "query": q, "source": "cache", "papers": hits, "total_results": None,
        "field": field, "sort": sort,
        "page": 1, "per_page": max_results, "total_pages": 1 if hits else 0,
        "next_page": None, "prev_page": None,
        "message": ("arXiv is unreachable right now — showing matches from the "
                    "cached digest (last few days, ~20 papers) instead."),
    }


# --- Export (outward-facing: let a researcher take the digest with them) ------
#
# A single-paper "copy BibTeX" button already exists; this completes the loop by
# letting a visitor export the whole digest — or the category-filtered subset —
# in a format a researcher actually uses. It is read-only and pure: no network,
# no state, bounded to the digest's (small) paper count, and the BibTeX output is
# the SAME string research.bibtex() already produces for the per-paper buttons,
# so what you download is exactly what you copy.

EXPORT_FORMATS = ("bibtex", "csv", "json")

_CSV_COLUMNS = [
    "arxiv_id", "title", "published", "authors", "primary_category",
    "categories", "url", "doi", "abstract",
]


def _export_url(paper: dict) -> str:
    return "https://arxiv.org/abs/" + (paper.get("arxiv_id") or "").strip()


def _export_doi(paper: dict) -> str:
    pid = (paper.get("arxiv_id") or "").strip()
    return f"10.48550/arXiv.{pid}" if pid else ""


def export_papers(papers, fmt: str = "bibtex", cat: str | None = None) -> str:
    """Serialize a list of papers into an export format, as text.

    `papers` is the digest's paper list (the dicts produced by the parser /
    `get_research_digest`). `fmt` is one of EXPORT_FORMATS:
      - "bibtex" -> a .bib file: one @article per paper, from research.bibtex()
                    (the single source of truth shared with the per-paper copy
                    buttons). Blank line between entries.
      - "csv"    -> one row per paper, RFC-4180 quoting, fixed column order.
      - "json"   -> a stable, JSON-safe object: {exported_at, count, format,
                    papers:[...]} with a curated field set (no internal keys).
    `cat` optionally narrows to papers whose primary_category (case-insensitive)
    or any listed category equals it — the same match the /research page filter
    uses, so an export of a filtered view matches what the visitor sees. An
    unknown/empty `cat` returns all papers.

    Pure and deterministic (no network, no clock for bibtex/csv; the json
    `exported_at` is the only time-dependent field and is injected by the caller
    so the function itself stays testable). Never raises on a malformed paper —
    missing fields degrade to empty strings.
    """
    fmt = (fmt or "").strip().lower()
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {list(EXPORT_FORMATS)}, got {fmt!r}")

    if cat:
        cat = cat.strip().lower()
        if cat:
            papers = [
                p for p in papers
                if (p.get("primary_category") or "").lower() == cat
                or any((c or "").lower() == cat for c in p.get("categories", []))
            ]

    if fmt == "bibtex":
        # research.bibtex is the single source of truth — the same string the
        # per-paper "copy citation" button uses.
        return "\n".join(bibtex(p) for p in papers)

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for p in papers:
            writer.writerow({
                "arxiv_id": (p.get("arxiv_id") or "").strip(),
                "title": (p.get("title") or "").strip(),
                "published": (p.get("published") or "").strip(),
                "authors": (p.get("authors") or "").strip(),
                "primary_category": (p.get("primary_category") or "").strip(),
                "categories": " ".join(p.get("categories", [])),
                "url": _export_url(p),
                "doi": _export_doi(p),
                "abstract": (p.get("summary") or "").strip().replace("\n", " "),
            })
        return buf.getvalue()

    # json
    items = [
        {
            "arxiv_id": (p.get("arxiv_id") or "").strip(),
            "title": (p.get("title") or "").strip(),
            "published": (p.get("published") or "").strip(),
            "authors": (p.get("authors") or "").strip(),
            "author_names": list(p.get("author_names", [])),
            "primary_category": (p.get("primary_category") or "").strip(),
            "categories": list(p.get("categories", [])),
            "url": _export_url(p),
            "doi": _export_doi(p),
            "abstract": (p.get("summary") or "").strip(),
        }
        for p in papers
    ]
    return json.dumps(
        {"count": len(items), "format": "json", "papers": items},
        indent=2, ensure_ascii=False,
    )


# --- Egress self-check (turns a SILENT degradation into VISIBLE data) ---------
#
# The live features above reach the internet with a plain urllib.request call
# that inherits proxy settings from the process environment. When that
# environment is missing the proxy vars (this box has NO direct egress), the
# features don't crash — they fail to resolve names and `search()` silently
# degrades to `source="cache"`, so a visitor sees stale results with no warning.
# That exact failure mode (shell curl works, service silently falls back) was
# caught in session 14 only by luck. This endpoint makes the class of bug
# observable on demand: it probes the same urllib path and reports, with a
# pointed error, whether the service can actually reach the internet.

EGRESS_PROBE_URL = "https://www.google.com/generate_204"


def _egress_default_probe(url: str, timeout: float):
    """The default egress probe: a HEAD-ish GET through urllib.request, which
    inherits proxy settings from the environment exactly like the arXiv fetchers
    do. Kept as a separate function so tests can monkeypatch it and stay
    hermetic. Returns the HTTP status code on success; raises on any network
    error (URLError for DNS/proxy, HTTPError for a real non-2xx response)."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def check_egress(probe_url: str = EGRESS_PROBE_URL, timeout: float = 5.0,
                 fetcher=None) -> dict:
    """Probe the service's outbound HTTP path and report reachability as data.

    `fetcher` is injectable (defaults to `_egress_default_probe`) so tests run
    hermetically. The result is JSON-safe and always carries the probe's status
    and a `message` that points at the most likely cause when it fails —
    specifically the proxy-missing signature (DNS failure + no proxy env vars),
    which is the regression this box is actually prone to.
    """
    fetch = fetcher or _egress_default_probe
    proxy_set = any(
        os.environ.get(k) for k in ("http_proxy", "https_proxy", "all_proxy")
    )
    try:
        status = fetch(probe_url, timeout)
        return {
            "reachable": True,
            "url": probe_url,
            "status": status,
            "proxy_configured": proxy_set,
            "message": f"Outbound HTTPS via urllib OK (HTTP {status}).",
        }
    except urllib.error.HTTPError as e:
        # We reached a real HTTP server but it answered non-2xx — that still
        # proves egress works (the name resolved and a proxy responded).
        return {
            "reachable": True,
            "url": probe_url,
            "status": e.code,
            "proxy_configured": proxy_set,
            "message": f"Outbound HTTPS OK (server responded HTTP {e.code}).",
        }
    except Exception as e:
        # URLError wraps a socket.gaierror for DNS failure; the message
        # "Temporary failure in name resolution" is the exact signature of the
        # proxy-missing regression on this box.
        raw = str(e)
        is_dns = "name resolution" in raw or "getaddrinfo" in raw or "gaierror" in raw
        if is_dns and not proxy_set:
            message = (
                "Name resolution failed and no proxy is configured — the "
                "service has no direct egress, so internet features (live arXiv "
                "search) will silently degrade to their cache fallback. Add "
                "http_proxy/https_proxy to the service environment."
            )
        else:
            message = f"Outbound HTTPS probe failed: {raw}"
        return {
            "reachable": False,
            "url": probe_url,
            "status": None,
            "proxy_configured": proxy_set,
            "message": message,
        }


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
    #
    # Fast-fail on the first failure: a per-category try/except used to let a
    # fully-down arXiv run all 4 fetches to their socket timeout (~4 x 5s = 20s
    # in one in-flight call). If the feed for the first category can't be
    # fetched, the rest will almost certainly fail the same way (same host,
    # same proxy path) — stop after the first failure and fall back to the
    # stale-while-revalidate path below. A genuinely transient blip on category
    # 1 degrades to the cached digest for one TTL cycle, which is the same
    # behavior the except-already gave for later categories.
    all_papers = []
    for cat in CATEGORIES:
        try:
            papers = _fetch_arxiv(cat, max_results=5)
        except Exception:
            break  # same host / same proxy — the rest will fail too; fail fast
        all_papers.extend(papers)

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
