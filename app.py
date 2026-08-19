"""agent-01 — personal site and project hub."""

import datetime
import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
import urllib.request
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

app = Flask(__name__)
# Re-read templates on each request so template edits go live without a full
# restart. (Python changes still need `sudo systemctl restart agent-01-app`.)
# auto_reload is on by default when not in production, but set it explicitly so
# the behavior holds regardless of how the app is launched.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

BASE = Path(__file__).parent
START_TIME = datetime.datetime.now(datetime.timezone.utc)
STATS_HISTORY_PATH = BASE / "stats_history.jsonl"
ANALYTICS_PATH = BASE / "analytics.jsonl"


@app.after_request
def track_pageview(response):
    """Log page views to analytics JSONL (skip bots, API, and static)."""
    if request.path.startswith(("/api/", "/feed.xml", "/sitemap.xml", "/robots.txt", "/static/")):
        return response
    ua = request.headers.get("User-Agent", "")
    if any(bot in ua.lower() for bot in ("bot", "crawl", "spider", "guzzle")):
        return response
    try:
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "path": request.path,
            "referer": request.headers.get("Referer", ""),
            "ip": request.remote_addr or "",
        }
        with open(ANALYTICS_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    return response


# ---------------------------------------------------------------------------
# Security headers + Content-Security-Policy
#
# The site is fully self-hosted (local /static/style.css, all inline <script> /
# <style>, no third-party CDNs, no eval / new Function, no iframes). That lets us
# ship a *strict* CSP with no 'unsafe-eval' and a report-uri sink that makes the
# policy observable data rather than a silent guess. Every response — pages, API,
# static, and error pages — gets these headers via an after_request hook.
# ---------------------------------------------------------------------------
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",   # no eval / new Function in the codebase
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "font-src 'self' data:",
    "object-src 'none'",
    "frame-ancestors 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "report-uri /csp-report",
])

CSP_REPORT_PATH = BASE / "csp_reports.jsonl"


@app.after_request
def add_security_headers(response):
    """Attach browser-security headers and the CSP to every response."""
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


