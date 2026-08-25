"""compute.py — an isolated, resource-limited Python workbench.

Submit a short Python snippet; it executes in a *separate* interpreter (a
subprocess) under hard OS resource limits, and we return stdout / any traceback.

Design and threat model
-----------------------
This is an **exploration sandbox**, not a security boundary against a determined
attacker. The honest framing, in order of strength:

1. **Process isolation.** The user code never runs inside a gunicorn worker. It
   runs in a fresh ``python -I`` (isolated mode) child, so a crash, ``sys.exit``,
   or runaway does not take down the web app. A hung/over-limit child is killed
   by a wall-clock timeout the *parent* controls (well under gunicorn's default
   30s worker timeout, so the worker is only ever blocked for ~6s).

2. **Hard OS backstop.** The child sets ``RLIMIT_CPU`` (SIGKILL on exceed),
   ``RLIMIT_AS`` (total address space — stops memory bombs), ``RLIMIT_FSIZE``
   (caps any file write at 1 MiB), and ``RLIMIT_NPROC`` (stops fork bombs). These
   are enforced by the kernel, not by the code, so a snippet cannot reason its
   way around them.

3. **Restricted namespace (defense-in-depth / UX).** The snippet runs in a
   globals dict with a curated set of builtins and a handful of safe stdlib
   modules pre-imported (``math``, ``random``, ``statistics``, ``collections``,
   ``itertools``, ``string``, ``textwrap``, ``fractions``, ``decimal``,
   ``functools``, ``json``, ``re``, ``datetime``), plus a **restricted
   matplotlib plotting shim** (as ``plt``). ``open``, ``input``, ``eval``,
   ``exec``, ``compile`` and ``__import__`` are not exposed, and ``os`` /
   ``socket`` / ``subprocess`` / ``shutil`` / ``numpy`` are not in scope. This
   keeps normal work one-liner-friendly and blocks the obvious escalation paths
   — but the OS limits above are what actually bound the blast radius.

   **Plotting:** ``plt.plot`` / ``plt.scatter`` / ``plt.bar`` / ``plt.hist`` /
   ``plt.pie`` / ``plt.boxplot`` / ``plt.stem`` create matplotlib figures on the
   non-interactive Agg backend. Instead of opening a window, ``plt.show()``
   returns every figure as a PNG **base64 data-URL** in the result's ``images``
   field, so a snippet can *compute and visualize* and the page renders the
   image. It is a deliberately small surface (no subplots, no file saving, no
   animation), and matplotlib only enters scope *after* the rlimits are set —
   so a runaway plot is just another killable child, not a hole.

   **Structured results:** ``show(value)`` surfaces a *value* — a number, a
   list, a table of rows (a list of equal-length lists), a dict, or a set — so
   the page can render it as a table, the analogue of a REPL echoing the last
   expression. The value passes through a bounded serializer (depth / count /
   cell / byte caps) that degrades unrepresentable objects to a short ``repr``;
   it is a display path, not a security boundary, and it reads a value the
   snippet already produced — it never reads a file.

Blast radius: a low-privilege ``agent`` user on an isolated machine that cannot
reach the other agents or the outside world. That is the honest ceiling of what
this can be; we do not pretend otherwise.
"""

import base64
import datetime
import json
import math
import os
import resource
import re
import signal
import subprocess
import sys
import tempfile
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(BASE, ".venv", "bin", "python")
COMPUTE_HISTORY_PATH = os.path.join(BASE, "compute_history.jsonl")

# --- Resource limits (the hard backstop) ------------------------------------
WALL_TIMEOUT_SECONDS = 6        # parent kill deadline; must stay < gunicorn 30s
CPU_TIME_LIMIT_SECONDS = 5      # RLIMIT_CPU hard limit -> SIGKILL
MEMORY_LIMIT_BYTES = 800 * 1024 * 1024      # RLIMIT_AS (total address space)
FILE_SIZE_LIMIT_BYTES = 1024 * 1024         # RLIMIT_FSIZE (cap any single write)
MAX_PROCESSES = 64                       # RLIMIT_NPROC (fork-bomb cap)
MAX_OUTPUT_CHARS = 20000        # truncate captured output
MAX_CONCURRENT = 2              # max simultaneous compute children
MAX_IMAGES = 3                  # at most 3 figures per run (keep responses small)
MAX_IMAGE_PIXELS = (1600, 1200) # cap per-figure resolution
FIG_DPI = 100                   # render DPI
FIG_SIZE = (7, 4.5)             # default figure size in inches
SHARED_MAX_CHARS = 4000         # cap a shareable snippet (URL length + honest limit)
# --- Structured results: what show(value) may surface as a JSON-serializable
#     value (scalar, list/tuple, dict, or set) so the page can render a table.
#     These bounds are small on purpose: they keep a single run's JSON payload
#     and the rendered table well under a sane size. Nothing here is a security
#     boundary -- it is a display cap, not a safety cap (the rlimits are that).
MAX_RESULT_DEPTH = 6            # nesting depth (list/dict/set) before we truncate
MAX_RESULT_ITEMS = 500          # max elements kept in one list/tuple/set
MAX_RESULT_KEYS = 500           # max keys kept in one dict
MAX_RESULT_CELLS = 1200         # total cells (list items + dict entries) per value
MAX_RESULT_BYTES = 64 * 1024    # max size of the serialized JSON per value

# --- Session state (carry data variables between runs) ----------------------
# A workbench session is a REPL: run 1 defines `data`, run 2 analyzes it. State
# travels client -> server -> client (the browser owns its own session; the
# server stores nothing, so a public endpoint never keeps one user's data and
# never leaks it to another). Persistence is JSON-only and EXACT: a variable is
# carried only if it can be re-created bit-identically in a fresh interpreter
# (no pickle -- no code can survive a JSON round-trip). Anything else is
# dropped and reported by name, never truncated into a silently-wrong value.
# These bounds keep a session's payload small enough to ride in an HTTP body.
MAX_STATE_DEPTH = 40          # nesting depth (list/dict) before we stop
MAX_STATE_ITEMS = 20000       # max elements kept across ALL containers
MAX_STATE_KEYS = 2000         # max keys kept across ALL dicts
MAX_STATE_BYTES = 512 * 1024  # max size of the whole session's JSON (in or out)

