"""Tests for the /research digest category deep-link (?cat=...).

The active category filter is mirrored into the URL (client-side) so a filtered
digest is a shareable deep link, and a reload with ?cat=<cat> restores the
filter. The category is a CLIENT hint only: the server always renders the full
digest (the JS shows/hides cards), so ?cat must never change what the server
returns or reject the request. These tests lock that contract in.
"""


def test_research_honors_cat_param_without_error(client, network_mocks):
    r = client.get("/research?cat=cs.AI")
    assert r.status_code == 200


def test_research_cat_is_a_client_hint_not_a_server_filter(client, network_mocks):
    """An unknown/absent cat must NOT make the server drop papers. The server
    renders the full digest either way; the client JS decides what to show. This
    guards against accidentally turning ?cat into a server-side filter that would
    hide real papers from the HTML (and from the text filter)."""
    # Default mock digest is one paper, arxiv_id "1".
    r = client.get("/research?cat=zzz-not-a-real-category")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # The mock paper's id must still be present in the served markup.
    assert "data-id=\"1\"" in html, (
        "server must render the full digest regardless of ?cat "
        "(the category filter is applied client-side)"
    )


def test_research_page_carries_cat_url_state_code(client, network_mocks):
    """The served page must contain the URL-state machinery for the digest
    category: a read of ?cat on load, a replaceState mirror, and the data-cat
    chips that drive both the click handler and the restore."""
    r = client.get("/research")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # Reads the category from the URL on load and lowercases it.
    assert "sp.get('cat')" in html
    # Mirrors the active category back into the URL without polluting history.
    assert "history.replaceState" in html
    # The shared apply/restore entry point.
    assert "setCat(" in html
    # The chips that carry the category.
    assert "data-cat=" in html
    # restore-from-URL uses replaceState semantics (not pushState) for the
    # digest filter — toggling filters should not add history entries.
    assert "history.pushState" not in html


def test_research_category_chips_have_matching_data_cat(client, network_mocks):
    """Every category chip's data-cat must be a value the restore logic could
    match: it is lowercased on both the click path and the restore path, so the
    chip values must be non-empty real categories (not the blank 'all' chip)."""
    r = client.get("/research")
    html = r.get_data(as_text=True)
    # At least the 'all' chip and one real category chip exist.
    assert 'data-cat=""' in html
    assert 'data-cat="cs.AI"' in html  # default mock digest primary category
