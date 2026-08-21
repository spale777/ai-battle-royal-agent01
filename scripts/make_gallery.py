#!/usr/bin/env python3
"""Render the workbench gallery PNGs once and write them to static/gallery/.

Run from the project root:
    .venv/bin/python scripts/make_gallery.py

This is a build step, not part of the web app. The output is committed so that
/gallery serves pre-rendered images and matplotlib never runs inside a request.
Because every snippet in compute.GALLERY_EXAMPLES is deterministic (fixed
random.seed or no randomness), re-running this produces identical PNGs — which
is what lets us verify the committed images match the snippets shown on the page.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import compute  # noqa: E402

OUT_DIR = ROOT / "static" / "gallery"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for ex in compute.GALLERY_EXAMPLES:
        png = compute.render_gallery_png(ex["code"])
        dest = OUT_DIR / (ex["key"] + ".png")
        dest.write_bytes(png)
        written.append((ex["key"], len(png)))
        print("wrote %-12s %6d bytes" % (ex["key"], len(png)))
    # Sanity: every entry produced a non-empty PNG.
    assert all(b > 1000 for _, b in written), "a gallery PNG looks empty"
    print("\n%d gallery images -> %s" % (len(written), OUT_DIR))


if __name__ == "__main__":
    main()
