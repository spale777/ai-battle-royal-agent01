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

Blast radius: a low-privilege ``agent`` user on an isolated machine that cannot
reach the other agents or the outside world. That is the honest ceiling of what
this can be; we do not pretend otherwise.
"""

import base64
import datetime
import json
import os
import resource
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


def _legend(labels=None, **kwargs):
    if _plt_figures:
        return _plt_figures[-1][1].legend(labels, **kwargs)


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

def _emit(payload):
    sys.stderr.write(json.dumps(payload) + "\n")
    sys.stderr.flush()

code = sys.stdin.read()
ns = {"__builtins__": _builtins, "__name__": "__compute__"}
ns.update(_MODULES)
ns["plt"] = plt
_MAXOUT = @MAXOUT@

buf = io.StringIO()
_images = []
try:
    _LIMITS()
    with _cl.redirect_stdout(buf):
        exec(code, ns)
    _emit({"ok": True, "out": buf.getvalue()[:_MAXOUT], "error": None, "images": _images})
except SystemExit as _e:
    _emit({"ok": True, "out": buf.getvalue()[:_MAXOUT],
           "error": None, "note": "SystemExit(%r)" % (_e.code,), "images": _images})
except Exception:
    _tb = traceback.format_exc()
    _emit({"ok": False, "out": buf.getvalue()[:_MAXOUT],
           "error": _tb.strip(),
           "exception": _tb.strip().splitlines()[-1] if _tb.strip() else "",
           "images": _images})
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


def _interpret(rc, out, err, timed_out):
    """Combine the child's JSON result (if any) with the observed exit/timeout
    into the response dict the API serves."""
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
            "error": result.get("error"),
            "exception": result.get("exception"),
            "note": result.get("note"),
            "rc": rc,
        }

    # No structured result: the child crashed hard or was killed before it
    # could report. Surface what we can.
    return {
        "ok": False,
        "timed_out": False,
        "out": (out or "")[:MAX_OUTPUT_CHARS],
        "images": [],
        "error": (err or "").strip() or ("Process exited with code %s." % rc),
        "exception": None,
        "rc": rc,
    }


def run_compute(code, wall_timeout=WALL_TIMEOUT_SECONDS):
    """Run ``code`` in the isolated, limited interpreter.

    Returns a dict: ``{ok, out, images, error, exception, timed_out, note, rc}``.
    ``images`` is a list of ``{format, bytes, data_url}`` for any figures a
    snippet produced via ``plt.show()`` (empty when the snippet plots nothing).
    Never raises for user-code problems; only raises if the sandbox itself is
    misconfigured (e.g. the interpreter is missing).
    """
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "out": "", "images": [], "error": "No code supplied.",
                "exception": None, "timed_out": False, "note": None, "rc": None}

    code = code.strip()

    if not _compute_slots.acquire(timeout=wall_timeout + 2):
        return {"ok": False, "out": "", "images": [], "error": "Workbench is busy; try again in a moment.",
                "exception": None, "timed_out": False, "note": "overloaded", "rc": None}
    try:
        try:
            proc = subprocess.run(
                [VENV_PYTHON, "-I", "-c", _HARNESS],
                input=code,
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
        return _interpret(rc, out, err, timed_out)
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
