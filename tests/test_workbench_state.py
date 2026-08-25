"""Tests for workbench session state (carry data variables between runs).

The capability: a snippet can reference the previous run's variables, so the
workbench is a REPL for exploratory analysis, not a one-shot. The contract these
tests lock in:

  * a variable defined in run N is available in run N+1 (the data flows
    server -> client -> server; the server itself stores nothing);
  * persistence is JSON-EXACT: a value is carried only if it can be re-created
    bit-identically in a fresh interpreter (no pickle, so no code can survive);
  * non-persistable names (user functions, sets, bytes, NaN/inf, oversize) are
    dropped AND reported by name in `state_dropped` -- never lost silently and
    never truncated into a wrong value;
  * a client cannot shadow `plt` / `show` / a pre-loaded module with state;
  * a run killed by the resource limits returns the inbound session unchanged
    (its variables did not change);
  * the state surface is exposed at /api/compute/capabilities and the page is
    wired (memory panel + localStorage + sending state with each run).

Kept hermetic: no network.
"""
import datetime
import decimal
import json

import compute
from compute import run_compute


# ---- the core REPL contract -------------------------------------------------

def test_state_carry_between_runs():
    """The defining property: run 2 sees run 1's variable."""
    r1 = run_compute("data = [1, 2, 3, 4]")
    assert r1["ok"] is True
    assert r1["state"] == {"data": [1, 2, 3, 4]}
    assert r1["state_dropped"] == []
    r2 = run_compute("print('sum', sum(data))", state=r1["state"])
    assert r2["ok"] is True
    assert "sum 10" in r2["out"]


def test_state_is_a_full_repl_session():
    """A multi-step analysis: load -> transform -> analyze across runs."""
    r1 = run_compute("vals = [2, 4, 4, 4, 5, 5, 7, 9]\n")
    r2 = run_compute("mean = statistics.mean(vals)\n", state=r1["state"])
    r3 = run_compute("print(mean, vals.count(4))", state=r2["state"])
    assert r3["ok"] is True
    # statistics.mean returns int 5 (exact division) -> "5 3"; the point is the
    # value and the list both survived the boundary.
    assert r3["out"].split() == ["5", "3"]


def test_state_without_state_is_fresh():
    """No state in -> NameError, not a crash; session stays empty."""
    r = run_compute("print(data)", state=None)
    assert r["ok"] is False
    assert "NameError" in (r["exception"] or "")
    assert r["state"] == {}


def test_state_empty_by_default():
    r = run_compute("print(1)")
    assert r["ok"] is True
    assert r["state"] == {}
    assert r["state_dropped"] == []


# ---- exactness --------------------------------------------------------------

def test_state_float_is_bit_exact():
    """0.1 does not become a rounded value: the re-created float is == 0.1."""
    r1 = run_compute("x = 0.1")
    r2 = run_compute("print(x == 0.1)", state=r1["state"])
    assert r2["ok"] is True
    assert "True" in r2["out"]


def test_state_exact_types_datetime_date_timedelta_decimal():
    """The non-JSON types come back as the SAME type (tagged codec), and equal."""
    r1 = run_compute(
        "d = datetime.datetime(2026, 8, 25, 6, 30, 45, 123456, tzinfo=datetime.timezone.utc)\n"
        "day = datetime.date(2026, 8, 25)\n"
        "span = datetime.timedelta(days=1, seconds=90, microseconds=7)\n"
        "price = decimal.Decimal('0.1')\n"
    )
    assert r1["ok"] is True
    st = r1["state"]
    assert set(st) == {"d", "day", "span", "price"}
    r2 = run_compute(
        "print(type(d).__name__, type(day).__name__, type(span).__name__, type(price).__name__)\n"
        "print(d.isoformat(), day.isoformat(), price == decimal.Decimal('0.1'), span.days)",
        state=st)
    assert r2["ok"] is True
    out = r2["out"]
    assert "datetime date timedelta Decimal" in out
    assert "2026-08-25T06:30:45.123456+00:00" in out
    assert "2026-08-25" in out
    assert "True" in out and " 1" in out