# The child interpreter runs isolated (-I) and executes this harness. User code
# is read from stdin. A single JSON line is written to stderr so the parent can
# recover the result even if the child is later killed mid-stream.
_HARNESS_SOURCE = r"""
import builtins as _bi
import base64, collections, contextlib as _cl, datetime, decimal, fractions
import functools, io, json, math, os, tempfile, types
import random, re, statistics, string, sys, textwrap, time, traceback

# Give matplotlib a stable, writable config dir so it (a) never warns on stderr
# (the harness reports results over stderr as one JSON line) and (b) caches its
# font list once instead of rebuilding it on every run.
os.environ.setdefault(
    "MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "mpl-agent-01"))
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (@CPU@, @CPU@))
    resource.setrlimit(resource.RLIMIT_AS, (@MEM@, @MEM@))
    resource.setrlimit(resource.RLIMIT_FSIZE, (@FSIZE@, @FSIZE@))
    resource.setrlimit(resource.RLIMIT_NPROC, (@NPROC@, @NPROC@))

_LIMITS = _limits

# Curated builtins: enough to write normal code, none of the escalation paths.
_BUILTIN_WHITELIST = [
    "abs", "all", "any", "bin", "bool", "bytearray", "bytes", "callable", "chr",
    "complex", "dict", "dir", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "getattr", "hasattr", "hash", "hex", "id", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next", "object",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round", "set",
    "slice", "sorted", "str", "sum", "tuple", "type", "zip", "ValueError",
    "TypeError", "KeyError", "IndexError", "ZeroDivisionError", "StopIteration",
    "RuntimeError", "ArithmeticError", "Exception", "NotImplementedError",
]
_builtins = {n: getattr(_bi, n) for n in _BUILTIN_WHITELIST}

# Safe stdlib modules, pre-imported and named so no `import` statement is needed.
_MODULES = dict(
    math=math, random=random, statistics=statistics, collections=collections,
    itertools=__import__("itertools"), string=string, textwrap=textwrap,
    fractions=fractions, decimal=decimal, functools=functools, json=json,
    re=re, datetime=datetime, time=time,
)

# --- Restricted plotting (matplotlib, non-interactive Agg) ---------------------
# Deliberately imported *only* if a snippet actually uses plt, and *after* the
# rlimits are set, so a runaway plot is just another killable child. Exposed as
# a small, curated surface: enough to plot computed results, none of matplotlib's
# file/network/animation reach. Works on plain Python lists -- no numpy in scope,
# so there is no path to np.load / open a file (the sandbox stays a true sandbox).
plt = types.SimpleNamespace()
plt.__doc__ = ("Restricted matplotlib: plot computed results. Data is plain Python "
               "lists/tuples. Call plt.show() to return the figures as images.")
_plt_figures = []
_plt_ready = {"flag": False}


def _init_matplotlib():
    if _plt_ready["flag"]:
        return
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib import rcParams
    rcParams["figure.dpi"] = @FIGDPI@
    plt._Figure = Figure
    _plt_ready["flag"] = True


def _new_ax():
    _init_matplotlib()
    fig = plt._Figure(figsize=@FIGSIZE@)
    ax = fig.add_subplot(111)
    _plt_figures.append((fig, ax))
    return ax


def _plot(*args, **kwargs):
    ax = _new_ax()
    return ax.plot(*args, **kwargs)


def _scatter(x, y, *args, **kwargs):
    ax = _new_ax()
    return ax.scatter(x, y, *args, **kwargs)


def _bar(x, height, *args, **kwargs):
    ax = _new_ax()
    return ax.bar(x, height, *args, **kwargs)


def _hist(data, bins=10, *args, **kwargs):
    ax = _new_ax()
    return ax.hist(data, bins=bins, *args, **kwargs)


def _pie(x, *args, **kwargs):
    ax = _new_ax()
    return ax.pie(x, *args, **kwargs)


def _boxplot(x, *args, **kwargs):
    ax = _new_ax()
    return ax.boxplot(x, *args, **kwargs)


def _stem(x, y=None, *args, **kwargs):
    ax = _new_ax()
    return ax.stem(x, y, *args, **kwargs)


def _figure(figsize=@FIGSIZE@):
    _init_matplotlib()
    fig = plt._Figure(figsize=figsize)
    ax = fig.add_subplot(111)
    _plt_figures.append((fig, ax))
    return fig


def _axes(fig=None):
    # Return the most recent axes so a figure created via plt.figure() is drawable.
    if not _plt_figures:
        return _new_ax()
    return _plt_figures[-1][1]


def _axis(*args, **kwargs):
    # ax.axis: "equal" (square aspect, e.g. for pie charts), "off", or a
    # [xmin, xmax, ymin, ymax] limits list. Safe — only sets axis properties.
    if _plt_figures:
        return _plt_figures[-1][1].axis(*args, **kwargs)


def _set_title(text, *args, **kwargs):
    if _plt_figures:
        return _plt_figures[-1][1].set_title(text, *args, **kwargs)


def _set_xlabel(label, *args, **kwargs):
    if _plt_figures:
        return _plt_figures[-1][1].set_xlabel(label, *args, **kwargs)


def _set_ylabel(label, *args, **kwargs):
    if _plt_figures:
        return _plt_figures[-1][1].set_ylabel(label, *args, **kwargs)


def _set_xticks(ticks, labels=None, **kwargs):
    if _plt_figures:
        return _plt_figures[-1][1].set_xticks(ticks, labels, **kwargs)


def _set_yticks(ticks, labels=None, **kwargs):
    if _plt_figures:
        return _plt_figures[-1][1].set_yticks(ticks, labels, **kwargs)


def _grid(visible=True, *args, **kwargs):
    if _plt_figures:
        return _plt_figures[-1][1].grid(visible, *args, **kwargs)


def _legend(*args, **kwargs):
    # Forward to ax.legend: no-arg (use existing labels), (labels,), or kwargs
    # like loc/. The old form always injected labels=None positionally, which
    # broke the common no-arg plt.legend() call.
    if _plt_figures:
        return _plt_figures[-1][1].legend(*args, **kwargs)


def _savefig(*args, **kwargs):
    # No file writing here -- open() is not in scope anyway, and this makes the
    # intent explicit: figures come back as images, never as files.
    raise PermissionError("plt.savefig is not available; use plt.show() to return images.")


def _close(fig=None):
    if _plt_figures:
        _plt_figures.clear()


def _show():
    _init_matplotlib()
    images = []
    maxpix = @MAXPIXEL@
    # Cap the *total* figures returned across every plt.show() call this run.
    for i, (fig, ax) in enumerate(_plt_figures):
        if len(_images) + len(images) >= @MAXIMAGES@:
            break
        fig.set_size_inches(maxpix[0] / @FIGDPI@, maxpix[1] / @FIGDPI@)
        out = io.BytesIO()
        fig.savefig(out, format="png")
        b = out.getvalue()
        data = base64.b64encode(b).decode("ascii")
        images.append({"format": "png", "bytes": len(b),
                       "data_url": "data:image/png;base64," + data})
    _images.extend(images)  # register so the emitted result carries them
    return images


plt.plot = _plot
plt.scatter = _scatter
plt.bar = _bar
plt.hist = _hist
plt.pie = _pie
plt.boxplot = _boxplot
plt.stem = _stem
plt.figure = _figure
plt.axes = _axes
plt.axis = _axis
plt.title = _set_title
plt.xlabel = _set_xlabel
plt.ylabel = _set_ylabel
plt.xticks = _set_xticks
plt.yticks = _set_yticks
plt.grid = _grid
plt.legend = _legend
plt.savefig = _savefig
plt.close = _close
plt.show = _show
plt._figures = _plt_figures

# Things deliberately NOT exposed: open, input, eval, exec, compile, __import__,
# globals, locals, vars, breakpoint, numpy. os/socket/subprocess/shutil/sys not
# in scope. No file or network access of any kind from a snippet.

# --- Structured results: show(value) -----------------------------------------
# A snippet can surface a *value* (a number, a list, a table of rows, a dict, a
# set) for the page to render as a table -- the analogue of what a REPL shows
# for the last expression, but explicit and capped. The serializer below is the
# ONLY way a value reaches the page: it is bounded (depth / count / cells /
# bytes) and it never serializes an arbitrary object (it degrades to repr). It
# is a display path, not a security boundary -- and it opens no escalation path
# (it reads a value a snippet already produced, never a file).
_MAXDEPTH = @MAXDEPTH@
_MAXITEMS = @MAXITEMS@
_MAXKEYS = @MAXKEYS@
_MAXCELLS = @MAXCELLS@
_MAXBYTES = @MAXBYTES@
_STATE_DEPTH = @STATEDEPTH@
_STATE_ITEMS = @STATEITEMS@
_STATE_KEYS = @STATEKEYS@
_STATE_BYTES = @STATEBYTES@


def _to_cell(x, depth=0):
    # Recursively convert a value to a JSON-serializable form under hard bounds.
    # Returns a plain JSON-able value; unrepresentable values degrade to a short
    # repr, never to their full object graph.
    _cells = [0]

    def conv(v, d):
        if v is None or isinstance(v, (bool, int, str)):
            return v
        if isinstance(v, float):
            # keep the JSON small; NaN/inf have no JSON form -> None
            if v != v or v == float("inf") or v == float("-inf"):
                return None
            return round(v, 8)
        if d >= _MAXDEPTH:
            # too deep to expand -- name the type, do not expand the object
            return {"_truncated": True, "_type": type(v).__name__}
        if isinstance(v, (list, tuple, set, frozenset)):
            out = []
            for item in v:
                if len(out) >= _MAXITEMS or _cells[0] >= _MAXCELLS:
                    out.append({"_truncated": True})
                    break
                _cells[0] += 1
                out.append(conv(item, d + 1))
            return out
        if isinstance(v, dict):
            out = {}
            for k, val in v.items():
                if len(out) >= _MAXKEYS or _cells[0] >= _MAXCELLS:
                    out["_<truncated>"] = True
                    break
                key = k if isinstance(k, int) else str(k)  # JSON wants str keys
                _cells[0] += 1
                out[key] = conv(val, d + 1)
            return out
        # non-container, non-scalar: degrade to a short repr (no object graph)
        try:
            return repr(v)[:200]
        except Exception:
            return "<unrepresentable>"

    return conv(x, depth)


def _show_result(x):
    # Register a value for the page to render. Bounded: even a huge value is
    # capped and, if its serialized form still exceeds the byte budget, replaced
    # by an honest "too large" stub rather than a giant payload.
    cell = _to_cell(x)
    try:
        if len(json.dumps(cell, default=str, separators=(",", ":"))) > _MAXBYTES:
            cell = {"_truncated": True, "_note": "value exceeds the display size limit",
                    "_type": type(x).__name__}
    except (TypeError, ValueError):
        cell = {"_truncated": True, "_note": "value could not be serialized",
                "_type": type(x).__name__}
    _results.append(cell)
    return None


def _emit(payload):
    sys.stderr.write(json.dumps(payload) + "\n")
    sys.stderr.flush()

import io, math as _math, datetime as _dt, decimal as _decimal, json as _json

# --- Session-state round-trip codec (the single authority) -------------------
# The only way a value crosses a run boundary. It round-trips through JSON with
# tagged wrappers for the non-JSON types (datetime/date/timedelta/Decimal) so a
# value reloads bit-identically in a fresh interpreter. No pickle, so no code can
# ever survive; a value we cannot recreate exactly is DROPPED and reported by
# name (never truncated into a silently-wrong number).
def _st_encode(v, d):
    if d > _STATE_DEPTH:
        raise ValueError("too deep")
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if _math.isnan(v) or _math.isinf(v):
            raise ValueError("float is not JSON-safe (NaN/inf)")
        return v
    if isinstance(v, str):
        return v
    if isinstance(v, _dt.datetime):
        return {"$a01:type": "dt", "$a01:value": v.isoformat()}
    if isinstance(v, _dt.date):
        return {"$a01:type": "date", "$a01:value": v.isoformat()}
    if isinstance(v, _dt.timedelta):
        return {"$a01:type": "td", "$a01:value": [v.days, v.seconds, v.microseconds]}
    if isinstance(v, _decimal.Decimal):
        return {"$a01:type": "dec", "$a01:value": str(v)}
    if isinstance(v, (list, tuple)):
        return [_st_encode(x, d + 1) for x in v]
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            if isinstance(k, bool) or not (isinstance(k, str) or isinstance(k, int)):
                raise ValueError("dict key must be str or int")
            # int keys are carried as their string form -- the exact JSON form
            # (JSON object keys are strings); a round-trip is stable because the
            # decoded side decodes every key as a string too.
            out[str(k)] = _st_encode(val, d + 1)
        return out
    raise ValueError("type %s" % type(v).__name__)

def _st_decode(v, d):
    if d > _STATE_DEPTH:
        raise ValueError("too deep")
    if isinstance(v, dict) and set(v.keys()) <= {"$a01:type", "$a01:value"} and "$a01:type" in v:
        t = v["$a01:type"]
        if t not in ("dt", "date", "td", "dec"):
            return v  # wrapper-shaped but unknown type: keep it a plain dict
        if t == "dt":
            return _dt.datetime.fromisoformat(v["$a01:value"])
        if t == "date":
            return _dt.date.fromisoformat(v["$a01:value"])
        if t == "td":
            return _dt.timedelta(days=v["$a01:value"][0], seconds=v["$a01:value"][1], microseconds=v["$a01:value"][2])
        if t == "dec":
            return _decimal.Decimal(v["$a01:value"])
    if isinstance(v, dict):
        return {k: _st_decode(x, d + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [_st_decode(x, d + 1) for x in v]
    return v

def _st_exact(v):
    # Return the value re-created through the JSON round-trip, or raise
    # ValueError if it is not exactly representable (wrong type, NaN/inf, too
    # deep, or its serialized form is not byte-stable). A value passes only if
    # this succeeds: persistence is exact, or it does not happen at all.
    enc = _st_encode(v, 0)
    s = _json.dumps(enc, allow_nan=False, separators=(",", ":"), ensure_ascii=False)
    if len(s) > _STATE_BYTES:
        raise ValueError("too large")
    dec = _st_decode(_json.loads(s), 0)
    s2 = _json.dumps(_st_encode(dec, 0), allow_nan=False, separators=(",", ":"), ensure_ascii=False)
    if s2 != s:
        raise ValueError("not an exact round-trip")
    return dec

def _state_value_ok(v):
    try:
        _st_exact(v)
        return True
    except Exception:
        return False

def _count_cells(v, d, depth):
    if d >= depth:
        return 1
    if isinstance(v, (list, tuple)):
        return sum(_count_cells(x, d + 1, depth) for x in v)
    if isinstance(v, dict):
        return sum(_count_cells(x, d + 1, depth) for x in v.values())
    return 1

def _count_keys(v, d, depth):
    if d >= depth or not isinstance(v, dict):
        return 0
    return sum(1 + _count_keys(x, d + 1, depth) for x in v.values())

def _collect_state(g):
    # Gather the top-level user data names from the run's namespace into the
    # next session. Only JSON-exact values carry forward; functions (user defs)
    # and anything else non-persistable are DROPPED and listed by name, so the
    # page can say exactly what did not survive instead of silently losing it.
    # Dunder and harness-internal names never cross the boundary; cells/keys are
    # capped so a single session cannot balloon past MAX_STATE_*.
    out = {}
    dropped = []
    cells = 0
    keys = 0
    for name in sorted(g):
        if name.startswith("_"):
            continue  # dunders, __name__, and any _-prefixed name are not user data
        if name in _RESERVED:
            continue  # plt / show / pre-loaded modules are harness names, never user data
        val = g[name]
        if callable(val):
            dropped.append(name)
            continue
        if not _state_value_ok(val):
            dropped.append(name)
            continue
        c = _count_cells(val, 0, _STATE_DEPTH)
        k = _count_keys(val, 0, _STATE_DEPTH)
        if cells + c > _STATE_ITEMS or keys + k > _STATE_KEYS:
            dropped.append(name)
            continue
        out[name] = _st_encode(val, 0)  # wire form: JSON-safe, decodes back exact
        cells += c
        keys += k
    return out, dropped
# Names a session may never shadow: the plotting surface, the show() helper,
# and every pre-loaded module. State is seeded *before* these are re-asserted
# and any state name in this set is dropped (and reported), so a client cannot
# clobber plt / show / a module.
_RESERVED = {"plt", "show"}
_RESERVED.update(_MODULES.keys())

code = sys.stdin.read()
_MAXOUT = @MAXOUT@

# --- Read the run envelope from stdin ---------------------------------------
# run_compute ALWAYS prefixes the stdin with a state line: base64 of the prior
# session's state JSON, or an empty line when there is no state. The snippet is
# everything after the FIRST newline. Riding it in stdin (like the code) keeps
# it out of argv and env, so an oversize session can never trip ARG_MAX at
# spawn. An empty envelope line is the "no state" marker; a malformed envelope
# line degrades to an empty session, never a crash.
_state_in = {}
if "\n" in code:
    _first, code = code.split("\n", 1)
    if _first.strip():
        try:
            _st_raw = base64.urlsafe_b64decode(_first.strip() + "===").decode("utf-8")
            _state_in = _json.loads(_st_raw)
        except Exception:
            _state_in = {}
if not isinstance(_state_in, dict):
    _state_in = {}

_state_dropped = []
_ns_seed = {}
for _k, _v in _state_in.items():
    if _k in _RESERVED:
        _state_dropped.append(_k)
        continue
    try:
        _ns_seed[_k] = _st_decode(_v, 0) if isinstance(_v, (dict, list)) else _v
    except Exception:
        _state_dropped.append(_k)

buf = io.StringIO()
_images = []
_results = []
ns = {"__builtins__": _builtins, "__name__": "__compute__"}
ns.update(_ns_seed)
ns.update(_MODULES)
ns["plt"] = plt
ns["show"] = _show_result

try:
    _LIMITS()
    with _cl.redirect_stdout(buf):
        exec(code, ns)
    _new_state, _dropped = _collect_state(ns)
    _state_dropped.extend(_dropped)
    _emit({"ok": True, "out": buf.getvalue()[:_MAXOUT], "error": None,
           "images": _images, "results": _results,
           "state": _new_state, "state_dropped": _state_dropped})
except SystemExit as _e:
    _emit({"ok": True, "out": buf.getvalue()[:_MAXOUT],
           "error": None, "note": "SystemExit(%r)" % (_e.code,),
           "images": _images, "results": _results,
           "state": {}, "state_dropped": _state_dropped})
except Exception:
    _tb = traceback.format_exc()
    _emit({"ok": False, "out": buf.getvalue()[:_MAXOUT],
           "error": _tb.strip(),
           "exception": _tb.strip().splitlines()[-1] if _tb.strip() else "",
           "images": _images, "results": _results,
           "state": {}, "state_dropped": _state_dropped})
"""

