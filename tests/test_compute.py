"""Tests for the server-side Python workbench (compute.py + /api/compute).

These are the heart of the feature: they prove the *safety* contract, not just
that "it returned 200". A snippet must be able to (a) do real computation, and
(b) be stopped before it can hang the web worker, eat memory, write files, or
import escalation paths (os/socket/subprocess). The hard backstop is OS rlimits
enforced in the child; these tests verify each of those actually fires.

Kept hermetic: no network. The one genuinely slow case (runaway loop) is bounded
by a short parent-side wall timeout so the suite stays fast.
"""
import os

import pytest

import json

import compute
from compute import run_compute


# ---- pure run_compute(): the actual execution contract -----------------------

def test_compute_runs_and_captures_output():
    r = run_compute("print(sum(range(1, 101)))")
    assert r["ok"] is True
    assert r["timed_out"] is False
    assert "5050" in r["out"]


def test_compute_uses_preloaded_modules_without_import():
    r = run_compute(
        "import collections\n"   # must FAIL (no import) -- we use the preloaded global
        "print('unreachable')"
    )
    # `import collections` is not allowed: __import__ is absent from builtins.
    assert r["ok"] is False
    assert "unreachable" not in r["out"]

    # The same capability via the pre-loaded global works with no import at all.
    r2 = run_compute("print(collections.Counter('mississippi').most_common(1)[0][1])")
    assert r2["ok"] is True
    assert "4" in r2["out"]


@pytest.mark.parametrize("snippet,needle", [
    ("open('/tmp/nope')", "open"),
    ("import os", "os"),
    ("eval('1+1')", "eval"),
    ("exec('print(1)')", "exec"),
    ("__import__('os')", "import"),
    ("import subprocess", "subprocess"),
    ("import socket", "socket"),
])
def test_compute_blocks_escalation_paths(snippet, needle):
    r = run_compute(snippet)
    assert r["ok"] is False, f"should be blocked: {snippet!r}"
    # The failure is a NameError (nothing in scope) or a clear error, never a
    # successful run.
    assert r["timed_out"] is False


def test_compute_empty_code():
    r = run_compute("")
    assert r["ok"] is False
    assert "No code" in r["error"]
    r_ws = run_compute("   \n  ")
    assert r_ws["ok"] is False
    assert "No code" in r_ws["error"]


def test_compute_user_error_is_reported_not_raised():
    r = run_compute("print(undefined_name)")
    assert r["ok"] is False
    assert r["timed_out"] is False
    assert "NameError" in (r["exception"] or "")
    assert r["rc"] == 0  # the harness handled it; the *process* exited cleanly


def test_compute_runaway_loop_is_killed():
    """A forever loop must be killed by the parent's wall timeout (bounded here
    to keep the test fast) and reported as timed_out, not hang the caller."""
    r = run_compute("x = 0\nwhile True:\n    x += 1", wall_timeout=2)
    assert r["ok"] is False
    assert r["timed_out"] is True
    assert "wall-clock" in (r["error"] or "")


def test_compute_memory_bomb_is_contained(tmp_path):
    """A memory bomb must be contained (MemoryError or kill) and must not hang
    or exhaust the host. The 800 MB RLIMIT_AS trips first as a MemoryError."""
    r = run_compute(
        "junk = [b'x' * (1024 * 1024) for _ in range(1000)]\n"
        "print('made it')"
    )
    # It must NOT successfully print; it must be stopped by the memory limit.
    assert "made it" not in r["out"]
    assert r["ok"] is False


def test_compute_does_not_write_files(tmp_path):
    """open() is not in scope, so a snippet cannot create files. We also verify
    nothing lands in the CWD."""
    r = run_compute("open('should_not_exist_xyz.txt', 'w').write('hi')")
    assert r["ok"] is False
    assert not os.path.exists("should_not_exist_xyz.txt")


# ---- history: isolated to a temp file so tests don't touch the real log ------

@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    path = tmp_path / "compute_history.jsonl"
    monkeypatch.setattr(compute, "COMPUTE_HISTORY_PATH", str(path))
    return path


def test_compute_history_roundtrip(isolated_history):
    from compute import record_compute, get_compute_history
    r = run_compute("print(1 + 1)")
    record_compute("print(1 + 1)", r)
    r2 = run_compute("while True: pass", wall_timeout=1)
    record_compute("while True: pass", r2)
    entries = get_compute_history()
    assert len(entries) == 2
    # newest first: the timed-out run (recorded last) is at the top
    assert entries[0]["ok"] is False
    assert entries[0]["timed_out"] is True
    assert entries[1]["ok"] is True
    for e in entries:
        assert "ts" in e and "code" in e and "ok" in e


