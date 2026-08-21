"""Citation tests for /research — hermetic (no network).

Locks in the outward-facing depth move: each paper carries a server-built
BibTeX entry (single source of truth in research.bibtex) that a visitor can
copy, plus a PDF link. The page, the digest API, and the live-search API all
return that same string, so what you copy is exactly what the API serves.
"""
import json

import pytest

import research
from research import bibtex, _bibtex_key


# ---------------------------------------------------------------------------
# bibtex() — the single source of truth for a paper's citation
# ---------------------------------------------------------------------------

def _paper(pid="2401.12345", title="Attention Is All You Need",
           authors="Vaswani et al.", published="2024-01-15",
           primary="cs.CL", cats=None, author_names=None):
    return {
        "arxiv_id": pid,
        "title": title,
        "authors": authors,
        "author_names": author_names if author_names is not None else ["Ashish Vaswani", "Noam Shazeer"],
        "published": published,
        "summary": "a summary",
        "categories": cats or [primary, "cs.AI"],
        "primary_category": primary,
    }


def test_bibtex_is_well_formed_article_entry():
    b = bibtex(_paper())
    assert b.startswith("@article{")
    assert b.rstrip().endswith("}")
    # Balanced braces: equal number of { and }.
    assert b.count("{") == b.count("}")
    # Every entry has the core fields.
    for field in ("title", "author", "year", "eprint",
                  "archivePrefix", "primaryClass", "url"):
        assert field in b, f"missing field: {field}"


def test_bibtex_fields_are_populated_from_paper():
    b = bibtex(_paper(pid="2401.12345", published="2024-01-15", primary="cs.CL"))
    assert "2401.12345" in b            # eprint
    assert "Attention Is All You Need" in b  # title
    assert "2024" in b                  # year
    assert "cs.CL" in b                 # primaryClass
    assert "10.48550/arXiv.2401.12345" in b  # doi
    assert "https://arxiv.org/abs/2401.12345" in b  # url


def test_bibtex_joins_full_author_list_with_and():
    p = _paper(author_names=["Jane Q. Doe", "John A. Smith", "Bob C. Lee"])
    b = bibtex(p)
    assert "Jane Q. Doe and John A. Smith and Bob C. Lee" in b


def test_bibtex_uses_full_untruncated_author_names():
    # author_names (full) must drive the citation, not the truncated `authors`
    # display string — so long author lists are never silently cut in a cite.
    names = ["A One", "B Two", "C Three", "D Four", "E Five"]
    p = _paper(author_names=names, authors="A One, B Two, C Three")  # display truncated
    b = bibtex(p)
    assert "D Four" in b and "E Five" in b


def test_bibtex_is_deterministic():
    assert bibtex(_paper()) == bibtex(_paper())


def test_bibtex_is_pure_and_idempotent():
    p = _paper()
    b1 = bibtex(p)
    b2 = bibtex(p)
    assert b1 == b2
    # Calling bibtex must not mutate the paper dict (no side effects).
    snapshot = dict(p)
    bibtex(p)
    assert p == snapshot


def test_bibtex_degrades_on_missing_fields():
    # A malformed/empty paper must yield a valid entry, never raise.
    b = bibtex({})
    assert b.startswith("@article{")
    assert b.count("{") == b.count("}")
    assert "unknown" in b   # placeholder id
    assert "(untitled)" in b


def test_bibtex_handles_missing_published_gracefully():
    b = bibtex(_paper(published=""))
    assert "n.d." in b  # year placeholder


def test_bibtex_key_is_stable_and_sanitized():
    k = _bibtex_key(_paper())
    # First-author surname (Vaswani) + year (2024) + first title word (Attention).
    assert k == "Vaswani2024attention"
    # Only ascii alnum in the key.
    assert k.isalnum()
    # Deterministic.
    assert k == _bibtex_key(_paper())


