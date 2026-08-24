"""Tests for the unified /research view deep link (?q=...&cat=...).

/research has TWO independent client-side views, each with its own URL state:
the live arXiv search (?q=&field=&sort=&page=) and the digest category filter
(?cat=). Each is mirrored into the URL by its own IIFE. The failure mode these
tests guard against is the two IIFEs stomping each other's query string: each
syncUrl must start from the CURRENT location.search and only touch its own
keys, so a visitor can hold BOTH a live search result set and a
category-filtered digest at once, in ONE shareable link that reproduces both
on load. (Before the fix, the search IIFE rebuilt the URL from an empty
params object, silently wiping ?cat= — a combined deep link could never
restore the digest filter.)
"""


def test_combined_view_link_is_served_and_cat_stays_a_client_hint(client, network_mocks):
    """A link with BOTH a search query and a digest category is served fine,
    and the server still renders the full digest (the category is a client
    hint, never a server-side filter, regardless of ?q)."""
    r = client.get("/research?q=diffusion&cat=cs.AI")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # The mock digest paper must be present even though ?q is also set.
    assert 'data-id="1"' in html, (
        "server must render the full digest for a combined ?q&cat link "
        "(the category filter is applied client-side)"
    )
    # Both restore paths are wired on the same page.
    assert "sp.get('q')" in html    # search restore reads the query
    assert "sp.get('cat')" in html  # category restore reads the filter


def test_both_url_syncers_merge_the_current_query_string(client, network_mocks):
    """The stomp regression: BOTH the search IIFE's and the category IIFE's
    syncUrl must build on top of the CURRENT location.search (so each sees
    the other's keys), not from a blank params object. Before the fix only
    the category IIFE merged — the search one wiped ?cat= on every search."""
    r = client.get("/research")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    merges = html.count("new URLSearchParams(location.search)")
    assert merges >= 2, (
        f"expected both URL syncers to merge the current query string "
        f"(found {merges} occurrence(s) of "
        f"'new URLSearchParams(location.search)'; the search IIFE was "
        f"building from an empty params object and stomping other keys)"
    )


def test_search_url_syncer_owns_only_its_keys_and_deletes_defaults(client, network_mocks):
    """The search IIFE's syncUrl sets q/field/sort/page — and EXPLICITLY
    deletes field/sort/page when they are back at their defaults — so a
    reset search leaves no stale params, while every foreign key (cat) is
    left untouched (it is never .set/.delete'd in that function)."""
    r = client.get("/research")
    html = r.get_data(as_text=True)
    assert "params.set('q', query)" in html
    for key in ("field", "sort", "page"):
        assert f"params.delete('{key}')" in html, (
            f"search syncUrl must explicitly delete default {key}="
            f"params instead of leaving stale state in the URL"
        )
    # The category syncer must likewise only touch its own key.
    assert "sp.set('cat', activeCat)" in html
    assert "sp.delete('cat')" in html


def test_search_restore_reads_field_sort_page(client, network_mocks):
    """A ?q deep link restores the FULL search view — query plus the
    field/sort/page selection — so the URL state is complete, not just the
    query string."""
    r = client.get("/research")
    html = r.get_data(as_text=True)
    assert "sp.get('field')" in html
    assert "sp.get('sort')" in html
    assert "sp.get('page')" in html
