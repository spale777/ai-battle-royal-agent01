"""Tests for the workbench example gallery (compute.GALLERY_EXAMPLES + /gallery +
/api/compute/gallery).

The gallery is the outward-facing face of the Python Workbench: pre-rendered
figures that show what the tool computes and plots, each deep-linking into the
live workbench via its share link. The correctness contract these tests lock in:

  * gallery_examples() is pure and deterministic (no matplotlib, no file I/O).
  * every example's share token round-trips to its exact snippet, so
    "open in workbench" reproduces what the visitor sees.
  * every snippet is deterministic (seeded or no randomness) — this is what
    makes a pre-rendered PNG a faithful stand-in for a live run.
  * the committed PNG assets exist and are valid, so the page never shows a
    broken image.
  * /gallery renders (server-side) and /api/compute/gallery is well-shaped.

No test here runs the real workbench subprocess or imports matplotlib — those
are exercised separately in test_compute.py.
"""
import base64
import struct
from pathlib import Path

import compute


# --- gallery_examples() (pure, deterministic) --------------------------------
def test_gallery_examples_shape_and_count():
    exs = compute.gallery_examples()
    assert len(exs) == len(compute.GALLERY_EXAMPLES)
    assert len(exs) >= 3  # a gallery of one would not be a gallery
    keys = {e["key"] for e in exs}
    for e in exs:
        for field in ("key", "title", "caption", "code", "token", "png",
                      "workbench", "chars"):
            assert field in e, "missing field %r" % field
        assert e["key"] in keys
        assert e["code"].strip(), "example has empty code"
        assert e["chars"] == len(e["code"].strip())


def test_gallery_examples_tokens_roundtrip_to_exact_code():
    """The whole point of a gallery card: its link must reproduce the snippet."""
    for e in compute.gallery_examples():
        assert e["token"] is not None
        assert compute.decode_share(e["token"]) == e["code"]
        assert e["workbench"] == "/sandbox#c=" + e["token"]
        assert e["png"] == "/static/gallery/%s.png" % e["key"]


def test_gallery_examples_are_deterministic():
    """Two calls must agree token-for-token — a stale PNG must not drift."""
    a = [e["token"] for e in compute.gallery_examples()]
    b = [e["token"] for e in compute.gallery_examples()]
    assert a == b
    assert len(set(a)) == len(a), "two examples share a token"


def test_gallery_examples_snippets_are_deterministic():
    """Every snippet either avoids random or fixes random.seed — otherwise a
    pre-rendered PNG is not a faithful stand-in for a live run."""
    for e in compute.GALLERY_EXAMPLES:
        code = e["code"]
        uses_random = "random." in code
        if uses_random:
            assert "random.seed(" in code, (
                "%s uses random.* without a fixed seed — its figure would not be "
                "reproducible, so a pre-rendered PNG could not match a live run"
                % e["key"])


# --- committed PNG assets ----------------------------------------------------
def test_gallery_png_assets_exist_and_are_valid():
    """Guard the committed images against loss — a deleted PNG shows as a
    broken <img> on the page with no server error to catch it."""
    root = Path(compute.BASE)
    for e in compute.GALLERY_EXAMPLES:
        p = root / "static" / "gallery" / (e["key"] + ".png")
        assert p.exists(), "missing gallery PNG for %s" % e["key"]
        b = p.read_bytes()
        assert b[:8] == b"\x89PNG\r\n\x1a\n", "%s is not a valid PNG" % p.name
        w, h = struct.unpack(">II", b[16:24])
        assert w >= 200 and h >= 200, "%s looks too small (%dx%d)" % (p.name, w, h)


# --- HTTP surface -----------------------------------------------------------
def test_api_compute_gallery_shape(client):
    r = client.get("/api/compute/gallery")
    assert r.status_code == 200
    data = r.get_json()
    assert data["count"] == len(compute.GALLERY_EXAMPLES)
    assert len(data["examples"]) == data["count"]
    for e in data["examples"]:
        assert e["code"] and e["token"] and e["png"]
        assert compute.decode_share(e["token"]) == e["code"]


def test_gallery_page_renders_examples_server_side(client):
    r = client.get("/gallery")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for e in compute.gallery_examples():
        # The pre-rendered image and the live-workbench deep link are both in
        # the HTML (server-rendered, not built by client JS).
        assert 'src="%s"' % e["png"] in html, "missing <img> for %s" % e["key"]
        assert 'href="%s"' % e["workbench"] in html, "missing workbench link for %s" % e["key"]
        assert e["title"] in html
    assert "gallery-grid" in html
    assert "Open in workbench" in html
    # The snippet code is present (escaped) for each card.
    for e in compute.gallery_examples():
        assert 'id="gcode-%s"' % e["key"] in html


def test_gallery_in_nav_and_sitemap(client):
    home = client.get("/").get_data(as_text=True)
    assert 'href="/gallery"' in home, "/gallery is missing from the nav"
    sm = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/gallery" in sm


def test_gallery_page_has_csp_safe_wiring(client):
    """The per-card interactivity (code toggle, click-to-zoom) is client JS, so
    the test asserts the wiring is present; a regression that drops it silently
    strips the feature with no server error."""
    html = client.get("/gallery").get_data(as_text=True)
    assert "gallery-code-toggle" in html
    assert "querySelectorAll('.gallery-card')" in html
    css = client.get("/static/style.css").get_data(as_text=True)
    assert ".gallery-grid" in css
    assert ".gallery-card" in css
    assert ".gallery-code" in css