_HARNESS = (
    _HARNESS_SOURCE
    .replace("@CPU@", str(CPU_TIME_LIMIT_SECONDS))
    .replace("@MEM@", str(MEMORY_LIMIT_BYTES))
    .replace("@FSIZE@", str(FILE_SIZE_LIMIT_BYTES))
    .replace("@NPROC@", str(MAX_PROCESSES))
    .replace("@MAXOUT@", str(MAX_OUTPUT_CHARS))
    .replace("@FIGDPI@", str(FIG_DPI))
    .replace("@FIGSIZE@", repr(FIG_SIZE))
    .replace("@MAXPIXEL@", repr(MAX_IMAGE_PIXELS))
    .replace("@MAXIMAGES@", str(MAX_IMAGES))
    .replace("@MAXDEPTH@", str(MAX_RESULT_DEPTH))
    .replace("@MAXITEMS@", str(MAX_RESULT_ITEMS))
    .replace("@MAXKEYS@", str(MAX_RESULT_KEYS))
    .replace("@MAXCELLS@", str(MAX_RESULT_CELLS))
    .replace("@MAXBYTES@", str(MAX_RESULT_BYTES))
    .replace("@STATEDEPTH@", str(MAX_STATE_DEPTH))
    .replace("@STATEITEMS@", str(MAX_STATE_ITEMS))
    .replace("@STATEKEYS@", str(MAX_STATE_KEYS))
    .replace("@STATEBYTES@", str(MAX_STATE_BYTES))
)

