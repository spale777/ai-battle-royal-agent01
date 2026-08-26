"""Canonical x/y-tick handling — the ONE rule shared by BOTH render paths.

The workbench reaches matplotlib two ways: the live sandbox child (the
_HARNESS_SOURCE template, run as a subprocess) and the gallery build
(render_gallery_png, in-process). The two used to wrap ax.set_xticks
differently, and a curated workflow's 11-bars-vs-10-labels slip crashed one
path while the other tolerated it. The fix makes compute._canonical_ticks()
the single tick rule: the gallery path calls it directly, and the child gets a
verbatim copy of its source spliced into _HARNESS at import time.

These tests lock that in:
  * the rule itself (matching counts -> labels applied; mismatch -> dropped);
  * the child's embedded copy is the parent's source VERBATIM (so a future
    edit to the parent propagates to the child automatically, and a drift
    between the two is a test failure, not a runtime surprise);
  * the spliced harness still compiles;
  * the real regression: a mismatched-label run through the ACTUAL sandbox
    subprocess renders a figure instead of crashing (the exact s30 bug).
"""
import inspect

import compute
from compute import run_compute


# --- the rule itself (pure) -----------------------------------------------
def test_canonical_ticks_matching_counts_applies_labels():
    assert compute._canonical_ticks([0, 1, 2], ["a", "b", "c"]) == ([0, 1, 2], ["a", "b", "c"])


def test_canonical_ticks_mismatch_drops_labels():
    # 3 positions, 2 labels: labels are dropped, positions still set.
    assert compute._canonical_ticks([0, 1, 2], ["a", "b"]) == ([0, 1, 2],)


def test_canonical_ticks_no_labels():
    assert compute._canonical_ticks([0, 1, 2]) == ([0, 1, 2],)


def test_canonical_ticks_normalizes_sequences():
    # range/tuple inputs come back as plain lists, in both outputs.
    assert compute._canonical_ticks(range(3), tuple("abc")) == ([0, 1, 2], ["a", "b", "c"])


def test_canonical_ticks_is_pure_and_no_matplotlib():
    # The helper must not IMPORT matplotlib — it is the shared pure core that
    # both paths (and this test) can call without a rendering environment.
    # (The docstring may *mention* matplotlib; a real import is what we bar.)
    src = inspect.getsource(compute._canonical_ticks)
    assert "import matplotlib" not in src
    assert "from matplotlib" not in src


# --- the splice: child == parent, verbatim ---------------------------------
def test_child_harness_embeds_parent_canonical_ticks_verbatim():
    """The anti-drift core of the fix: the child's copy of the rule is the
    parent's source itself, not a second hand-maintained version. If someone
    edits _canonical_ticks() in the parent, this (and the child) both change
    together; if the splice is removed, this fails at collection time."""
    src = inspect.getsource(compute._canonical_ticks)
    assert src in compute._HARNESS


def test_harness_placeholder_fully_consumed():
    # No un-substituted placeholder may survive into the child program.
    assert "@_CANONICAL_TICKS@" not in compute._HARNESS
    # ...but it must still be the thing the template is built from.
    assert "@_CANONICAL_TICKS@" in compute._HARNESS_SOURCE


def test_harness_still_compiles_after_splice():
    # A broken splice is a SyntaxError in the child's program; catch it here
    # instead of at a visitor's first figure.
    code = compile(compute._HARNESS, "<harness>", "exec")
    assert code is not None


def test_gallery_path_uses_the_same_rule():
    """render_gallery_png's shim must route xticks/yticks through the SAME
    compute._canonical_ticks (checked by source, since rendering a PNG here
    would pull in matplotlib in the test process)."""
    import inspect as _i
    src = _i.getsource(compute.render_gallery_png)
    assert "_canonical_ticks(ticks, labels)" in src
    # The gallery shim no longer forwards raw *a straight to ax.set_xticks.
    assert "ax.set_xticks(*a" not in src


# --- the real regression, through the ACTUAL sandbox subprocess ------------
def test_mismatched_tick_labels_render_instead_of_crashing():
    """The s30 bug, end-to-end: 11 bars with 10 labels used to raise in the
    child (set_xticks got a 3rd positional and rejected it). Now the labels are
    dropped and the figure still renders. This is the real subprocess path —
    the whole point of the finding was that a mock never surfaces it."""
    code = (
        "buckets = [3, 2, 1, 4, 2, 3, 1, 2, 2, 3, 1]\n"
        "labels = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j']\n"
        "plt.bar(range(len(buckets)), buckets)\n"
        "plt.xticks(range(len(buckets)), labels)\n"   # 11 ticks, 10 labels
        "plt.title('mismatched labels must not crash')\n"
        "plt.show()\n"
        "print('rendered ok')\n"
    )
    r = run_compute(code)
    assert r["ok"] is True, r
    assert r["timed_out"] is False
    assert "rendered ok" in r["out"]
    assert len(r["images"]) == 1, "the figure must still come back"
    assert r["images"][0]["data_url"].startswith("data:image/png;base64,")


def test_matched_tick_labels_still_apply():
    """The matching case must keep working: 10 bars, 10 labels -> labels
    applied, figure renders. (Guards the tolerant path from eating the happy
    path.)"""
    code = (
        "buckets = [3, 2, 1, 4, 2, 3, 1, 2, 2, 3]\n"
        "labels = ['0-9', '10-19', '20-29', '30-39', '40-49',\n"
        "          '50-59', '60-69', '70-79', '80-89', '90-99']\n"
        "plt.bar(range(len(buckets)), buckets)\n"
        "plt.xticks(range(len(buckets)), labels, rotation=45, ha='right')\n"
        "plt.show()\n"
        "print('matched ok')\n"
    )
    r = run_compute(code)
    assert r["ok"] is True, r
    assert "matched ok" in r["out"]
    assert len(r["images"]) == 1
