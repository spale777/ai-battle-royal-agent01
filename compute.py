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
   ``functools``, ``json``, ``re``, ``datetime``). ``open``, ``input``, ``eval``,
   ``exec``, ``compile`` and ``__import__`` are not exposed, and ``os`` /
   ``socket`` / ``subprocess`` / ``shutil`` are not in scope. This keeps normal
   work one-liner-friendly and blocks the obvious escalation paths — but the OS
   limits above are what actually bound the blast radius.

Blast radius: a low-privilege ``agent`` user on an isolated machine that cannot
reach the other agents or the outside world. That is the honest ceiling of what
this can be; we do not pretend otherwise.
"""

import datetime
import json
import os
import resource
import signal
import subprocess
import sys
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

# The child interpreter runs isolated (-I) and executes this harness. User code
# is read from stdin. A single JSON line is written to stderr so the parent can
# recover the result even if the child is later killed mid-stream.
_HARNESS_SOURCE = r"""
import builtins as _bi
import collections, datetime, decimal, fractions, functools, io, json, math
import random, re, statistics, string, sys, textwrap, time, traceback
import contextlib as _cl

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

# Things deliberately NOT exposed: open, input, eval, exec, compile, __import__,
# globals, locals, vars, breakpoint. os/socket/subprocess/shutil/sys not in scope.

def _emit(payload):
    sys.stderr.write(json.dumps(payload) + "\n")
    sys.stderr.flush()

code = sys.stdin.read()
ns = {"__builtins__": _builtins, "__name__": "__compute__"}
ns.update(_MODULES)
_MAXOUT = @MAXOUT@

buf = io.StringIO()
try:
    _LIMITS()
    with _cl.redirect_stdout(buf):
        exec(code, ns)
    _emit({"ok": True, "out": buf.getvalue()[:_MAXOUT], "error": None})
except SystemExit as _e:
    _emit({"ok": True, "out": buf.getvalue()[:_MAXOUT],
           "error": None, "note": "SystemExit(%r)" % (_e.code,)})
except Exception:
    _tb = traceback.format_exc()
    _emit({"ok": False, "out": buf.getvalue()[:_MAXOUT],
           "error": _tb.strip(),
           "exception": _tb.strip().splitlines()[-1] if _tb.strip() else ""})
"""

_HARNESS = (
    _HARNESS_SOURCE
    .replace("@CPU@", str(CPU_TIME_LIMIT_SECONDS))
    .replace("@MEM@", str(MEMORY_LIMIT_BYTES))
    .replace("@FSIZE@", str(FILE_SIZE_LIMIT_BYTES))
    .replace("@NPROC@", str(MAX_PROCESSES))
    .replace("@MAXOUT@", str(MAX_OUTPUT_CHARS))
)

_compute_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


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
        "error": (err or "").strip() or ("Process exited with code %s." % rc),
        "exception": None,
        "rc": rc,
    }


def run_compute(code, wall_timeout=WALL_TIMEOUT_SECONDS):
    """Run ``code`` in the isolated, limited interpreter.

    Returns a dict: ``{ok, out, error, exception, timed_out, note, rc}``.
    Never raises for user-code problems; only raises if the sandbox itself is
    misconfigured (e.g. the interpreter is missing).
    """
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "out": "", "error": "No code supplied.",
                "exception": None, "timed_out": False, "note": None, "rc": None}

    code = code.strip()

    if not _compute_slots.acquire(timeout=wall_timeout + 2):
        return {"ok": False, "out": "", "error": "Workbench is busy; try again in a moment.",
                "exception": None, "timed_out": False, "note": "overloaded", "rc": None}
    try:
        try:
            proc = subprocess.run(
                [VENV_PYTHON, "-I", "-c", _HARNESS],
                input=code,
                capture_output=True,
                text=True,
                timeout=wall_timeout,
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
