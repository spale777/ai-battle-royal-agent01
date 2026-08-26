"""The /api/health worker-kill bound.

The hazard: /api/health re-runs every endpoint in-process, including the
networked ones (arXiv search, the egress probe, the research digest on cache
expiry, and the platform stats/notebook). Their worst-case latency once summed
to ~95s — over 3x gunicorn's default 30s worker timeout — so a slow arXiv day
would let a single /api/health call (polled by /dashboard every 60s) exceed the
timeout and gunicorn would SIGKILL the worker mid-check.

The fix has two prongs, each locked in here:
  * a WALL-CLOCK BUDGET: once the budget is spent, the remaining networked
    checks are reported as `skipped` (not run, not failed) so a health call can
    never approach the worker timeout.
  * a per-worker SHORT-TTL CACHE for the networked probes: the dashboard's
    recurring poll reuses the last probe result instead of re-hitting the
    network every time.
Plus a lowered arXiv socket timeout (research.ARXIV_FETCH_TIMEOUT, 15s -> 5s)
so a single in-flight check is hard-bounded under the budget.

All tests are hermetic: network is mocked (network_mocks) or the budget is
shrunken so the networked checks never actually run.
"""
import research as research_module


# --- the wall-clock budget: a health call can never exceed the worker timeout --
def test_budget_exceeded_skips_networked_checks_not_fails(client, network_mocks, app_module):
    """With the budget shrunken to ~0, the health check must STOP running
    networked probes once the budget is spent and report them as `skipped` —
    not `failed`. The in-process checks (git / file / JSONL / pygount) still
    run and pass. This is the core of the worker-kill fix: a slow external
    dependency degrades to visible data, never to a killed worker."""
    # Shrink the budget to ~0 so the budget is already spent by the time the
    # first networked check is reached (uptime/commits/timeline run first).
    import pytest
    monkeypatch_budget = app_module._API_HEALTH_BUDGET_SECONDS
    try:
        app_module._API_HEALTH_BUDGET_SECONDS = 0.0
        data = client.get("/api/health").get_json()
    finally:
        app_module._API_HEALTH_BUDGET_SECONDS = monkeypatch_budget

    n_net = len(app_module._HEALTH_NETWORKED)
    # Every networked check was skipped (none served from cache — reset per test).
    assert data["skipped"] == n_net, data
    assert data["budget_exceeded"] is True
    # Nothing actually failed: skipping is not a failure, so `ok` stays True.
    assert data["failed"] == 0, data
    assert data["ok"] is True
    # The invariant: every check is passed, failed, or skipped.
    assert data["passed"] + data["failed"] + data["skipped"] == data["checked"]
    # Each skipped item is flagged and names the budget (visible, not silent).
    skipped_items = [e for e in data["endpoints"] if e.get("skipped")]
    assert len(skipped_items) == n_net
    for e in skipped_items:
        assert "budget" in (e["error"] or "").lower()
    # The in-process checks still ran (their routes are present and not skipped).
    in_proc_routes = {"/api/uptime", "/api/commits", "/api/codebase"}
    present = {e["route"] for e in data["endpoints"]}
    assert in_proc_routes <= present
    for r in in_proc_routes:
        item = next(e for e in data["endpoints"] if e["route"] == r)
        assert not item.get("skipped")


def test_normal_budget_runs_all_networked_and_stays_green(client, network_mocks, app_module):
    """With a normal budget and mocked network, every check runs (nothing
    skipped) and the surface is fully green — the fix must not hide healthy
    endpoints behind skips on the happy path."""
    data = client.get("/api/health").get_json()
    assert data["skipped"] == 0
    assert data["budget_exceeded"] is False
    assert data["failed"] == 0
    assert data["ok"] is True
    assert data["passed"] == data["checked"]