_compute_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

# matplotlib's numpy import spawns BLAS worker threads. Under the child's
# RLIMIT_NPROC those pthread_create calls fail and the import hangs, so we force
# single-threaded BLAS in the child environment. We also pin MPLCONFIGDIR to a
# stable, writable dir so matplotlib caches its font list once and never warns on
# stderr (the child reports results over stderr as one JSON line).
_MPLCONFIG_DIR = os.path.join(tempfile.gettempdir(), "mpl-agent-01")


def _child_env():
    env = dict(os.environ)
    env.update({
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "MPLCONFIGDIR": _MPLCONFIG_DIR,
    })
    return env


# --- Gallery of worked workbench examples (pre-rendered, deterministic) ------
# Curated snippets that show what the workbench can compute AND plot. The PNGs
# are rendered ONCE by scripts/make_gallery.py (committed under static/gallery/),
# so /gallery is instant and matplotlib never runs inside a web request. Each
# entry's ``code`` is the exact snippet a visitor runs in the workbench, and
# ``token`` is its share-link fragment, so "open in workbench" reproduces it.
#
# Determinism matters: every snippet either avoids randomness or fixes
# random.seed(...), so a re-render of the gallery is byte-stable and the
# committed PNG always matches the snippet shown.
GALLERY_EXAMPLES = [
    {
        "key": "sine",
        "title": "Sine wave",
        "caption": "Sample a sine curve at 0.1-radian steps and plot it. "
                   "The workbench computes the data in plain Python — no numpy.",
        "code": (
            "xs = [x / 10 for x in range(-60, 61)]\n"
            "ys = [math.sin(x) for x in xs]\n"
            "plt.plot(xs, ys, color=\"tab:blue\", linewidth=2)\n"
            "plt.title('sin(x) for x in [-6, 6]'); plt.xlabel('x'); plt.ylabel('sin x')\n"
            "plt.grid(True, alpha=0.3)\n"
            "plt.show()\n"
            "print('samples:', len(xs), '· min', min(ys), 'max', max(ys))"
        ),
    },
    {
        "key": "primes",
        "title": "Prime density",
        "caption": "Count primes in each 100-number bucket from 1 to 1000. "
                   "A classic sieve, plotted as a bar chart.",
        "code": (
            "def is_prime(n):\n"
            "    if n < 2: return False\n"
            "    for d in range(2, int(math.sqrt(n)) + 1):\n"
            "        if n % d == 0: return False\n"
            "    return True\n"
            "buckets = [sum(1 for n in range(lo, hi + 1) if is_prime(n))\n"
            "           for lo, hi in [(i, i + 99) for i in range(1, 1001, 100)]]\n"
            "labels = ['1–100', '101–200', '201–300', '301–400', '401–500',\n"
            "          '501–600', '601–700', '701–800', '801–900', '901–1000']\n"
            "plt.bar(range(len(buckets)), buckets, color=\"tab:orange\")\n"
            "plt.xticks(range(len(buckets)), labels, rotation=45, ha='right')\n"
            "plt.title('Primes per 100, from 1 to 1000'); plt.ylabel('count')\n"
            "plt.grid(True, axis='y', alpha=0.3)\n"
            "plt.show()\n"
            "print('total primes:', sum(buckets))"
        ),
    },
    {
        "key": "boxplot",
        "title": "Box plot of three samples",
        "caption": "Three Gaussian samples (fixed seed) compared as a box plot. "
                   "Randomness is seeded, so the figure is identical every render.",
        "code": (
            "random.seed(42)\n"
            "g = [random.gauss(50, 10) for _ in range(40)]\n"
            "h = [random.gauss(62, 8) for _ in range(40)]\n"
            "j = [random.gauss(55, 16) for _ in range(40)]\n"
            "plt.boxplot([g, h, j], tick_labels=['A', 'B', 'C'])\n"
            "plt.title('Three Gaussian samples (seed 42)'); plt.ylabel('value')\n"
            "plt.grid(True, axis='y', alpha=0.3)\n"
            "plt.show()\n"
            "print('means:', round(statistics.mean(g), 1),\n"
            "      round(statistics.mean(h), 1), round(statistics.mean(j), 1))"
        ),
    },
    {
        "key": "pie",
        "title": "Proportions",
        "caption": "A pie chart of four fixed proportions. Plain lists in, a "
                   "labelled figure out.",
        "code": (
            "sizes = [42, 27, 18, 13]\n"
            "labels = ['alpha', 'beta', 'gamma', 'other']\n"
            "plt.pie(sizes, labels=labels, autopct='%1.0f%%',\n"
            "        colors=['tab:blue', 'tab:orange', 'tab:green', 'tab:red'])\n"
            "plt.title('Share of the whole')\n"
            "plt.axis('equal')\n"
            "plt.show()\n"
            "print('total:', sum(sizes))"
        ),
    },
    {
        "key": "fibonacci",
        "title": "Fibonacci spiral",
        "caption": "Walk outward by successive Fibonacci numbers, turning 90° "
                   "each step, and trace the spiral.",
        "code": (
            "a, b = 0, 1\n"
            "xs = [0]; ys = [0]\n"
            "for _ in range(30):\n"
            "    a, b = b, a + b\n"
            "    ang = math.radians(90 * (len(xs) % 4))\n"
            "    xs.append(xs[-1] + b * math.cos(ang))\n"
            "    ys.append(ys[-1] + b * math.sin(ang))\n"
            "plt.plot(xs, ys, marker='o', color='tab:purple')\n"
            "plt.title('Fibonacci spiral (first 30 terms)')\n"
            "plt.grid(True, alpha=0.3)\n"
            "plt.show()\n"
            "print('last Fibonacci value:', b)"
        ),
    },
    {
        "key": "scatter",
        "title": "Clustered scatter",
        "caption": "Two seeded Gaussian blobs in one scatter, colored per cluster. "
                   "One plotting call, one figure — the workbench's real model.",
        "code": (
            "random.seed(7)\n"
            "a = [(random.gauss(3, 1.2), random.gauss(4, 1.2)) for _ in range(60)]\n"
            "c = [(random.gauss(7, 1.2), random.gauss(6, 1.2)) for _ in range(60)]\n"
            "pts = a + c\n"
            "xs = [p[0] for p in pts]; ys = [p[1] for p in pts]\n"
            "cols = ['tab:blue'] * len(a) + ['tab:red'] * len(c)\n"
            "plt.scatter(xs, ys, c=cols, s=40, alpha=0.6)\n"
            "plt.title('Two Gaussian blobs (seed 7)'); plt.xlabel('x'); plt.ylabel('y')\n"
            "plt.grid(True, alpha=0.3)\n"
            "plt.show()\n"
            "print('points:', len(pts), '(60 blue + 60 red)')"
        ),
    },
]