def test_state_nested_structures_exact():
    r1 = run_compute("t = {'a': [1, 2.5, 'x', None, True], 'b': {2: 'two'}}\n")
    r2 = run_compute("print(t['a'], t['b'])", state=r1["state"])
    assert r2["ok"] is True
    assert "[1, 2.5, 'x', None, True]" in r2["out"]
    assert "{'2': 'two'}" in r2["out"]


def test_state_tuple_round_trips_as_list():
    """A tuple is carried as a list (JSON has no tuples); that is the exact
    JSON form and is documented behaviour, not silent data loss."""
    r1 = run_compute("pt = (3, 4)\n")
    assert r1["ok"] is True
    assert r1["state"]["pt"] == [3, 4]


# ---- honesty: non-persistable names are dropped AND reported ----------------

def test_state_user_function_is_dropped_and_reported():
    r1 = run_compute("def double(x):\n    return x * 2\nkept = 1\n")
    assert r1["ok"] is True
    assert "double" in r1["state_dropped"]
    assert "kept" in r1["state"] and "double" not in r1["state"]
    # And it is genuinely gone from the next run (no code survived).
    r2 = run_compute("print(double(2))", state=r1["state"])
    assert r2["ok"] is False
    assert "NameError" in (r2["exception"] or "")


def test_state_non_persistable_values_are_dropped_and_reported():
    r1 = run_compute(
        "blob = b'bytes'\nnanv = float('nan')\ns = {1, 2, 3}\nkeep = 7\n"
    )
    assert r1["ok"] is True
    for name in ("blob", "nanv", "s"):
        assert name in r1["state_dropped"], name
        assert name not in r1["state"]
    assert r1["state"] == {"keep": 7}


def test_state_inbound_nested_value_is_decoded_and_uses():
    """Inbound state is decoded (containers) and then usable; an inbound value
    that is not JSON-exact degrades rather than injecting something odd."""
    r = run_compute("print(sum(row))", state={"row": [1, 2, 3]})
    assert r["ok"] is True
    assert "6" in r["out"]
    # Inbound a value tagged as a datetime wrapper: decodes to a real datetime.
    r2 = run_compute(
        "print(type(day).__name__, day.year, day.month, day.day)",
        state={"day": {"$a01:type": "date", "$a01:value": "2026-08-25"}})
    assert r2["ok"] is True
    assert "date 2026 8 25" in r2["out"]


# ---- anti-shadow: state can never clobber the reserved surface --------------

def test_state_cannot_shadow_plt():
    r = run_compute("print(type(plt).__name__)", state={"plt": "EVIL"})
    assert r["ok"] is True
    assert "SimpleNamespace" in r["out"]  # plt is the real restricted namespace
    assert "plt" in r["state_dropped"]
    # And a follow-up can still plot with it.
    r2 = run_compute("plt.plot([0, 1], [1, 0]); plt.show()", state={"plt": "EVIL"})
    assert r2["ok"] is True
    assert len(r2.get("images", [])) == 1


def test_state_cannot_shadow_a_module():
    r = run_compute("print(math.pi)", state={"math": "EVIL"})
    assert r["ok"] is True
    assert "3.14" in r["out"]
    assert "math" in r["state_dropped"]


def test_state_cannot_shadow_show():
    r = run_compute("show(123)", state={"show": "EVIL"})
    assert r["ok"] is True
    assert r["results"] == [123]
    assert "show" in r["state_dropped"]


# ---- bounded: oversize state is dropped, not ballooned ----------------------

def test_state_depth_cap_drops_too_deep_value():
    deep = [1]
    for _ in range(60):  # far past MAX_STATE_DEPTH
        deep = [deep]
    r = run_compute("print('ok')", state={"deep": deep, "shallow": 1})
    assert r["ok"] is True
    assert "deep" in r["state_dropped"]
    assert r["state"] == {"shallow": 1}


def test_state_items_cap_drops_oversize_container():
    big = list(range(compute.MAX_STATE_ITEMS + 500))  # more than the cell cap
    r = run_compute("print('ok')", state={"big": big, "small": 2})
    assert r["ok"] is True
    assert "big" in r["state_dropped"]
    assert r["state"] == {"small": 2}


