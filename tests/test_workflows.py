"""Tests for curated analysis workflows (compute.WORKFLOWS + /api/compute/workflows
+ the /sandbox page wiring).

Workflows are the outward-facing, multi-step face of the Python Workbench: a
small analysis pipeline whose steps reference the PREVIOUS step's variables
(the REPL session memory). The contract these tests lock in:

  * workflows() is pure and deterministic (no matplotlib, no execution, no
    file I/O) — cheap to call per request, trivial to diff;
  * each step carries a share-link token that round-trips to its EXACT code
    (so any single step can be opened in the workbench and reproduced);
  * a later step genuinely depends on an earlier step's variables: run the
    steps in order carrying state forward and they all succeed, but a later
    step run ALONE (fresh session) fails with a NameError — proof the steps
    are a pipeline, not a bag of independent snippets;
  * the whole pipeline, run the way the page runs it (sequential POSTs to
    /api/compute carrying `state`), completes every step ok and produces the
    figures the steps plot;
  * /api/compute/workflows is well-shaped and in the health surface;
  * /sandbox renders the workflows server-side (cards, steps, code, run/edit
    buttons, #w= deep-link handling, and the shared renderer).

The dependency tests execute real code through run_compute, so they use the
same isolated-subprocess path as production (kept in a small number of cases).
"""
import compute
from compute import run_compute


# ---- workflows() shape & determinism (pure, no execution) -------------------
def test_workflows_shape():
    wfs = compute.workflows()
    assert len(wfs) >= 3  # a single workflow would not be a plural feature
    for wf in wfs:
        assert set(wf) >= {"key", "title", "description", "step_count", "steps", "deep_link"}
        assert isinstance(wf["step_count"], int)
        assert wf["step_count"] >= 2  # multi-step is the whole point
        assert len(wf["steps"]) == wf["step_count"]
        assert wf["deep_link"] == "/sandbox#w=" + wf["key"]
        for i, st in enumerate(wf["steps"], 1):
            assert set(st) >= {"index", "name", "note", "code", "token", "chars", "workbench"}
            assert st["index"] == i
            assert st["code"].strip()
            assert st["token"], "every step must have a share-link token"
            assert st["workbench"] == "/sandbox#c=" + st["token"]
            assert st["chars"] == len(st["code"].strip())


def test_workflows_deterministic():
    a = compute.workflows()
    b = compute.workflows()
    assert a == b
    # tokens are stable across calls (encode_share is a pure function)
    assert [st["token"] for wf in a for st in wf["steps"]] == \
           [st["token"] for wf in b for st in wf["steps"]]


def test_workflows_tokens_roundtrip_to_exact_code():
    for wf in compute.workflows():
        for st in wf["steps"]:
            decoded = compute.decode_share(st["token"])
            assert decoded is not None
            assert decoded == st["code"].strip(), "token must reproduce the step byte-for-byte"


def test_workflow_keys_are_stable_and_unique():
    keys = [wf["key"] for wf in compute.workflows()]
    assert len(keys) == len(set(keys))
    # The page anchors + deep links are built from these; keep the curated set.
    assert set(keys) == {"eda", "primes", "zeta"}


def test_workflow_steps_use_only_sandbox_surface():
    """The curated code must not reach for anything outside the sandbox's
    pre-loaded modules / restricted plt (no import, no open, no file I/O)."""
    banned = ["import ", "open(", "os.", "socket", "subprocess", "__import__",
              "eval(", "exec(", "compile(", "savefig", "urllib", "requests"]
    for wf in compute.workflows():
        for st in wf["steps"]:
            for word in banned:
                assert word not in st["code"], \
                    "%s.%s uses banned construct %r" % (wf["key"], st["name"], word)


# ---- the pipeline property: steps DEPEND on earlier steps' state ------------
def test_primes_step2_requires_step1_state():
    wfs = {wf["key"]: wf for wf in compute.workflows()}
    s1 = wfs["primes"]["steps"][0]["code"]
    s2 = wfs["primes"]["steps"][1]["code"]
    # Alone, step 2 references `primes` which does not exist yet.
    alone = run_compute(s2)
    assert alone["ok"] is False
    assert "NameError" in (alone.get("exception") or "") or "NameError" in (alone.get("error") or "")
    # After step 1 (carrying its state), step 2 succeeds and surfaces the gaps.
    r1 = run_compute(s1)
    assert r1["ok"] is True
    r2 = run_compute(s2, state=r1["state"])
    assert r2["ok"] is True
    assert r2["results"], "step 2 must surface the gap distribution"