# The safe stdlib the sandbox exposes to a snippet, reused here so a gallery
# snippet renders with exactly the modules the workbench gives it.
_GALLERY_MODULES = [
    "math", "random", "statistics", "collections", "itertools", "string",
    "textwrap", "fractions", "decimal", "functools", "json", "re",
]


def render_gallery_png(code, dpi=FIG_DPI, figsize=(16.0, 12.0)):
    """Render a workbench-style snippet to PNG bytes on the non-interactive Agg
    backend. Used by ``scripts/make_gallery.py`` so the committed gallery PNGs
    match their snippets exactly. It is *not* called from a web request — the
    PNGs are pre-rendered and served statically, so matplotlib never runs inside
    the app. Runs only curated, deterministic, self-authored snippets.
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib import rcParams
    rcParams["figure.dpi"] = dpi

    fig = Figure(figsize=figsize)
    ax = fig.add_subplot(111)

    class _Plt:
        """Minimal single-axes plotting shim matching the sandbox's ``plt``."""
        def plot(self, *a, **k): return ax.plot(*a, **k)
        def scatter(self, x, y, *a, **k): return ax.scatter(x, y, *a, **k)
        def bar(self, x, h, *a, **k): return ax.bar(x, h, *a, **k)
        def hist(self, d, bins=10, *a, **k): return ax.hist(d, bins=bins, *a, **k)
        def pie(self, x, *a, **k): return ax.pie(x, *a, **k)
        def boxplot(self, x, *a, **k): return ax.boxplot(x, *a, **k)
        def stem(self, x, y=None, *a, **k): return ax.stem(x, y, *a, **k)
        def title(self, *a, **k): return ax.set_title(*a, **k)
        def xlabel(self, *a, **k): return ax.set_xlabel(*a, **k)
        def ylabel(self, *a, **k): return ax.set_ylabel(*a, **k)
        def xticks(self, *a, **k): return ax.set_xticks(*a, **k)
        def yticks(self, *a, **k): return ax.set_yticks(*a, **k)
        def grid(self, *a, **k): return ax.grid(*a, **k)
        def legend(self, *a, **k): return ax.legend(*a, **k)
        def axis(self, *a, **k): return ax.axis(*a, **k)
        def savefig(self, *a, **k):
            raise PermissionError("plt.savefig is not available; use plt.show().")
        def show(self, *a, **k):
            # No-op here: the figure is saved after the snippet returns. Kept so
            # gallery snippets are identical to what a visitor runs in the sandbox.
            return [0]

    ns = {"plt": _Plt(), "print": print}
    for name in _GALLERY_MODULES:
        ns[name] = __import__(name)
    exec(compile(code, "<gallery>", "exec"), ns)

    out = io.BytesIO()
    fig.savefig(out, format="png", bbox_inches="tight")
    return out.getvalue()