# ---- robustness: a killed run keeps the session unchanged -------------------

def test_state_preserved_when_run_is_killed():
    prior = {"data": [1, 2, 3]}
    r = run_compute("x = 0\nwhile True:\n    x += 1", state=prior, wall_timeout=2)
    assert r["ok"] is False
    assert r["timed_out"] is True
    assert r["state"] == prior  # the session is returned exactly as it entered


# ---- API surface -------------------------------------------------------------

def test_api_compute_state_roundtrip(client):
    resp1 = client.post("/api/compute", json={"code": "vals = [10, 20, 30]"})
    assert resp1.status_code == 200
    d1 = resp1.get_json()
    assert d1["ok"] is True
    assert d1["state"] == {"vals": [10, 20, 30]}
    resp2 = client.post("/api/compute", json={"code": "print('max', max(vals))", "state": d1["state"]})
    assert resp2.status_code == 200
    d2 = resp2.get_json()
    assert d2["ok"] is True
    assert "max 30" in d2["out"]


def test_api_compute_state_fields_present_on_all_paths(client):
    # Success
    d = client.post("/api/compute", json={"code": "a = 1"}).get_json()
    assert "state" in d and "state_dropped" in d
    # Snippet error
    d = client.post("/api/compute", json={"code": "1/0"}).get_json()
    assert "state" in d and "state_dropped" in d
    # Empty code
    d = client.post("/api/compute", json={"code": "   "}).get_json()
    assert "state" in d and "state_dropped" in d


def test_api_compute_inbound_state_not_a_dict_is_ignored(client):
    resp = client.post("/api/compute", json={"code": "print('ok')", "state": ["not", "a", "dict"]})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_api_compute_oversize_state_is_413(client):
    # A dict whose JSON form exceeds MAX_STATE_BYTES is refused cheaply.
    big = {"k%d" % i: "v" * 100 for i in range(8000)}
    assert len(json.dumps(big)) > compute.MAX_STATE_BYTES
    resp = client.post("/api/compute", json={"code": "print('ok')", "state": big})
    assert resp.status_code == 413
    assert resp.get_json()["note"] == "state_too_large"


def test_api_compute_inbound_reserved_name_is_dropped(client):
    resp = client.post("/api/compute", json={"code": "print(type(plt).__name__)", "state": {"plt": 42}})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["ok"] is True
    assert "SimpleNamespace" in d["out"]
    assert "plt" in d["state_dropped"]


# ---- capabilities + page wiring ---------------------------------------------

def test_capabilities_exposes_state_surface(client):
    data = client.get("/api/compute/capabilities").get_json()
    assert "state" in data
    st = data["state"]
    assert st["enabled"] is True
    assert "client-carried" in st["mechanism"]
    assert "JSON-only" in st["persistence"]
    assert st["storage"].startswith("none")  # the server keeps nothing
    limits = st["limits"]
    assert limits["max_bytes"] == compute.MAX_STATE_BYTES
    assert limits["max_items"] == compute.MAX_STATE_ITEMS
    assert limits["max_depth"] == compute.MAX_STATE_DEPTH


def test_sandbox_page_has_memory_wiring(client):
    """The session-memory UI is client-side JS + a panel; a hermetic test asserts
    the wiring must be present in the served page and its CSS, so a regression
    that strips the feature is caught without a browser."""
    html = client.get("/sandbox").get_data(as_text=True)
    assert 'id="compute-memory"' in html            # the panel
    assert 'id="compute-memory-clear"' in html       # the Clear button
    assert 'id="compute-memory-names"' in html       # the variable list
    assert "function loadComputeState" in html       # localStorage restore
    assert "function saveComputeState" in html       # localStorage persist
    assert "function updateMemoryPanel" in html      # render names + dropped
    assert "state: computeState" in html             # runCompute sends the state
    assert "a01_workbench_state_v1" in html          # the storage key
    css = client.get("/static/style.css").get_data(as_text=True)
    assert ".compute-memory" in css
    assert ".compute-memory-names" in css