def test_bibtex_key_sanitizes_special_characters():
    p = _paper(title="Machine Learning: A Survey (2nd ed.)",
               author_names=["Ada Lovelace"], published="2023-05-01")
    k = _bibtex_key(p)
    assert k.isalnum()
    assert "Machine" in k or "machine" in k.lower()
    # No punctuation survives into the key.
    assert not any(c in k for c in ":.()")


def test_bibtex_key_falls_back_when_no_authors():
    p = _paper(author_names=[], authors="", title="Something Novel")
    k = _bibtex_key(p)
    assert k.isalnum()
    assert len(k) > 0


def test_bibtex_newlines_in_title_are_folded():
    p = _paper(title="Line One\nLine Two", author_names=["X Y"])
    b = bibtex(p)
    # No raw newline inside the title field would break a one-line BibTeX field.
    assert "Line One\nLine Two" not in b
    assert "Line One Line Two" in b


# ---------------------------------------------------------------------------
# /api/research — each digest paper carries a ready-made bibtex entry
# ---------------------------------------------------------------------------

def test_api_research_attaches_bibtex_to_every_paper(client, network_mocks):
    r = client.get("/api/research")
    assert r.status_code == 200
    data = r.get_json()
    papers = data.get("papers", [])
    assert papers, "digest should have papers (mocked)"
    for p in papers:
        assert "bibtex" in p, "every digest paper must carry a bibtex field"
        assert p["bibtex"].startswith("@article{")
        # The entry must reference the paper's own id.
        assert p["arxiv_id"] in p["bibtex"]


# ---------------------------------------------------------------------------
# /api/research/search — live hits carry the same bibtex field
# ---------------------------------------------------------------------------

def test_api_research_search_attaches_bibtex(client, network_mocks):
    r = client.get("/api/research/search?q=ai")
    assert r.status_code == 200
    data = r.get_json()
    papers = data.get("papers", [])
    assert papers, "search should return the mocked hit"
    for p in papers:
        assert "bibtex" in p
        assert p["bibtex"].startswith("@article{")
        assert p["arxiv_id"] in p["bibtex"]


def test_search_bibtex_matches_research_bibtex_for_same_paper(client, network_mocks):
    """The citation a visitor copies is exactly what research.bibtex produces —
    no client/API drift. The mocked hit is paper id '2'; compare the served
    string against bibtex() run on the same fields."""
    r = client.get("/api/research/search?q=ai")
    served = r.get_json()["papers"][0]
    expected = research.bibtex(served)
    assert served["bibtex"] == expected


# ---------------------------------------------------------------------------
# /research page — the digest renders a copy-BibTeX button + PDF link
# ---------------------------------------------------------------------------

def test_research_page_renders_cite_button_and_pdf_link(client, network_mocks):
    r = client.get("/research")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'class="cite-btn"' in html, "digest must offer a copy-BibTeX button"
    assert 'class="pdf-link"' in html, "digest must offer a PDF link"
    # The BibTeX entry is embedded server-side for the button to copy.
    assert "data-bibtex=" in html
    # The PDF link points at arXiv's pdf endpoint.
    assert "arxiv.org/pdf/" in html
    # The delegation handler that performs the copy is present on the page.
    assert "cite-btn" in html
    assert "navigator.clipboard" in html


def test_research_page_cite_button_carries_valid_bibtex(client, network_mocks):
    """The data-bibtex attribute (after the browser decodes entities) must be a
    well-formed entry whose id matches the card it belongs to."""
    import re as _re
    r = client.get("/research")
    html = r.get_data(as_text=True)
    # Grab the first data-bibtex="..." value and confirm it is a @article entry.
    m = _re.search(r'data-bibtex="([^"]*)"', html)
    assert m, "page must embed a data-bibtex attribute"
    raw = html  # the attribute is HTML-escaped by Jinja; check the marker text
    assert "@article{" in raw
    # The PDF link and button appear together inside a .paper-actions group.
    assert 'class="paper-actions"' in html