def gallery_examples():
    """The workbench gallery as data: metadata + the snippet's share-link token
    + the static PNG path. Pure and deterministic — no matplotlib, no file I/O,
    so it's cheap to call per request and trivial to test. The PNGs themselves
    live under static/gallery/ and are served by the normal static handler.
    """
    out = []
    for ex in GALLERY_EXAMPLES:
        token = encode_share(ex["code"])
        out.append({
            "key": ex["key"],
            "title": ex["title"],
            "caption": ex["caption"],
            "code": ex["code"],
            "token": token,
            "chars": len(ex["code"].strip()),
            "png": "/static/gallery/%s.png" % ex["key"],
            "workbench": "/sandbox#c=" + (token or ""),
        })
    return out


# --- Curated analysis workflows (multi-step, state-carrying) ------------------
# Where the gallery shows ONE snippet, a workflow shows a SEQUENCE: a small
# analysis pipeline where each step references the variables defined by the
# previous step (the REPL session memory). The steps run live in the workbench
# (the same isolated /api/compute path — no new execution surface); the client
# simply POSTs the steps in order, carrying the session state forward, so a
# visitor sees the full load -> transform -> analyze -> plot loop, and can edit
# or fork any step. Every step is deterministic (fixed seeds / pure math) and
# uses only the sandbox's pre-loaded modules + restricted plt.
WORKFLOWS = [
    {
        "key": "eda",
        "title": "Explore a sample, step by step",
        "description": (
            "A classic exploratory pass over one seeded sample: generate it, "
            "look at its shape, check it for a drift, then collect the numbers "
            "into a report. Each step only reads the variables the previous "
            "step defined — that is the point."
        ),
        "steps": [
            {
                "name": "Generate the sample",
                "note": "Define `sample` (seeded, so it is identical every run).",
                "code": (
                    "random.seed(11)\n"
                    "sample = [random.gauss(0, 1) for _ in range(2000)]\n"
                    "show({'n': len(sample), 'mean': round(statistics.mean(sample), 3),\n"
                    "      'std': round(statistics.pstdev(sample), 3)})"
                ),
            },
            {
                "name": "Look at the shape",
                "note": "Plot the `sample` from step 1; report its quartiles.",
                "code": (
                    "s = sorted(sample)\n"
                    "q1, med, q3 = s[len(s)//4], s[len(s)//2], s[3*len(s)//4]\n"
                    "plt.hist(sample, bins=40)\n"
                    "plt.title('Step 1\\'s sample, n=%d' % len(sample))\n"
                    "plt.xlabel('value'); plt.ylabel('count'); plt.grid(True, alpha=0.3)\n"
                    "plt.show()\n"
                    "show({'q1': round(q1, 3), 'median': round(med, 3), 'q3': round(q3, 3)})"
                ),
            },
            {
                "name": "Check for drift",
                "note": "Compare the first half of `sample` to its second half.",
                "code": (
                    "half = len(sample) // 2\n"
                    "first, second = statistics.mean(sample[:half]), statistics.mean(sample[half:])\n"
                    "d = second - first\n"
                    "show({'mean(first half)': round(first, 3), 'mean(second half)': round(second, 3),\n"
                    "      'difference': round(d, 4),\n"
                    "      'reading': 'drift' if abs(d) > 0.05 else 'stable (within noise)'})"
                ),
            },
            {
                "name": "Collect the report",
                "note": "Gather everything computed so far into one table of rows.",
                "code": (
                    "show([['n', len(sample)],\n"
                    "       ['mean', round(statistics.mean(sample), 3)],\n"
                    "       ['std', round(statistics.pstdev(sample), 3)],\n"
                    "       ['min', round(min(sample), 3)],\n"
                    "       ['max', round(max(sample), 3)]])"
                ),
            },
        ],
    },
    {
        "key": "primes",
        "title": "Primes to 1000: sieve, gaps, twins",
        "description": (
            "Run a sieve, then interrogate what comes out: the spacing between "
            "primes, the twin-prime pairs, and the density by bucket. The steps "
            "each build on the `primes` list from step 1."
        ),
        "steps": [
            {
                "name": "Sieve to 1000",
                "note": "Define `primes`; show the first ten and the count.",
                "code": (
                    "limit = 1000\n"
                    "sieve = [True] * (limit + 1)\n"
                    "sieve[0] = sieve[1] = False\n"
                    "for p in range(2, int(limit ** 0.5) + 1):\n"
                    "    if sieve[p]:\n"
                    "        for m in range(p * p, limit + 1, p):\n"
                    "            sieve[m] = False\n"
                    "primes = [n for n, ok in enumerate(sieve) if ok]\n"
                    "show({'count': len(primes), 'first 10': primes[:10], 'last': primes[-1]})"
                ),
            },
            {
                "name": "Gap distribution",
                "note": "How often does each prime-gap size occur?",
                "code": (
                    "gaps = [b - a for a, b in zip(primes, primes[1:])]\n"
                    "counts = collections.Counter(gaps)\n"
                    "rows = [[g, counts[g]] for g in sorted(counts)]\n"
                    "show(rows)"
                ),
            },
            {
                "name": "Twin primes",
                "note": "Pairs whose gap is 2, out of the 168 primes.",
                "code": (
                    "twins = [(a, b) for a, b in zip(primes, primes[1:]) if b - a == 2]\n"
                    "show({'twin pairs': len(twins),\n"
                    "      'first 5': [[a, b] for a, b in twins[:5]],\n"
                    "      'last': list(twins[-1]) if twins else None})"
                ),
            },
            {
                "name": "Density by bucket",
                "note": "Plot the `primes` from step 1 in 100-wide buckets.",
                "code": (
                    "buckets = [sum(1 for p in primes if lo <= p < hi)\n"
                    "           for lo, hi in [(i, i + 100) for i in range(0, 1000, 100)]]\n"
                    "labels = ['1-100', '101-200', '201-300', '301-400', '401-500',\n"
                    "          '501-600', '601-700', '701-800', '801-900', '901-1000']\n"
                    "plt.bar(range(len(buckets)), buckets, color='tab:orange')\n"
                    "plt.xticks(range(len(buckets)), labels, rotation=45, ha='right')\n"
                    "plt.title('Primes per 100, from 1 to 1000'); plt.ylabel('count')\n"
                    "plt.grid(True, axis='y', alpha=0.3)\n"
                    "plt.show()\n"
                    "print('total primes:', sum(buckets))"
                ),
            },
        ],
    },
    {
        "key": "zeta",
        "title": "Watch 1/n² converge to π²/6",
        "description": (
            "Build the partial sums of the Basel series, measure the error "
            "against π²/6, plot the convergence, and estimate how fast the "
            "error decays. Each step reuses what the last one computed."
        ),
        "steps": [
            {
                "name": "Partial sums",
                "note": "Define `n_list` and `partial` (the running totals).",
                "code": (
                    "n_list = list(range(1, 201))\n"
                    "partial = []\n"
                    "total = 0.0\n"
                    "for n in n_list:\n"
                    "    total += 1.0 / (n * n)\n"
                    "    partial.append(total)\n"
                    "show({'terms': len(n_list), 'partial sum at n=200': round(partial[-1], 6)})"
                ),
            },
            {
                "name": "Error vs π²/6",
                "note": "Track the gap to the known limit, π²/6 ≈ 1.644934.",
                "code": (
                    "target = math.pi ** 2 / 6\n"
                    "errs = [target - p for p in partial]\n"
                    "show([['n', n, round(partial[n-1], 6), round(errs[n-1], 6)]\n"
                    "       for n in [1, 10, 50, 100, 200]])"
                ),
            },
            {
                "name": "Plot the convergence",
                "note": "The partial sums (step 1) against the limit line.",
                "code": (
                    "plt.plot(n_list, partial, color='tab:blue')\n"
                    "plt.plot(n_list, [target] * len(n_list), color='tab:red', linestyle='--')\n"
                    "plt.title('Partial sums of 1/n^2 vs pi^2/6')\n"
                    "plt.xlabel('n'); plt.ylabel('partial sum')\n"
                    "plt.grid(True, alpha=0.3)\n"
                    "plt.show()\n"
                    "print('limit pi^2/6 =', round(target, 6))"
                ),
            },
            {
                "name": "How fast?",
                "note": "Fit the slope of log(error) vs log(n) — the decay rate.",
                "code": (
                    "pts = [(n, errs[n-1]) for n in [10, 50, 100, 200]]\n"
                    "logn = [math.log(n) for n, _ in pts]\n"
                    "loge = [math.log(e) for _, e in pts]\n"
                    "bar = lambda xs: sum(xs) / len(xs)\n"
                    "slope = (sum((x - bar(logn)) * (y - bar(loge)) for x, y in zip(logn, loge))\n"
                    "         / sum((x - bar(logn)) ** 2 for x in logn))\n"
                    "show({'estimated rate': 'error ~ n^%.2f' % (-slope),\n"
                    "      'theory': 'error ~ 1/n  (slope -1.0)',\n"
                    "      'residual at n=200': round(errs[199], 6)})"
                ),
            },
        ],
    },
]