# --- the per-worker short-TTL cache: the recurring poll does not re-probe ------
def test_networked_probe_runs_once_then_served_from_cache(client, network_mocks, app_module, monkeypatch):
    """Call /api/health twice back-to-back. The networked probe must run ONCE
    and the second call must be served from the per-worker cache (flagged
    `cached`) — proving the dashboard's 60s poll does not re-hit the network
    every time. A call counter makes the 'runs once' claim concrete."""
    calls = {"n": 0}

    def counting_egress(*a, **k):
        calls["n"] += 1
        return {"reachable": True, "url": "https://www.google.com/generate_204",
                "status": 204, "proxy_configured": True, "message": "ok"}

    monkeypatch.setattr(research_module, "check_egress", counting_egress)

    r1 = client.get("/api/health").get_json()
    r2 = client.get("/api/health").get_json()

    # The egress probe was executed exactly once, across both calls.
    assert calls["n"] == 1, "egress probe must run once, then be served from cache"
    # The second call's egress item is flagged as cached (and still ok).
    egress2 = next(e for e in r2["endpoints"] if e["route"] == "/api/egress")
    assert egress2.get("cached") is True
    assert egress2["ok"] is True
    # The first call's egress item was NOT cached (it did the real probe).
    egress1 = next(e for e in r1["endpoints"] if e["route"] == "/api/egress")
    assert not egress1.get("cached")
    # Both calls still report a green, fully-checked surface.
    assert r1["ok"] is True and r1["failed"] == 0
    assert r2["ok"] is True and r2["failed"] == 0


def test_failed_networked_probe_is_not_cached(client, network_mocks, app_module, monkeypatch):
    """A FAILING networked probe must be re-probed on the next call (not cached
    as a green) so a recovery is never hidden. Here egress fails; both calls
    must report it failed and actually re-run the probe."""
    calls = {"n": 0}

    def failing_egress(*a, **k):
        calls["n"] += 1
        raise OSError("name resolution failed (simulated dead network)")

    monkeypatch.setattr(research_module, "check_egress", failing_egress)

    r1 = client.get("/api/health").get_json()
    r2 = client.get("/api/health").get_json()

    # The probe ran on BOTH calls (a failure is never cached).
    assert calls["n"] == 2, "a failing probe must be re-probed, not cached"
    for r in (r1, r2):
        egress = next(e for e in r["endpoints"] if e["route"] == "/api/egress")
        assert egress["ok"] is False
        assert not egress.get("cached")
        # It counted as a real failure (not a skip).
        assert not egress.get("skipped")
    assert r1["failed"] >= 1 and r2["failed"] >= 1
    assert r1["ok"] is False and r2["ok"] is False


# --- the lowered arXiv socket timeout ------------------------------------------
def test_arxiv_fetch_timeout_is_bounded_under_budget(app_module):
    """A single in-flight networked probe must be hard-bounded well under the
    health budget (which is itself under gunicorn's 30s worker timeout), because
    the budget check runs BETWEEN probes and cannot interrupt one already in
    flight. The arXiv socket timeout caps the fetch; get_research_digest
    fast-fails after the FIRST failed category (research.py), so even a fully
    dead arXiv costs one 5s fetch in the digest probe — not four."""
    # One arXiv fetch (the per-check ceiling) must fit the budget with room
    # for the other checks that run around it.
    assert research_module.ARXIV_FETCH_TIMEOUT < app_module._API_HEALTH_BUDGET_SECONDS, (
        f"one in-flight arXiv fetch ({research_module.ARXIV_FETCH_TIMEOUT}s) "
        f"would eat the whole health budget "
        f"({app_module._API_HEALTH_BUDGET_SECONDS}s)"
    )
    # The digest fast-fails on the first failed category, so its in-flight
    # ceiling is ONE fetch timeout, not len(CATEGORIES) of them.
    digest_ceiling = research_module.ARXIV_FETCH_TIMEOUT
    assert digest_ceiling < app_module._API_HEALTH_BUDGET_SECONDS
    # ...which is set well under gunicorn's default 30s worker timeout.
    assert app_module._API_HEALTH_BUDGET_SECONDS < 30
    # And the timeout itself is a sensible, non-trivial value.
    assert 0 < research_module.ARXIV_FETCH_TIMEOUT <= 10


def test_health_response_shape(client, app_module, network_mocks):
    """The bounded health response carries the new observability fields (the
    wall time, the skip count, the budget flag) in addition to the original
    keys — so a monitor can see the bound operating, not just its absence."""
    data = client.get("/api/health").get_json()
    for key in ("ok", "checked", "passed", "failed", "skipped", "budget_exceeded",
                "wall_ms", "budget_ms", "endpoints", "at"):
        assert key in data, "missing key %r" % key
    assert isinstance(data["wall_ms"], int)
    assert isinstance(data["budget_ms"], int)
    assert data["budget_ms"] == app_module._API_HEALTH_BUDGET_SECONDS * 1000


# make the budget-shrink test's pytest import explicit for linters
import pytest  # noqa: E402,F401