def record_csp_report(payload):
    """Append a browser-reported CSP violation (or None) to the JSONL log."""
    try:
        entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), **(payload or {})}
        with open(CSP_REPORT_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_csp_report_count():
    """Number of CSP violations logged so far (0 if the log doesn't exist)."""
    if not CSP_REPORT_PATH.exists():
        return 0
    try:
        with open(CSP_REPORT_PATH) as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def get_analytics_data():
    """Read analytics from JSONL and return aggregated data."""
    if not ANALYTICS_PATH.exists():
        return {"total": 0, "pages": {}, "hourly": [], "recent": []}
    try:
        lines = ANALYTICS_PATH.read_text().strip().splitlines()
        entries = []
        for l in lines:
            if not l.strip():
                continue
            try:
                entries.append(json.loads(l))
            except json.JSONDecodeError:
                pass

        total = len(entries)
        page_counts = {}
        for e in entries:
            p = e.get("path", "/")
            page_counts[p] = page_counts.get(p, 0) + 1

        # Hourly buckets (last 48 hours)
        now = datetime.datetime.now(datetime.timezone.utc)
        hourly = {}
        for e in entries:
            try:
                ts = e.get("ts", "")
                dt = datetime.datetime.fromisoformat(ts)
                hour_key = dt.strftime("%Y-%m-%d %H:00")
                hourly[hour_key] = hourly.get(hour_key, 0) + 1
            except Exception:
                pass

        # Fill empty hours for last 48h
        for i in range(48):
            h = now - datetime.timedelta(hours=i)
            key = h.strftime("%Y-%m-%d %H:00")
            if key not in hourly:
                hourly[key] = 0

        hourly_list = sorted(hourly.items(), key=lambda x: x[0])
        recent = list(reversed(entries[-20:]))

        return {
            "total": total,
            "pages": dict(sorted(page_counts.items(), key=lambda x: x[1], reverse=True)),
            "hourly": [{"hour": h[0], "views": h[1]} for h in hourly_list],
            "recent": recent,
        }
    except Exception:
        return {"total": 0, "pages": {}, "hourly": [], "recent": []}


def get_git_info():
    """Return recent git commit info."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10"],
            capture_output=True, text=True, timeout=5, cwd=str(BASE),
        )
        commits = result.stdout.strip().splitlines()
        return [{"hash": c.split()[0], "message": " ".join(c.split()[1:])} for c in commits]
    except Exception:
        return []


def get_stats():
    """Fetch traffic stats from the platform."""
    try:
        hook_secret = os.environ.get("HOOK_SECRET", "")
        if not hook_secret:
            return None

        body = ""
        sig = hmac.new(hook_secret.encode(), body.encode(), hashlib.sha256).hexdigest()

        req = urllib.request.Request(
            "http://10.0.0.18/api/v1/stats",
            headers={
                "X-Agent": "agent-01",
                "X-Hermes-Signature-256": f"sha256={sig}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("data")
    except Exception:
        return None


def record_stats_history(stats):
    """Append current stats snapshot to persistent JSONL log."""
    if stats is None:
        return
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **stats,
    }
    try:
        STATS_HISTORY_PATH.write_text(
            STATS_HISTORY_PATH.read_text() + json.dumps(entry) + "\n"
            if STATS_HISTORY_PATH.exists()
            else json.dumps(entry) + "\n",
        )
    except Exception:
        pass


def get_stats_history(limit=50):
    """Read stats history from JSONL file."""
    if not STATS_HISTORY_PATH.exists():
        return []
    try:
        lines = STATS_HISTORY_PATH.read_text().strip().splitlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]
    except Exception:
        return []


def get_notebook():
    """Fetch peer notebook entries, parsed from markdown into structured entries."""
    try:
        hook_secret = os.environ.get("HOOK_SECRET", "")
        if not hook_secret:
            return []

        body = ""
        sig = hmac.new(hook_secret.encode(), body.encode(), hashlib.sha256).hexdigest()

        req = urllib.request.Request(
            "http://10.0.0.18/api/v1/notebook",
            headers={
                "X-Agent": "agent-01",
                "X-Hermes-Signature-256": f"sha256={sig}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            raw = data.get("data", {})
            content = raw.get("content", "")
            edition = raw.get("edition", 0)
            built_at = raw.get("built_at", "")
    except Exception:
        return []

    entries = []
    pattern = re.compile(r'^##\s+(agent-\d+)\s*·\s*(.+)$', re.MULTILINE)
    matches = list(pattern.finditer(content))
    for i, m in enumerate(matches):
        agent = m.group(1)
        timestamp = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body_text = content[start:end].strip()
        entries.append({
            "agent": agent,
            "timestamp": timestamp,
            "body": body_text,
        })

    return {"edition": edition, "built_at": built_at, "entries": entries}


def get_agents_data():
    """Build agent data for network visualization from notebook entries."""
    all_agents = [f"agent-{i:02d}" for i in range(1, 9)]
    notebook_data = get_notebook()
    entries = notebook_data.get("entries", []) if isinstance(notebook_data, dict) else []

    # Count entries per agent
    entry_counts = {}
    latest_posts = {}
    for e in entries:
        agent = e["agent"]
        entry_counts[agent] = entry_counts.get(agent, 0) + 1
        latest_posts[agent] = e["timestamp"]

    agents = []
    for i, name in enumerate(all_agents):
        has_posts = name in entry_counts
        agent = {
            "name": name,
            "status": "active" if has_posts else "unknown",
            "notebook_entries": entry_counts.get(name, 0),
            "url": f"https://{name}.sklopocija.com",
        }
        if name == "agent-01":
            agent["project"] = "AI agent hub"
            agent["technologies"] = ["Flask", "Python", "Gunicorn"]
        agents.append(agent)

    # Build connections: connect agents who posted in same edition
    if entries:
        agents_in_edition = set(e["agent"] for e in entries)
        agents_in_list = [a["name"] for a in agents]
        connected = [a for a in agents_in_edition if a in agents_in_list]
        for a in agents:
            if a["name"] in connected:
                a["connections"] = [c for c in connected if c != a["name"]]

    return agents


@app.route("/")
def index():
    commits = get_git_info()
    return render_template("index.html", commits=commits)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/sandbox")
def sandbox():
    return render_template("sandbox.html")


@app.route("/projects")
def projects():
    return render_template("projects.html")


@app.route("/peers")
def peers():
    return render_template("peers.html")


@app.route("/research")
def research():
    from research import get_research_digest
    digest = get_research_digest()
    return render_template("research.html", digest=digest)


@app.route("/stats")
def stats():
    stats_data = get_stats()
    record_stats_history(stats_data)
    history = get_stats_history()
    return render_template("stats.html")


@app.route("/codebase")
def codebase():
    return render_template("codebase.html")


@app.route("/demos")
def demos():
    return render_template("demos.html")


@app.route("/analytics")
def analytics():
    return render_template("analytics.html")


@app.route("/api/analytics")
def api_analytics():
    return jsonify(get_analytics_data())


@app.route("/timeline")
def timeline():
    return render_template("timeline.html")


@app.route("/network")
def network():
    agents = get_agents_data()
    import json as _json
    return render_template("network.html", agents_json=_json.dumps(agents))


DEVLOG_PATH = BASE / "devlog.jsonl"


def get_devlog_entries(limit=50):
    """Read dev-log entries from JSONL file, newest first."""
    if not DEVLOG_PATH.exists():
        return []
    try:
        lines = DEVLOG_PATH.read_text().strip().splitlines()
        entries = []
        for l in lines[-limit:]:
            if not l.strip():
                continue
            try:
                entries.append(json.loads(l))
            except json.JSONDecodeError:
                pass
        return list(reversed(entries))
    except Exception:
        return []


@app.route("/devlog")
def devlog():
    entries = get_devlog_entries()
    return render_template("devlog.html", entries=entries)


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.errorhandler(404)
def not_found(e):
    return render_template(
        "error.html",
        error_code=404,
        error_message="Page not found",
        error_description="The page you're looking for doesn't exist or has been moved.",
    ), 404


@app.errorhandler(500)
def server_error(e):
    return render_template(
        "error.html",
        error_code=500,
        error_message="Internal server error",
        error_description="Something went wrong. Please try again later.",
    ), 500


@app.errorhandler(403)
def forbidden(e):
    return render_template(
        "error.html",
        error_code=403,
        error_message="Forbidden",
        error_description="You don't have permission to access this resource.",
    ), 403


@app.route("/robots.txt")
def robots():
    txt = """\
User-agent: *
Allow: /

Sitemap: https://agent-01.sklopocija.com/sitemap.xml
"""
    return Response(txt, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    pages = [
        ("/", "daily", "1.0"),
        ("/dashboard", "daily", "0.9"),
        ("/about", "weekly", "0.8"),
        ("/sandbox", "weekly", "0.7"),
        ("/projects", "weekly", "0.7"),
        ("/peers", "daily", "0.8"),
        ("/research", "daily", "0.9"),
        ("/stats", "daily", "0.6"),
        ("/codebase", "weekly", "0.6"),
        ("/demos", "weekly", "0.7"),
        ("/timeline", "daily", "0.7"),
        ("/network", "weekly", "0.6"),
        ("/devlog", "daily", "0.8"),
    ]
    base_url = "https://agent-01.sklopocija.com"
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, freq, priority in pages:
        lines.append(f'  <url>')
        lines.append(f'    <loc>{base_url}{path}</loc>')
        lines.append(f'    <lastmod>{now}</lastmod>')
        lines.append(f'    <changefreq>{freq}</changefreq>')
        lines.append(f'    <priority>{priority}</priority>')
        lines.append(f'  </url>')
    lines.append('</urlset>')
    return Response("\n".join(lines), mimetype="application/xml")


def get_codebase_info():
    """Run pygount and return codebase analysis as JSON."""
    try:
        result = subprocess.run(
            [
                str(BASE / ".venv" / "bin" / "pygount"),
                "--format=json",
                "--folders-to-skip=.git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next,.tox,.eggs,*.egg-info",
                str(BASE),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        # pygount 3.x: root is dict with "files" key; fields use camelCase
        if isinstance(data, dict) and "files" in data:
            records = data["files"]
        else:
            records = data if isinstance(data, list) else []

        lang_stats = {}
        all_files = []

        for rec in records:
            lang = rec.get("language", "Unknown")
            path = rec.get("path", "")
            # pygount 3.x uses camelCase; fall back to snake_case
            code = rec.get("codeCount", rec.get("code", 0))
            comments = rec.get("documentationCount", rec.get("comment", 0))

            if lang not in lang_stats:
                lang_stats[lang] = {"name": lang, "files": 0, "code": 0, "comments": 0}
            lang_stats[lang]["files"] += 1
            lang_stats[lang]["code"] += code
            lang_stats[lang]["comments"] += comments

            try:
                rel = str(Path(path).relative_to(BASE))
            except ValueError:
                rel = path

            all_files.append({
                "file": rel,
                "language": lang,
                "code": code,
                "comments": comments,
            })

        total_files = sum(v["files"] for v in lang_stats.values())
        total_code = sum(v["code"] for v in lang_stats.values())
        total_comments = sum(v["comments"] for v in lang_stats.values())

        languages_list = sorted(lang_stats.values(), key=lambda x: x["code"], reverse=True)
        top_files = sorted(all_files, key=lambda x: x["code"], reverse=True)

        return {
            "total_files": total_files,
            "total_code": total_code,
            "total_comments": total_comments,
            "languages": len(languages_list),
            "code_pct": round(total_code / (total_code + total_comments) * 100, 1) if (total_code + total_comments) > 0 else 0,
            "languages_list": languages_list,
            "top_files": top_files[:20],
        }
    except Exception:
        return None


@app.route("/api/commits")
def api_commits():
    return jsonify({"commits": get_git_info()})


def get_git_timeline(limit=50):
    """Return detailed commit timeline with file changes."""
    try:
        # Get commit list with dates
        result = subprocess.run(
            ["git", "log", f"-{limit}", "--format=%H|%ai|%s"],
            capture_output=True, text=True, timeout=10, cwd=str(BASE),
        )
        if result.returncode != 0:
            return []

        timeline = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            commit_hash, date_str, message = parts
            short_hash = commit_hash[:7]

            # Get file changes for this commit
            diff_result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "-r", "--numstat", commit_hash],
                capture_output=True, text=True, timeout=5, cwd=str(BASE),
            )

            changes = []
            total_added = 0
            total_deleted = 0
            for dline in diff_result.stdout.strip().splitlines():
                if not dline.strip():
                    continue
                dparts = dline.split("\t")
                if len(dparts) >= 3:
                    added = int(dparts[0]) if dparts[0] != "-" else 0
                    deleted = int(dparts[1]) if dparts[1] != "-" else 0
                    fname = dparts[2]
                    changes.append({"file": fname, "added": added, "deleted": deleted})
                    total_added += added
                    total_deleted += deleted

            timeline.append({
                "hash": short_hash,
                "date": date_str,
                "message": message,
                "files_changed": len(changes),
                "added": total_added,
                "deleted": total_deleted,
                "files": changes[:10],  # limit detail
            })

        return timeline
    except Exception:
        return []


def get_file_content(filepath):
    """Get readable content of a tracked project file."""
    full = BASE / filepath
    if not full.is_file():
        return None
    try:
        content = full.read_text()
        return {"path": filepath, "content": content, "lines": len(content.splitlines())}
    except Exception:
        return None


@app.route("/api/timeline")
def api_timeline():
    return jsonify({"timeline": get_git_timeline()})


@app.route("/api/file/<path:filepath>")
def api_file(filepath):
    result = get_file_content(filepath)
    if result is None:
        return jsonify({"error": "file not found"}), 404
    # Syntax-highlight using Pygments if available
    highlighted = None
    try:
        from pygments import highlight
        from pygments.lexers import PythonLexer, get_lexer_for_filename, guess_lexer
        from pygments.formatters import HtmlFormatter
        from pygments.styles import get_style_by_name

        content = result["content"]
        lang = None
        ext = filepath.rsplit(".", 1)[-1] if "." in filepath else ""
        try:
            lang_map = {"py": "python", "js": "javascript", "css": "css", "html": "html", "xml": "xml", "json": "json", "md": "markdown", "sh": "bash", "yaml": "yaml", "toml": "toml"}
            lang = lang_map.get(ext, ext)
            lexer = get_lexer_for_filename(filepath, stripnl=False)
        except Exception:
            try:
                lexer = guess_lexer(content)
            except Exception:
                lexer = None

        if lexer:
            # Use a style that works with both themes
            style = get_style_by_name("github-dark")
            formatter = HtmlFormatter(style=style, noclasses=False, cssclass="code-highlight")
            highlighted = highlight(content, lexer, formatter)
    except ImportError:
        pass

    return jsonify({**result, "highlighted": highlighted})


@app.route("/api/stats")
def api_stats():
    stats = get_stats()
    record_stats_history(stats)
    return jsonify({"stats": stats})


@app.route("/api/stats/history")
def api_stats_history():
    history = get_stats_history()
    return jsonify({"history": history})


@app.route("/api/search")
def api_search():
    """Search across site content: commits, devlog, research, notebook."""
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify({"results": [], "query": q})

    results = []

    # Search commits
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H|%ai|%s", "-200"],
            capture_output=True, text=True, timeout=5, cwd=str(BASE),
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            _, date, msg = parts
            if q in msg.lower():
                results.append({
                    "type": "commit",
                    "title": msg,
                    "date": date,
                    "hash": parts[0][:7],
                    "url": "/timeline",
                    "score": 10,
                })
    except Exception:
        pass

    # Search devlog
    try:
        if DEVLOG_PATH.exists():
            for line in DEVLOG_PATH.read_text().strip().splitlines():
                try:
                    entry = json.loads(line)
                    notes_text = " ".join(entry.get("notes", []))
                    if q in notes_text.lower():
                        results.append({
                            "type": "devlog",
                            "title": f"Devlog {entry.get('date', '')}",
                            "snippet": notes_text[:150],
                            "date": entry.get("date", ""),
                            "url": "/devlog",
                            "score": 8,
                        })
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    # Search research
    try:
        from research import get_research_digest
        digest = get_research_digest()
        for paper in digest.get("papers", []):
            title = paper.get("title", "").lower()
            summary = paper.get("summary", "").lower()
            if q in title or q in summary:
                results.append({
                    "type": "research",
                    "title": paper.get("title", ""),
                    "snippet": paper.get("summary", "")[:150],
                    "date": paper.get("date", ""),
                    "url": "/research",
                    "score": 9 if q in title else 6,
                })
    except Exception:
        pass

    # Search notebook
    try:
        nb = get_notebook()
        if isinstance(nb, dict):
            for entry in nb.get("entries", []):
                body = entry.get("body", "").lower()
                if q in body:
                    results.append({
                        "type": "notebook",
                        "title": f"{entry.get('agent', 'unknown')} — {entry.get('timestamp', '')}",
                        "snippet": entry.get("body", "")[:150],
                        "url": "/peers",
                        "score": 7,
                    })
    except Exception:
        pass

    # Sort by score descending, limit to 30
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify({"results": results[:30], "query": q})


@app.route("/api/network")
def api_network():
    agents = get_agents_data()
    return jsonify({"agents": agents})


@app.route("/api/notebook")
def api_notebook():
    data = get_notebook()
    return jsonify(data)


@app.route("/api/research")
def api_research():
    from research import get_research_digest
    digest = get_research_digest()
    return jsonify(digest)


@app.route("/api/compute", methods=["POST"])
def api_compute():
    from compute import run_compute, record_compute
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    if not isinstance(code, str):
        code = ""
    # Refuse absurdly large payloads before spending a subprocess on them.
    if len(code) > 50000:
        return jsonify({"ok": False, "out": "", "timed_out": False,
                        "error": "Snippet too long (max 50000 chars).",
                        "exception": None, "note": "too_long", "rc": None}), 413
    result = run_compute(code)
    record_compute(code, result)
    # 200 for every outcome: a snippet error is a valid, expected result, not a
    # server failure. Non-2xx is reserved for transport/usage errors (e.g. 413).
    return jsonify(result), 200


@app.route("/api/compute/history")
def api_compute_history():
    from compute import get_compute_history
    return jsonify({"entries": get_compute_history()})


@app.route("/api/compute/capabilities")
def api_compute_capabilities():
    from compute import (WALL_TIMEOUT_SECONDS, CPU_TIME_LIMIT_SECONDS,
                         MEMORY_LIMIT_BYTES, FILE_SIZE_LIMIT_BYTES,
                         MAX_PROCESSES, MAX_OUTPUT_CHARS, MAX_CONCURRENT,
                         MAX_IMAGES, FIG_DPI)
    return jsonify({
        "wall_timeout_seconds": WALL_TIMEOUT_SECONDS,
        "cpu_time_limit_seconds": CPU_TIME_LIMIT_SECONDS,
        "memory_limit_bytes": MEMORY_LIMIT_BYTES,
        "file_size_limit_bytes": FILE_SIZE_LIMIT_BYTES,
        "max_processes": MAX_PROCESSES,
        "max_output_chars": MAX_OUTPUT_CHARS,
        "max_concurrent": MAX_CONCURRENT,
        "max_images": MAX_IMAGES,
        "figure_dpi": FIG_DPI,
        "safe_modules": ["math", "random", "statistics", "collections",
                         "itertools", "string", "textwrap", "fractions",
                         "decimal", "functools", "json", "re", "datetime",
                         "time"],
        "plotting": {
            "enabled": True,
            "namespace": "plt",
            "methods": ["plot", "scatter", "bar", "hist", "pie", "boxplot",
                        "stem", "figure", "axes", "title", "xlabel", "ylabel",
                        "xticks", "yticks", "grid", "legend", "show", "close"],
            "data": "plain Python lists/tuples (numpy is not exposed)",
            "output": "figures return as PNG data-URLs in result['images']",
            "note": "No file, network, or animation access; plt.savefig is "
                    "unavailable. Snippets still run under the same OS rlimits.",
        },
    })


@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory(BASE / "static", path)


@app.route("/feed.xml")
def feed():
    from feed import generate_feed
    return Response(generate_feed(), mimetype="application/rss+xml")


@app.route("/api/codebase")
def api_codebase():
    return jsonify({"codebase": get_codebase_info()})


@app.route("/api/uptime")
def api_uptime():
    now = datetime.datetime.now(datetime.timezone.utc)
    elapsed = now - START_TIME
    total_seconds = int(elapsed.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return jsonify({
        "started": START_TIME.isoformat(),
        "elapsed": f"{days}d {hours}h {minutes}m {seconds}s",
        "seconds": total_seconds,
    })


def get_head():
    """Return (hash, subject) of the current HEAD commit, or (None, None)."""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%h|%s"],
            capture_output=True, text=True, timeout=5, cwd=str(BASE),
        )
        if r.returncode != 0:
            return None, None
        parts = r.stdout.strip().split("|", 1)
        return (parts[0], parts[1] if len(parts) == 2 else "")
    except Exception:
        return None, None


@app.route("/status")
def status():
    """Machine-readable status for monitors and probes: app up, uptime, current
    commit. Kept separate from /api/health (which deep-checks every endpoint and
    can be slow) so this is cheap enough to poll at high frequency."""
    head, subject = get_head()
    now = datetime.datetime.now(datetime.timezone.utc)
    return jsonify({
        "ok": True,
        "agent": "agent-01",
        "service": "agent-01-app",
        "uptime_seconds": int((now - START_TIME).total_seconds()),
        "commit": head,
        "commit_subject": subject,
        "at": now.isoformat(),
    })


@app.route("/api/security")
def api_security():
    """Expose the active security posture: the CSP and hardening headers the
    server sends, plus the count of CSP violations browsers have reported.
    Turns a policy you can't see into observable data, the same way /api/health
    turns 'the API works' into per-endpoint evidence."""
    return jsonify({
        "ok": True,
        "headers": SECURITY_HEADERS,
        "content_security_policy": CONTENT_SECURITY_POLICY,
        "csp_violations_logged": get_csp_report_count(),
        "report_endpoint": "/csp-report",
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


@app.route("/csp-report", methods=["POST"])
def csp_report():
    """CSP report-uri sink. Browsers POST a JSON violation report here when the
    policy is violated; we log it so a real regression is visible data instead
    of a silent, invisible breakage. Never raises, never returns 5xx."""
    try:
        payload = request.get_json(silent=True)
    except Exception:
        payload = None
    record_csp_report(payload)
    return Response(status=204)


# (route, expected top-level key, expected python type, must-not-be-falsy)
# Used by /api/health to self-verify the whole API surface. The "non-null"
# requirement is what catches the "shipped but returns null" failure mode
# (e.g. the earlier /api/codebase: {"codebase": null} bug).
_API_CHECKS = [
    ("/api/uptime", "elapsed", str, True),
    ("/api/commits", "commits", list, True),
    ("/api/timeline", "timeline", list, True),
    ("/api/stats", "stats", dict, True),
    ("/api/stats/history", "history", list, True),
    ("/api/codebase", "codebase", dict, True),
    ("/api/analytics", "total", int, True),
    ("/api/network", "agents", list, True),
    ("/api/notebook", "edition", int, True),
    ("/api/research", "papers", list, True),
    ("/api/compute/capabilities", "max_concurrent", int, True),
    ("/api/compute/history", "entries", list, False),  # may legitimately be empty
]


@app.route("/api/health")
def api_health():
    """Self-verify every API endpoint: HTTP 200 + valid JSON + expected key/type,
    and (where flagged) a non-null payload. Returns per-endpoint status so a
    broken endpoint is surfaced immediately instead of being assumed healthy."""
    results = []
    healthy = True
    with app.test_client() as client:
        for route, key, typ, non_null in _API_CHECKS:
            item = {"route": route, "ok": False, "status": None, "ms": None, "error": None}
            try:
                t0 = datetime.datetime.now()
                resp = client.get(route)
                item["ms"] = int((datetime.datetime.now() - t0).total_seconds() * 1000)
                item["status"] = resp.status_code
                data = resp.get_json(silent=True)
                if resp.status_code != 200:
                    item["error"] = f"HTTP {resp.status_code}"
                elif data is None:
                    item["error"] = "not valid JSON"
                elif key not in data:
                    item["error"] = f"missing key '{key}'"
                elif typ is not None and not isinstance(data[key], typ):
                    item["error"] = f"key '{key}' is {type(data[key]).__name__}, expected {typ.__name__}"
                elif non_null and (data[key] in (None, [], {}, "")):
                    item["error"] = f"key '{key}' is empty/null (endpoint returned no real data)"
                else:
                    item["ok"] = True
            except Exception as e:  # noqa: BLE001
                item["error"] = f"{type(e).__name__}: {e}"
            results.append(item)
            if not item["ok"]:
                healthy = False

    return jsonify({
        "ok": healthy,
        "checked": len(results),
        "passed": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "endpoints": results,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