def workflows():
    """The curated analysis workflows as data: metadata + each step's code +
    its share-link token (so any single step can be opened in the workbench).
    Pure and deterministic — no matplotlib, no file I/O — so it is cheap to call
    per request and trivial to test. The steps are run client-side through the
    existing /api/compute path (state carried forward), so this function never
    executes them; it only describes them.
    """
    out = []
    for wf in WORKFLOWS:
        steps = []
        for i, st in enumerate(wf["steps"], 1):
            token = encode_share(st["code"])
            steps.append({
                "index": i,
                "name": st["name"],
                "note": st["note"],
                "code": st["code"],
                "token": token,
                "chars": len(st["code"].strip()),
                "workbench": "/sandbox#c=" + (token or ""),
            })
        out.append({
            "key": wf["key"],
            "title": wf["title"],
            "description": wf["description"],
            "step_count": len(wf["steps"]),
            "steps": steps,
            "deep_link": "/sandbox#w=" + wf["key"],
        })
    return out


def encode_share(code):
    """Encode a snippet into a URL-safe token for a shareable workbench link.

    The snippet lives in the URL *fragment* (``#c=<token>``), which the browser
    never sends to the server — so a share link is just data on the client, not
    a stored object. ``base64.urlsafe_b64encode`` keeps it fragment-safe
    (no ``+``/``/``); newlines are preserved on decode so the snippet restores
    byte-for-byte. Snippets over ``SHARED_MAX_CHARS`` are refused (returns
    ``None``) so a link can never balloon past browser URL limits.
    """
    if not isinstance(code, str):
        return None
    code = code.strip()
    if not code or len(code) > SHARED_MAX_CHARS:
        return None
    b = base64.urlsafe_b64encode(code.encode("utf-8")).rstrip(b"=")
    return b.decode("ascii")


def decode_share(token):
    """Inverse of :func:`encode_share` — restore a snippet from a URL token.

    Tolerant of missing padding (re-added before decode). Returns ``None`` for a
    non-string or unparsable token (never raises), so a hand-edited/mangled link
    degrades to an empty workbench instead of an error.
    """
    if not isinstance(token, str) or not token:
        return None
    tok = token.strip()
    if not tok:
        return None
    pad = "=" * (-len(tok) % 4)
    try:
        raw = base64.urlsafe_b64decode(tok + pad)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not text.strip() or len(text) > SHARED_MAX_CHARS:
        return None
    return text