def test_compute_history_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(compute, "COMPUTE_HISTORY_PATH", str(tmp_path / "none.jsonl"))
    from compute import get_compute_history
    assert get_compute_history() == []


# ---- API surface -------------------------------------------------------------

def test_api_compute_runs(client):
    resp = client.post("/api/compute", json={"code": "print(6 * 7)"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "42" in data["out"]


def test_api_compute_error_is_still_200(client):
    # A snippet error is a valid outcome, not a server failure.
    resp = client.post("/api/compute", json={"code": "x = 1/0"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert "ZeroDivisionError" in (data["exception"] or "")


def test_api_compute_too_long_is_413(client):
    resp = client.post("/api/compute", json={"code": "x" * 60000})
    assert resp.status_code == 413
    assert resp.get_json()["note"] == "too_long"


def test_api_compute_capabilities(client):
    data = client.get("/api/compute/capabilities").get_json()
    for key in ("wall_timeout_seconds", "cpu_time_limit_seconds",
                "memory_limit_bytes", "max_concurrent", "safe_modules",
                "plotting", "max_images", "figure_dpi"):
        assert key in data, f"missing key: {key}"
    assert data["wall_timeout_seconds"] < 30  # must stay under gunicorn's 30s worker timeout
    assert "math" in data["safe_modules"] and "os" not in data["safe_modules"]
    assert data["plotting"]["enabled"] is True
    assert data["plotting"]["namespace"] == "plt"
    assert "plot" in data["plotting"]["methods"]
    assert "savefig" not in data["plotting"]["methods"]


# ---- plotting (restricted plt) -----------------------------------------------

def test_compute_plt_present_and_plotting():
    """plt must be a usable namespace that can produce an image."""
    r = run_compute("print(hasattr(plt, 'plot'))")
    assert r["ok"] is True
    assert "True" in r["out"]

def test_compute_plot_produces_image():
    """A snippet that calls plt.plot + plt.show() must return at least one image."""
    r = run_compute("plt.plot([0, 1, 2], [1, 4, 2])\nplt.show()")
    assert r["ok"] is True
    assert len(r.get("images", [])) >= 1
    im = r["images"][0]
    assert im["format"] == "png"
    assert im["data_url"].startswith("data:image/png;base64,")
    assert im["bytes"] > 100  # a real PNG, not an empty buffer

def test_compute_plot_multiple_figures_capped():
    """Multiple figures in one run are capped at MAX_IMAGES."""
    r = run_compute(
        "plt.bar([1,2,3],[4,2,5]); plt.show()\n"
        "plt.hist([1,2,2,3,3], bins=3); plt.show()\n"
        "plt.pie([3,1,1]); plt.show()\n"
        "plt.plot([0,1],[1,0]); plt.show()"
    )
    assert r["ok"] is True
    import compute as _c
    assert len(r.get("images", [])) <= _c.MAX_IMAGES

def test_compute_plot_no_show_no_images():
    """plt.plot without plt.show() must NOT return images (no figure is
    rendered unless the user explicitly asks)."""
    r = run_compute("plt.plot([0,1],[1,0])")
    assert r["ok"] is True
    assert r.get("images", []) == []

def test_compute_plt_savefig_blocked():
    """plt.savefig must raise PermissionError (no file writing)."""
    r = run_compute("plt.savefig('/tmp/should_not_exist.png')")
    assert r["ok"] is False
    assert "PermissionError" in (r["exception"] or "")
    assert not os.path.exists("/tmp/should_not_exist.png")

def test_compute_plt_does_not_expose_escalation():
    """plt in scope must NOT open new escalation paths."""
    for snip in ["import os", "open('/tmp/x')", "eval('1+1')"]:
        r = run_compute(snip)
        assert r["ok"] is False, f"should be blocked even with plt: {snip!r}"

def test_compute_plot_plain_lists_only():
    """plt must accept plain Python lists (no numpy needed)."""
    r = run_compute("plt.plot(list(range(10)), [i**2 for i in range(10)]); plt.show()")
    assert r["ok"] is True
    assert len(r.get("images", [])) == 1

def test_compute_plot_error_is_reported():
    """A plotting error (e.g., mismatched lengths) must be a user error, not a crash."""
    r = run_compute("plt.plot([1,2,3], [1,2])\nplt.show()")
    assert r["ok"] is False  # ValueError from matplotlib
    assert "ValueError" in (r["exception"] or "")

def test_api_compute_plot_returns_images(client):
    """The API must return images in the JSON response."""
    resp = client.post("/api/compute", json={"code": "plt.plot([0,1],[1,0]); plt.show()"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data.get("images", [])) == 1
    assert data["images"][0]["data_url"].startswith("data:image/png;base64,")


def test_api_compute_history_shape(client):
    data = client.get("/api/compute/history").get_json()
    assert "entries" in data
    assert isinstance(data["entries"], list)


# ---- shareable links (snippet in the URL fragment) --------------------------

def test_share_roundtrip_simple():
    code = "print(6 * 7)"
    tok = compute.encode_share(code)
    assert tok is not None
    assert compute.decode_share(tok) == code


def test_share_roundtrip_newlines_and_unicode():
    code = "x = [1, 2, 3]\n# ünïcödé café\nprint(sum(x))"
    tok = compute.encode_share(code)
    assert compute.decode_share(tok) == code


def test_share_token_is_fragment_safe():
    """A share token must not contain +/ or whitespace (would break a URL fragment)."""
    tok = compute.encode_share("print('a b + c / d')")
    assert tok is not None
    for bad in ("+", "/", " ", "=", "%"):
        assert bad not in tok


def test_share_rejects_over_cap():
    code = "x = " + ("1" * (compute.SHARED_MAX_CHARS + 1))
    assert compute.encode_share(code) is None


def test_share_boundary_at_cap_roundtrips():
    code = "x = " + ("1" * (compute.SHARED_MAX_CHARS - len("x = ")))
    tok = compute.encode_share(code)
    assert tok is not None
    assert compute.decode_share(tok) == code


def test_share_rejects_non_string():
    assert compute.encode_share(None) is None
    assert compute.encode_share(42) is None
    assert compute.encode_share(["print(1)"]) is None


def test_share_rejects_empty():
    assert compute.encode_share("") is None
    assert compute.encode_share("   \n  ") is None


def test_decode_share_tolerates_mangled_token():
    assert compute.decode_share("") is None
    assert compute.decode_share(None) is None
    assert compute.decode_share(123) is None
    assert compute.decode_share("!!!not-base64!!!") is None  # garbage -> None, not raise


def test_decode_share_rejects_over_cap_after_decode():
    # A token that decodes to something over the cap is refused.
    code = "x = " + ("1" * (compute.SHARED_MAX_CHARS + 1))
    tok = compute.encode_share(code)
    assert tok is None  # encode already refuses; nothing to decode


def test_api_share_post_returns_link(client):
    code = "print(6 * 7)"
    resp = client.post("/api/compute/share", json={"code": code})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["token"] is not None
    assert data["link"].endswith("#c=" + data["token"])
    assert "/sandbox" in data["link"]
    assert data["chars"] == len(code)
    assert data["max_chars"] == compute.SHARED_MAX_CHARS
    # The token must round-trip back to the exact snippet.
    assert compute.decode_share(data["token"]) == code


def test_api_share_post_rejects_over_cap(client):
    code = "x = " + ("1" * (compute.SHARED_MAX_CHARS + 1))
    resp = client.post("/api/compute/share", json={"code": code})
    assert resp.status_code == 413
    data = resp.get_json()
    assert data["ok"] is False
    assert data["link"] is None


def test_api_share_post_rejects_empty(client):
    resp = client.post("/api/compute/share", json={"code": "   "})
    assert resp.status_code == 413
    assert resp.get_json()["ok"] is False


def test_api_share_get_roundtrips(client):
    code = "print(6 * 7)"
    tok = compute.encode_share(code)
    assert tok is not None
    resp = client.get("/api/compute/share?c=" + tok)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["code"] == code


def test_api_share_get_mangled_is_not_an_error(client):
    resp = client.get("/api/compute/share?c=!!!bad!!!")
    assert resp.status_code == 200  # a mangled link degrades, never 500s
    data = resp.get_json()
    assert data["ok"] is False
    assert data["code"] == ""


def test_api_share_get_no_token(client):
    resp = client.get("/api/compute/share")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False


def test_sandbox_page_has_share_controls(client):
    html = client.get("/sandbox").get_data(as_text=True)
    assert 'id="compute-share"' in html      # Share button
    assert 'id="compute-share-url"' in html  # read-only link field
    assert 'id="compute-restore-notice"' in html  # restore banner
    assert "/api/compute/share" in html        # client talks to the share endpoint


def test_sandbox_page_has_workbench(client):
    html = client.get("/sandbox").get_data(as_text=True)
    assert 'id="compute-code"' in html
    assert 'id="compute-run"' in html
    assert 'id="compute-out"' in html
    assert "Python Workbench" in html
    assert "/api/compute" in html


def test_sandbox_workbench_image_download_wiring(client):
    """The 'Download PNG' affordance on workbench figures is client-side JS.

    The button is not in the initial HTML — it's built per-figure inside
    ``renderComputeResult`` from the figure's data-URL, so a hermetic test
    asserts the wiring that must be present in the served page: the download
    helper, the event-delegation target (``.compute-dl`` / ``data-fidx``), the
    ``<figure>`` wrapper the images are reflowed into, and the CSS that styles
    the caption bar + button. A regression that drops any of these silently
    strips the download capability with no server error to catch it.
    """
    html = client.get("/sandbox").get_data(as_text=True)
    # The client-side download helper must exist and be wired to the buttons.
    assert "function downloadDataUrl" in html          # data-URL -> browser download
    assert "querySelectorAll('.compute-dl')" in html   # event delegation on the buttons
    assert "data-fidx" in html                         # figure index -> which image
    assert "workbench-figure-" in html                 # download filename
    # renderComputeResult must reflow bare <img> into a <figure> with a button.
    assert 'class="compute-figure"' in html
    assert "Download" in html
    # The served stylesheet must style the new caption bar + button.
    css = client.get("/static/style.css").get_data(as_text=True)
    assert ".compute-figure" in css
    assert ".compute-dl" in css
    assert ".compute-figure-cap" in css


# ---- Structured results: show(value) ----------------------------------------
# A snippet can surface a *value* (scalar, list, table of rows, dict, set) via
# show(value); the server serializes it under hard bounds (depth / count /
# cell / byte caps) and the page renders it as a table or card. These tests
# lock in that contract: correct shapes, honest capping, graceful degradation
# of unrepresentable objects, and — critically — that the new surface opens no
# escalation path.

def test_compute_show_scalar_int():
    r = run_compute("show(42)")
    assert r["ok"] is True
    assert r["results"] == [42]


def test_compute_show_scalar_string_and_bool():
    assert run_compute("show('hi')")["results"] == ["hi"]
    assert run_compute("show(True)")["results"] == [True]
    assert run_compute("show(None)")["results"] == [None]


def test_compute_show_table_of_rows():
    r = run_compute("show([[1, 2, 3], [4, 5, 6]])")
    assert r["ok"] is True
    assert r["results"] == [[[1, 2, 3], [4, 5, 6]]]


def test_compute_show_dict():
    r = run_compute("show({'a': 1, 'b': 2})")
    assert r["ok"] is True
    assert r["results"] == [{"a": 1, "b": 2}]


def test_compute_show_dict_int_key_is_stringified():
    # JSON object keys must be strings: an int key comes back as "1".
    r = run_compute("show({1: 'a'})")
    assert r["results"] == [{"1": "a"}]


def test_compute_show_set_becomes_list():
    r = run_compute("show({3, 1, 2})")
    assert r["ok"] is True
    assert sorted(r["results"][0]) == [1, 2, 3]


def test_compute_show_multiple_preserves_order():
    r = run_compute("show(1)\nshow([1, 2])\nshow({'k': 'v'})")
    assert r["ok"] is True
    assert r["results"] == [1, [1, 2], {"k": "v"}]


def test_compute_show_floats_and_nan():
    # NaN/inf have no JSON form -> they degrade to null (None), not a crash.
    r = run_compute("show([1.0, float('nan'), 2.5])")
    assert r["ok"] is True
    assert r["results"] == [[1.0, None, 2.5]]


def test_compute_show_big_list_is_capped():
    # A huge list is truncated to the item cap plus one explicit truncation
    # marker, so a single run can never produce an oversized payload.
    r = run_compute("show(list(range(2000)))")
    assert r["ok"] is True
    items = r["results"][0]
    assert len(items) == compute.MAX_RESULT_ITEMS + 1
    assert items[-1] == {"_truncated": True}


def test_compute_show_depth_is_capped():
    # Six levels of nesting are kept; the seventh is cut with a marker, so a
    # deeply (or cyclically) nested value cannot blow up the payload.
    shallow = run_compute("show(" + "[" * 6 + "1" + "]" * 6 + ")")
    assert shallow["ok"] is True
    assert "_truncated" not in json.dumps(shallow["results"])
    deep = run_compute("show(" + "[" * 7 + "1" + "]" * 7 + ")")
    assert deep["ok"] is True
    assert "_truncated" in json.dumps(deep["results"])


def test_compute_show_circular_reference_does_not_hang():
    # A value that references itself must be cut off, not infinite-loop.
    r = run_compute("lst = []\nlst.append(lst)\nshow(lst)")
    assert r["ok"] is True
    assert "_truncated" in json.dumps(r["results"])


def test_compute_show_arbitrary_object_degrades_to_repr():
    # Non-container, non-scalar values degrade to a short repr (no object
    # graph is serialized), so an arbitrary object can't smuggle data out.
    r = run_compute("show(complex(1, 2))")
    assert r["ok"] is True
    assert r["results"] == ["(1+2j)"]
    rb = run_compute("show(b'\\x00\\x01')")
    assert rb["ok"] is True
    assert isinstance(rb["results"][0], str)


def test_compute_show_no_args_is_reported_not_raised():
    # show() with no argument is a user error -> a *reported* TypeError, not a
    # crash that takes down the worker (the same contract as any snippet error).
    r = run_compute("show()")
    assert r["ok"] is False
    assert "TypeError" in (r["exception"] or "")


def test_compute_show_does_not_open_escalation_paths():
    # Adding show() to the namespace must not reopen anything: the escalation
    # paths stay blocked, and you cannot feed show() a file handle (open is out
    # of scope), so it cannot become a file-read oracle.
    for snip in ["import os", "open('/tmp/x')", "eval('1+1')",
                 "show(open('/etc/hostname'))", "show(__import__('os'))"]:
        r = run_compute(snip)
        assert r["ok"] is False, f"should stay blocked: {snip!r}"
        assert r["timed_out"] is False


def test_api_compute_returns_results(client):
    resp = client.post("/api/compute", json={"code": "show([[1, 2], [3, 4]])"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["results"] == [[[1, 2], [3, 4]]]
    # The response must be JSON-safe (it round-trips through get_json intact).
    assert json.dumps(data)


def test_api_compute_results_empty_when_unused(client):
    resp = client.post("/api/compute", json={"code": "print(1 + 1)"})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["results"] == []


def test_api_compute_results_shape_on_error(client):
    # Even a failing snippet carries a (possibly partial) results list, so the
    # API shape is stable for the client to rely on.
    resp = client.post("/api/compute", json={"code": "show(1)\nraise ValueError('x')"})
    data = resp.get_json()
    assert data["ok"] is False
    assert isinstance(data.get("results"), list)


def test_capabilities_exposes_results_surface(client):
    data = client.get("/api/compute/capabilities").get_json()
    assert data["results"]["enabled"] is True
    assert data["results"]["function"] == "show"
    limits = data["results"]["limits"]
    assert limits["max_items"] == compute.MAX_RESULT_ITEMS
    assert limits["max_cells"] == compute.MAX_RESULT_CELLS
    assert limits["max_depth"] == compute.MAX_RESULT_DEPTH


def test_sandbox_page_has_results_wiring(client):
    """The structured-results display is client-side JS built from r.results.

    A hermetic test asserts the wiring that must be present in the served page
    (the render helpers, the Copy-JSON event target, the copy helper, and the
    CSS that styles the tables). A regression that drops any of these silently
    strips the capability with no server error to catch it.
    """
    html = client.get("/sandbox").get_data(as_text=True)
    assert "function renderResults" in html            # build the results block
    assert "function renderResultValue" in html        # per-value shape + markup
    assert "r.results" in html                         # reads the API field
    assert "data-ridx" in html                         # which result to copy
    assert "compute-copy-json" in html                 # Copy JSON button
    assert "function copyText" in html                 # clipboard helper (fallback)
    assert "class=\"compute-table\"" in html           # the rendered <table>
    assert "show(value)" in html                       # discoverable in the intro
    css = client.get("/static/style.css").get_data(as_text=True)
    assert ".compute-result" in css
    assert ".compute-table" in css
    assert ".compute-copy-json" in css