def test_eda_step2_requires_step1_state():
    wfs = {wf["key"]: wf for wf in compute.workflows()}
    s1 = wfs["eda"]["steps"][0]["code"]
    s2 = wfs["eda"]["steps"][1]["code"]
    alone = run_compute(s2)
    assert alone["ok"] is False
    assert "NameError" in (alone.get("exception") or "") or "NameError" in (alone.get("error") or "")
    r1 = run_compute(s1)
    r2 = run_compute(s2, state=r1["state"])
    assert r2["ok"] is True
    assert r2["images"], "step 2 must plot step 1's sample"


def test_zeta_step2_requires_step1_state():
    wfs = {wf["key"]: wf for wf in compute.workflows()}
    s1 = wfs["zeta"]["steps"][0]["code"]
    s2 = wfs["zeta"]["steps"][1]["code"]
    alone = run_compute(s2)
    assert alone["ok"] is False
    assert "NameError" in (alone.get("exception") or "") or "NameError" in (alone.get("error") or "")
    r1 = run_compute(s1)
    r2 = run_compute(s2, state=r1["state"])
    assert r2["ok"] is True


def test_full_pipeline_completes_every_step():
    """Run each workflow the way the page runs it: sequential runs carrying the
    session state forward. Every step must succeed, and the steps that plot
    must actually return figures."""
    for wf in compute.workflows():
        state = {}
        n_images = 0
        for st in wf["steps"]:
            r = run_compute(st["code"], state=state)
            assert r["ok"] is True, "%s step %d failed: %s" % (wf["key"], st["index"], r.get("error"))
            if r.get("state"):
                state = r["state"]
            n_images += len(r.get("images", []))
        # Each curated workflow contains at least one plotting step.
        assert n_images >= 1, "%s should produce at least one figure" % wf["key"]


# ---- the API surface --------------------------------------------------------
def test_api_compute_workflows_shape(client):
    d = client.get("/api/compute/workflows").get_json()
    assert d["count"] >= 3
    assert isinstance(d["workflows"], list)
    assert len(d["workflows"]) == d["count"]
    assert {w["key"] for w in d["workflows"]} == {"eda", "primes", "zeta"}
    # The API shape must match the pure function's output exactly.
    assert d["workflows"] == compute.workflows()


def test_api_compute_workflows_in_health(client):
    d = client.get("/api/health").get_json()
    routes = [e["route"] for e in d["endpoints"]]
    assert "/api/compute/workflows" in routes


def test_capabilities_exposes_workflows(client):
    d = client.get("/api/compute/capabilities").get_json()
    assert d.get("workflows", {}).get("enabled") is True
    assert d["workflows"]["endpoint"] == "/api/compute/workflows"


# ---- the page wiring --------------------------------------------------------
def test_sandbox_renders_workflows(client):
    html = client.get("/sandbox").get_data(as_text=True)
    for key in ("eda", "primes", "zeta"):
        assert 'id="workflow-%s"' % key in html
    # Run + edit buttons are present and carry the right data attributes.
    assert 'class="wf-run" type="button" data-wf="eda"' in html
    assert 'data-wf="primes" data-step="1"' in html
    # Step code is rendered server-side (the client reads it from <code>).
    assert "workflow-step-code" in html
    # Deep-link handling is wired client-side.
    assert "#w=" in html
    # Shared renderer is used for per-step output.
    assert "buildResultHtml" in html
    assert "wireResultEl" in html


def test_sandbox_deep_link_reacts_to_hashchange(client):
    """The #w=<key> deep link must work the SAME in-page as on a cold load.

    A fragment-only change (#w=eda -> #w=primes) is a same-document navigation:
    the page does NOT reload, so the load-time IIFE alone can never fire again.
    The advertised "shareable link" must therefore also listen for `hashchange`
    and run the newly-named workflow — otherwise a visitor already on /sandbox
    who moves between deep links (or uses back/forward) scrolls to the card but
    silently gets no analysis. This locks that wiring in so the advertised
    behavior and the shipped behavior agree.
    """
    html = client.get("/sandbox").get_data(as_text=True)
    # The hash -> run bridge is named and present.
    assert "activateWorkflowFromHash" in html
    # It is registered as a hashchange listener (the actual fix), not just
    # called once at load.
    assert "addEventListener('hashchange', activateWorkflowFromHash)" in html
    # And it is still invoked once on cold load (the new-tab share-link path).
    assert html.count("activateWorkflowFromHash()") >= 1
    # The listener must route through the same guarded runner (so an in-flight
    # pipeline is never double-fired) rather than re-implementing the run.
    assert "runWorkflow(m[1])" in html