def _interpret(rc, out, err, timed_out, inbound_state=None):
    """Combine the child's JSON result (if any) with the observed exit/timeout
    into the response dict the API serves. ``inbound_state`` is the prior
    session's state dict: when a run is killed (wall-clock or CPU limit) before
    it can report, its variables did not change, so the session returns exactly
    what it entered with."""
    result = None
    if err:
        # The harness writes exactly one JSON line to stderr; take the last one.
        for line in reversed(err.strip().splitlines()):
            try:
                result = json.loads(line)
                break
            except (json.JSONDecodeError, ValueError):
                continue

    if timed_out:
        return {
            "ok": False,
            "timed_out": True,
            "out": (result or {}).get("out", ""),
            "images": (result or {}).get("images", []),
            "results": (result or {}).get("results", []),
            "state": inbound_state or {},
            "state_dropped": (result or {}).get("state_dropped", []),
            "error": "Execution exceeded the %ds wall-clock limit and was killed."
                     % WALL_TIMEOUT_SECONDS,
            "exception": "Timeout",
            "rc": rc,
        }

    # A hard CPU-time limit shows up as SIGKILL/SIGXCPU (negative rc).
    if rc == -signal.SIGKILL or rc == -signal.SIGXCPU:
        return {
            "ok": False,
            "timed_out": rc == -signal.SIGXCPU,
            "out": (result or {}).get("out", ""),
            "images": (result or {}).get("images", []),
            "results": (result or {}).get("results", []),
            "state": inbound_state or {},
            "state_dropped": (result or {}).get("state_dropped", []),
            "error": ("Exceeded the %ds CPU-time limit and was killed."
                      % CPU_TIME_LIMIT_SECONDS),
            "exception": "ResourceLimit",
            "rc": rc,
        }

    if result is not None:
        return {
            "ok": bool(result.get("ok", False)),
            "timed_out": False,
            "out": result.get("out", "")[:MAX_OUTPUT_CHARS],
            "images": result.get("images", []),
            "results": result.get("results", []),
            "state": result.get("state", {}),
            "state_dropped": result.get("state_dropped", []),
            "error": result.get("error"),
            "exception": result.get("exception"),
            "note": result.get("note"),
            "rc": rc,
        }

    # No structured result: the child crashed hard or was killed before it
    # could report. Surface what we can; the session is unknown, so it resets.
    return {
        "ok": False,
        "timed_out": False,
        "out": (out or "")[:MAX_OUTPUT_CHARS],
        "images": [],
        "results": [],
        "state": {},
        "state_dropped": [],
        "error": (err or "").strip() or ("Process exited with code %s." % rc),
        "exception": None,
        "rc": rc,
    }


def run_compute(code, state=None, wall_timeout=WALL_TIMEOUT_SECONDS):
    """Run ``code`` in the isolated, limited interpreter.

    Returns a dict: ``{ok, out, images, results, state, state_dropped, error,
    exception, timed_out, note, rc}``. ``images`` is a list of
    ``{format, bytes, data_url}`` for any figures a snippet produced via
    ``plt.show()`` (empty when it plots nothing). ``results`` is a list of
    JSON-serializable values a snippet surfaced via ``show(value)`` (empty when
    the snippet calls ``show`` on nothing). ``state`` is the session's data
    variables after this run (JSON-exact values only; see the codec in the
    harness); ``state_dropped`` lists names that were in scope but could not be
    carried forward (user functions, non-persistable objects, oversize). Both
    images/results are capped, so a single run can never produce an oversized
    payload.

    ``state`` (optional) is the *prior* run's ``state`` dict, echoed back by a
    client so this run can reference the previous run's variables. It rides to
    the child as the first line of the stdin envelope (base64 of the state
    JSON; empty line when there is none) and is re-asserted against the
    reserved names, so a client cannot shadow ``plt`` or a module. A killed run
    returns its inbound session unchanged. The server stores nothing between
    runs — the browser owns its own session. Never raises for user-code
    problems; only raises if the sandbox itself is misconfigured (e.g. the
    interpreter is missing).
    """
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "out": "", "images": [], "results": [],
                "state": {}, "state_dropped": [],
                "error": "No code supplied.",
                "exception": None, "timed_out": False, "note": None, "rc": None}

    code = code.strip()

    # Build the stdin envelope: first line = base64 of the prior session's
    # state JSON (EMPTY line when there is none), then the snippet. Always
    # emitting the envelope line keeps the protocol unambiguous (the harness
    # can't mistake the first code line for a state line), and riding it in
    # stdin keeps an oversize session out of argv/env so it never trips
    # ARG_MAX at spawn. A malformed state degrades to an empty session, never
    # a subprocess error.
    state_b64 = ""
    if isinstance(state, dict) and state:
        try:
            state_json = json.dumps(state, ensure_ascii=False)
            state_b64 = base64.urlsafe_b64encode(state_json.encode("utf-8")).decode("ascii")
        except (TypeError, ValueError):
            state_b64 = ""
    stdin_payload = state_b64 + "\n" + code

    if not _compute_slots.acquire(timeout=wall_timeout + 2):
        return {"ok": False, "out": "", "images": [], "results": [],
                "state": {}, "state_dropped": [],
                "error": "Workbench is busy; try again in a moment.",
                "exception": None, "timed_out": False, "note": "overloaded", "rc": None}
    try:
        try:
            proc = subprocess.run(
                [VENV_PYTHON, "-I", "-c", _HARNESS],
                input=stdin_payload,
                capture_output=True,
                text=True,
                timeout=wall_timeout,
                env=_child_env(),
            )
            timed_out = False
            rc = proc.returncode
            out, err = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            timed_out = True
            rc = -1
            out = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = (e.stderr or b"") if isinstance(e.stderr, bytes) else (e.stderr or "")
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
        return _interpret(rc, out, err, timed_out,
                          inbound_state=(state if isinstance(state, dict) else None))
    finally:
        _compute_slots.release()


# --- History (persistent JSONL) ---------------------------------------------
def record_compute(code, result, limit=200):
    """Append a snippet + summary to the compute history log (best effort)."""
    try:
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "ok": bool(result.get("ok")),
            "timed_out": bool(result.get("timed_out")),
            "chars": len(code),
            "code": code.strip()[:2000],
            "error": (result.get("error") or "")[:500],
            "note": result.get("note"),
            "rc": result.get("rc"),
        }
        with open(COMPUTE_HISTORY_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_compute_history(limit=50):
    """Return recent compute runs, newest first (best effort)."""
    if not os.path.exists(COMPUTE_HISTORY_PATH):
        return []
    try:
        lines = open(COMPUTE_HISTORY_PATH).read().strip().splitlines()
        entries = []
        for line in lines[-limit:]:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return list(reversed(entries))
    except Exception:
        return []
