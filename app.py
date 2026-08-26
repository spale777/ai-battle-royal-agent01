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

from flask import Flask, Response, jsonify, render_template, request, send_from_directory, url_for

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
    from compute import workflows
    return render_template("sandbox.html", workflows=workflows())


@app.route("/gallery")
def gallery():
    """Workbench example gallery: pre-rendered figures (served statically) that
    show what the workbench computes and plots, each deep-linking into the live
    workbench via its share link (``/sandbox#c=<token>``)."""
    from compute import gallery_examples
    examples = gallery_examples()
    return render_template("gallery.html", examples=examples, count=len(examples))


@app.route("/projects")
def projects():
    return render_template("projects.html")


@app.route("/peers")
def peers():
    return render_template("peers.html")


@app.route("/research")
def research():
    from research import get_research_digest, bibtex
    digest = get_research_digest()
    # Attach a server-built BibTeX entry to each paper so the page can offer a
    # "copy citation" action. Single source of truth (research.bibtex) — the
    # same string the JSON API returns.
    for p in digest.get("papers", []):
        p["bibtex"] = bibtex(p)
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
        ("/gallery", "weekly", "0.6"),
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
    from research import get_research_digest, bibtex
    digest = get_research_digest()
    # Each paper carries a ready-made BibTeX entry (single source of truth in
    # research.bibtex), so a client can cite a paper without rebuilding it.
    for p in digest.get("papers", []):
        p["bibtex"] = bibtex(p)
    return jsonify(digest)


@app.route("/api/research/export")
def api_research_export():
    """Export the research digest — or a category-filtered subset — as a file a
    researcher can take: BibTeX (.bib), CSV, or JSON. Read-only: it serializes
    the cached digest, fetches nothing, stores nothing. The BibTeX is the same
    string research.bibtex() produces for the per-paper copy buttons.

    ?format=bibtex|csv|json (default bibtex); ?cat=<cat> narrows to one category
    using the same match as the /research page filter (primary or any listed)."""
    from research import get_research_digest, export_papers, EXPORT_FORMATS
    fmt = (request.args.get("format") or "bibtex").strip().lower()
    if fmt not in EXPORT_FORMATS:
        return jsonify({"error": f"format must be one of {list(EXPORT_FORMATS)}",
                        "formats": list(EXPORT_FORMATS)}), 400
    cat = request.args.get("cat")
    digest = get_research_digest()
    papers = digest.get("papers", [])
    body = export_papers(papers, fmt=fmt, cat=cat)
    # Filename the browser uses when saving; reflect the category when present.
    cat_label = cat.strip().replace("/", "-") if (cat and cat.strip()) else "all"
    ext = {"bibtex": "bib", "csv": "csv", "json": "json"}[fmt]
    content_type = {
        "bibtex": "application/x-bibtex",
        "csv": "text/csv",
        "json": "application/json",
    }[fmt]
    resp = Response(body, content_type=content_type)
    resp.headers["Content-Disposition"] = f'attachment; filename="arxiv-digest-{cat_label}.{ext}"'
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/research/search")
def api_research_search():
    """Live arXiv search (outward-facing). A visitor queries the arXiv corpus
    directly, not just the 20 papers in the hourly digest. Read-only: no code
    runs, nothing is stored. When arXiv is unreachable it degrades honestly to
    a local search over the cached digest (the response's `source` field says
    which happened)."""
    from research import search, MIN_QUERY_CHARS
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"query": "", "source": "none", "papers": [],
                        "total_results": 0,
                        "message": "Provide a query: /api/research/search?q=..."}), 400
    if len(q) < MIN_QUERY_CHARS:
        return jsonify({"query": q, "source": "none", "papers": [],
                        "total_results": 0,
                        "message": f"Query too short (min {MIN_QUERY_CHARS} chars)."}), 400
    try:
        max_results = int(request.args.get("max", 10))
    except (TypeError, ValueError):
        max_results = 10
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    field = request.args.get("field", "all")
    sort = request.args.get("sort", "relevance")
    result = search(q, max_results=max_results, field=field, sort=sort, page=page)
    # Attach a ready-made BibTeX entry to each hit (same single source of truth
    # as the digest) so a visitor can cite a live-search result too.
    from research import bibtex
    for p in result.get("papers", []):
        p["bibtex"] = bibtex(p)
    return jsonify(result)


@app.route("/api/research/search/export")
def api_research_search_export():
    """Export a live arXiv *search* result set as a downloadable file:
    BibTeX (.bib), CSV, or JSON — the same formats and the same
    research.bibtex() single source of truth the digest export uses.

    The digest was already exportable, but a live search result was not: a
    researcher who ran a good query could cite one paper but not take the set.
    This closes that gap. It re-runs the SAME deterministic search the UI
    shows (same query/field/sort/page) and exports exactly the papers the
    visitor was looking at, so "export" means "export what you see" — and a
    ?q=...&field=...&sort=...&page=... deep link reproduces it byte-for-byte.

    Read-only: it queries arXiv and serializes their results; no code runs,
    nothing is stored. `format` is allow-listed; `max` is clamped to arXiv's
    per-page cap (30), so a single export is bounded to one page of results."""
    from research import search, export_papers, EXPORT_FORMATS
    from research import MAX_RESULTS_CAP, MIN_QUERY_CHARS
    fmt = (request.args.get("format") or "bibtex").strip().lower()
    if fmt not in EXPORT_FORMATS:
        return jsonify({"error": f"format must be one of {list(EXPORT_FORMATS)}",
                        "formats": list(EXPORT_FORMATS)}), 400
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Provide a query: /api/research/search/export?q=...&format=bibtex"}), 400
    if len(q) < MIN_QUERY_CHARS:
        return jsonify({"error": f"Query too short (min {MIN_QUERY_CHARS} chars)."}), 400
    try:
        max_results = int(request.args.get("max", 12))
    except (TypeError, ValueError):
        max_results = 12
    max_results = max(1, min(max_results, MAX_RESULTS_CAP))
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    field = request.args.get("field", "all")
    sort = request.args.get("sort", "relevance")
    result = search(q, max_results=max_results, field=field, sort=sort, page=page)
    papers = result.get("papers", [])
    body = export_papers(papers, fmt=fmt)  # no cat: a search result is not category-filtered
    ext = {"bibtex": "bib", "csv": "csv", "json": "json"}[fmt]
    content_type = {
        "bibtex": "application/x-bibtex",
        "csv": "text/csv",
        "json": "application/json",
    }[fmt]
    # Reflect the query in the filename so a saved file says what it is.
    q_slug = re.sub(r"[^A-Za-z0-9]+", "-", q).strip("-")[:40] or "query"
    resp = Response(body, content_type=content_type)
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="arxiv-search-{q_slug}-p{page}.{ext}"')
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/compute", methods=["POST"])
def api_compute():
    from compute import run_compute, record_compute, MAX_STATE_BYTES
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    if not isinstance(code, str):
        code = ""
    # Refuse absurdly large payloads before spending a subprocess on them.
    if len(code) > 50000:
        return jsonify({"ok": False, "out": "", "timed_out": False,
                        "error": "Snippet too long (max 50000 chars).",
                        "exception": None, "note": "too_long", "rc": None}), 413
    # The prior run's session state (client-carried). Accept only a dict; refuse
    # a state blob that would balloon the request (the child would reject it too,
    # but an early 413 is cheaper and honest).
    state = data.get("state")
    state_bytes = 0
    if state is not None:
        if not isinstance(state, dict):
            state = None
        else:
            try:
                state_bytes = len(json.dumps(state, ensure_ascii=False))
            except (TypeError, ValueError):
                state = None
    if state is not None and state_bytes > MAX_STATE_BYTES:
        return jsonify({"ok": False, "out": "", "images": [], "results": [],
                        "state": {}, "state_dropped": [], "timed_out": False,
                        "error": "Session state too large (max %d bytes)." % MAX_STATE_BYTES,
                        "exception": None, "note": "state_too_large", "rc": None}), 413
    result = run_compute(code, state=state)
    record_compute(code, result)
    # 200 for every outcome: a snippet error is a valid, expected result, not a
    # server failure. Non-2xx is reserved for transport/usage errors (e.g. 413).
    return jsonify(result), 200


@app.route("/api/compute/share", methods=["GET", "POST"])
def api_compute_share():
    """Shareable workbench links: the snippet rides in the URL *fragment*
    (``#c=<token>``) — base64url, so a shared session reproduces itself and the
    browser never posts a code blob to the server.

    POST ``{"code": "..."}`` -> ``{ok, link, token, chars, max_chars}``.
    GET  ``?c=<token>``       -> ``{ok, code, chars, note?}`` (reverse lookup).
    A snippet over the cap is refused (honest limit); a mangled token decodes to
    an empty code, never an error. Read-only: nothing here runs or stores code.
    """
    from compute import encode_share, decode_share, SHARED_MAX_CHARS
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        code = data.get("code", "")
        if not isinstance(code, str):
            code = ""
        token = encode_share(code)
        if token is None:
            return jsonify({
                "ok": False, "link": None, "token": None,
                "chars": len(code.strip()) if code else 0,
                "max_chars": SHARED_MAX_CHARS,
                "note": "Snippet is empty or over the %d-char share limit."
                        % SHARED_MAX_CHARS,
            }), 413
        path = url_for("sandbox")
        return jsonify({
            "ok": True, "link": path + "#c=" + token, "token": token,
            "chars": len(code.strip()), "max_chars": SHARED_MAX_CHARS,
        }), 200
    # GET: reverse lookup for ?c=<token>
    token = request.args.get("c", "")
    code = decode_share(token) if token else None
    if code is None:
        return jsonify({"ok": False, "code": "", "chars": 0,
                        "note": "No valid snippet in this link."}), 200
    return jsonify({"ok": True, "code": code, "chars": len(code),
                    "max_chars": SHARED_MAX_CHARS}), 200


@app.route("/api/compute/history")
def api_compute_history():
    from compute import get_compute_history
    return jsonify({"entries": get_compute_history()})


@app.route("/api/compute/capabilities")
def api_compute_capabilities():
    from compute import (WALL_TIMEOUT_SECONDS, CPU_TIME_LIMIT_SECONDS,
                         MEMORY_LIMIT_BYTES, FILE_SIZE_LIMIT_BYTES,
                         MAX_PROCESSES, MAX_OUTPUT_CHARS, MAX_CONCURRENT,
                         MAX_IMAGES, FIG_DPI,
                         MAX_RESULT_DEPTH, MAX_RESULT_ITEMS, MAX_RESULT_KEYS,
                         MAX_RESULT_CELLS, MAX_RESULT_BYTES,
                         MAX_STATE_DEPTH, MAX_STATE_ITEMS, MAX_STATE_KEYS,
                         MAX_STATE_BYTES)
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
        "results": {
            "enabled": True,
            "function": "show",
            "output": "values returned as JSON in result['results']",
            "kinds": ["number", "string", "bool", "null", "list", "dict", "set"],
            "limits": {
                "max_depth": MAX_RESULT_DEPTH,
                "max_items": MAX_RESULT_ITEMS,
                "max_keys": MAX_RESULT_KEYS,
                "max_cells": MAX_RESULT_CELLS,
                "max_bytes": MAX_RESULT_BYTES,
            },
            "note": "show(value) surfaces a computed value so the page can "
                    "render it as a table. The serializer is bounded and "
                    "degrades unrepresentable objects to a short repr; it is a "
                    "display path, not a security boundary, and reads a value "
                    "the snippet already produced (never a file).",
        },
        "state": {
            "enabled": True,
            "mechanism": "client-carried session: run N's data variables are "
                         "echoed back in result['state'] and sent as the next "
                         "run's 'state', so a snippet can reference the "
                         "previous run's variables (a REPL, not a one-shot).",
            "persistence": "JSON-only and exact -- a variable is carried only "
                           "if it can be re-created bit-identically in a fresh "
                           "interpreter (no pickle, so no code can survive). "
                           "Non-persistable names (functions, sets, bytes, "
                           "NaN/inf, oversize) are dropped and reported by "
                           "name in result['state_dropped'], never truncated.",
            "reserved_names": "plt, show, and the pre-loaded modules can never "
                              "be shadowed by session state.",
            "storage": "none on the server -- the browser owns its own session; "
                       "a public endpoint keeps no user data and cannot leak "
                       "one user's state to another.",
            "limits": {
                "max_depth": MAX_STATE_DEPTH,
                "max_items": MAX_STATE_ITEMS,
                "max_keys": MAX_STATE_KEYS,
                "max_bytes": MAX_STATE_BYTES,
            },
        },
        "workflows": {
            "enabled": True,
            "endpoint": "/api/compute/workflows",
            "mechanism": "curated multi-step analysis pipelines; the page runs "
                         "the steps in order through the existing /api/compute "
                         "path, carrying the session state forward, so each step "
                         "can reference the previous step's variables.",
            "note": "Describing the workflows is pure data (no execution); "
                    "running them is the same isolated, rlimit-bound path as a "
                    "normal run. Each step carries a share-link token so any "
                    "single step can be opened in the workbench.",
        },
    })


@app.route("/api/compute/gallery")
def api_compute_gallery():
    """Workbench example gallery as data: each entry has the exact snippet a
    visitor can run, its share-link token, and the pre-rendered PNG path. The
    PNGs are static files (served by /static/gallery/), so this endpoint never
    runs matplotlib — it just reports what the gallery page shows and how to
    reproduce each figure in the live workbench."""
    from compute import gallery_examples
    return jsonify({"count": len(gallery_examples()), "examples": gallery_examples()})


@app.route("/api/compute/workflows")
def api_compute_workflows():
    """Curated analysis workflows as data: each is a small multi-step pipeline
    whose steps reference the previous step's variables (the REPL session
    memory). The page renders these and runs the steps live through the
    existing /api/compute path, carrying the session state forward step by
    step — so this endpoint only describes the workflows, it never executes
    them (no new execution surface, no new rlimit envelope). Each step carries
    its own share-link token so any single step can be opened in the workbench.
    """
    from compute import workflows
    return jsonify({"count": len(workflows()), "workflows": workflows()})


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


@app.route("/api/egress")
def api_egress():
    """Self-check the service's outbound internet path. This box has no direct
    egress — all outbound HTTP must go through a proxy configured in the process
    environment. If those env vars are missing, the internet-facing features
    (live arXiv search) do NOT error; they silently degrade to a cache fallback.
    This endpoint probes the same urllib path and reports reachability as data,
    with a pointed error when the proxy is missing, so that silent-degradation
    regression is caught here instead of discovered by a visitor seeing stale
    results. Read-only, no side effects."""
    from research import check_egress
    return jsonify(check_egress())


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
    ("/api/research/search?q=ai", "papers", list, False),  # may honestly be 0 hits
    ("/api/compute/capabilities", "max_concurrent", int, True),
    ("/api/compute/history", "entries", list, False),  # may legitimately be empty
    # Shareable workbench link (reverse lookup). cHJpbnQoMSk is the base64url of
    # "print(1)" — a guaranteed-valid token so the probe is deterministically 200.
    ("/api/compute/share?c=cHJpbnQoMSk", "code", str, True),
    # Workbench example gallery — the curated, pre-rendered figures.
    ("/api/compute/gallery", "examples", list, True),
    # Curated analysis workflows (multi-step, state-carrying) as data. Pure
    # metadata — no execution — so this is a deterministic non-null probe.
    ("/api/compute/workflows", "workflows", list, True),
    # Live egress probe — proves the service can actually reach the internet.
    # This is the endpoint that turns the "features silently fall back to cache
    # when the proxy env is missing" regression into a visible health failure.
    ("/api/egress", "reachable", bool, True),
    # Digest export as a downloadable file (bibtex/csv/json). The bibtex and csv
    # variants are plain text, so this probes the JSON variant, which returns a
    # top-level "papers" list just like /api/research.
    ("/api/research/export?format=json", "papers", list, True),
    # Live-search result export (bibtex/csv/json). Like the search endpoint
    # above it may honestly return an empty result set, so non-null is off —
    # the probe checks the route is live and returns the JSON shape.
    ("/api/research/search/export?q=ai&format=json", "papers", list, False),
]


# --- /api/health: bounded, so it can never kill its own worker ---------------
#
# /api/health re-runs every API endpoint in-process (test_client), including the
# networked ones (live arXiv search, the egress probe, and the research digest on
# cache expiry). Their worst-case latency once summed to ~95s — over 3x
# gunicorn's default 30s worker timeout — so a slow arXiv day would make a single
# /api/health call (polled by /dashboard every 60s) exceed the timeout and
# gunicorn would SIGKILL the worker mid-check. Two prongs make that impossible:
#   1. A WALL-CLOCK BUDGET on the check loop: once the budget is spent, the
#      remaining networked checks are reported as `skipped` (not run, not
#      failed) so a health call can never approach the worker timeout.
#   2. A per-worker SHORT-TTL CACHE for the networked probes: the dashboard's
#      60s poll reuses the last probe result instead of re-hitting arXiv/google
#      every time, so the slow path is rare (first call / TTL expiry) and the
#      recurring cost is sub-second.
# The arXiv per-fetch socket timeout was also lowered 15s -> 5s (research.py) so
# a single in-flight check — the cold digest, the one check that can be slow by
# itself — is hard-bounded under the budget. urllib's socket timeout is a hard
# limit, so a fully-down arXiv cannot push one check past 5s per category.
_API_HEALTH_BUDGET_SECONDS = 20   # hard cap on one /api/health call (< gunicorn 30s)
_HEALTH_PROBE_TTL_SECONDS = 90    # how long a networked probe result is reused
# The health checks that make an OUTBOUND network call when the health check
# re-runs them in-process, matched by exact route string. Every endpoint that
# reaches the internet (arXiv) or the platform (10.0.0.18 stats/notebook) is
# here; everything else is in-process (git / file / JSONL) and always runs.
# Caching them all means the dashboard's 60s poll reuses the last result instead
# of re-hitting the network ~8 times per poll, and the budget protects against
# any one of them (a dead arXiv, a hung internal host) holding a worker.
_HEALTH_NETWORKED = {
    "/api/stats",
    "/api/network",
    "/api/notebook",
    "/api/research",
    "/api/research/search?q=ai",
    "/api/egress",
    "/api/research/export?format=json",
    "/api/research/search/export?q=ai&format=json",
}
# route -> (monotonic ts, item). Only OK networked results are cached; failures
# are re-probed every call (they fail fast, so re-probing is cheap) so a
# recovery is never hidden behind a cached green. Per-worker (each gunicorn
# worker has its own), so no cross-user data and no lock needed.
_HEALTH_PROBE_CACHE = {}


def _health_reset_probe_cache():
    """Clear the per-worker networked-probe cache (used by tests)."""
    _HEALTH_PROBE_CACHE.clear()


@app.route("/api/health")
def api_health():
    """Self-verify every API endpoint: HTTP 200 + valid JSON + expected key/type,
    and (where flagged) a non-null payload. Returns per-endpoint status so a
    broken endpoint is surfaced immediately instead of being assumed healthy.

    Bounded: a wall-clock budget caps the whole call, and the networked probes
    are short-TTL cached, so this endpoint can never exceed the worker timeout
    (see the comment above). A networked check the budget can't afford is
    reported as `skipped`, not run — the degradation is visible data, not a
    silent skip, and it does not flip `ok` (the API is not broken; the health
    check just ran out of time)."""
    import time as _time
    results = []
    healthy = True
    skipped = 0
    budget_exceeded = False
    t_start = _time.monotonic()
    with app.test_client() as client:
        for route, key, typ, non_null in _API_CHECKS:
            item = {"route": route, "ok": False, "status": None, "ms": None, "error": None}
            is_net = route in _HEALTH_NETWORKED
            # (2) Reuse a fresh networked probe result (the dashboard's 60s poll).
            if is_net:
                hit = _HEALTH_PROBE_CACHE.get(route)
                if hit and (_time.monotonic() - hit[0]) < _HEALTH_PROBE_TTL_SECONDS:
                    cached_item = dict(hit[1])
                    cached_item["cached"] = True
                    results.append(cached_item)
                    if not cached_item["ok"]:
                        healthy = False
                    continue
                # (1) Wall-clock budget: don't START a networked check we can't
                # finish in time. (A check already in-flight runs to completion —
                # it can't be interrupted — but the lowered socket timeouts keep
                # that bounded, so the in-flight + overhead stays under 30s.)
                if (_time.monotonic() - t_start) > _API_HEALTH_BUDGET_SECONDS:
                    item["error"] = ("skipped: /api/health time budget exceeded "
                                     "(not verified this pass)")
                    item["skipped"] = True
                    results.append(item)
                    skipped += 1
                    budget_exceeded = True
                    continue
            try:
                t0 = _time.monotonic()
                resp = client.get(route)
                item["ms"] = int((_time.monotonic() - t0) * 1000)
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
            # Cache a successful networked probe so the next poll reuses it.
            if is_net and item["ok"]:
                _HEALTH_PROBE_CACHE[route] = (_time.monotonic(), dict(item))
            results.append(item)
            if not item["ok"]:
                healthy = False

    wall_ms = int((_time.monotonic() - t_start) * 1000)
    return jsonify({
        "ok": healthy,
        "checked": len(results),
        "passed": sum(1 for r in results if r["ok"]),
        # `failed` counts checks that RAN and failed; skipped (budget) and
        # cached (ok) checks are not failures.
        "failed": sum(1 for r in results if (not r["ok"]) and not r.get("skipped")),
        "skipped": skipped,
        "budget_exceeded": budget_exceeded,
        "wall_ms": wall_ms,
        "budget_ms": _API_HEALTH_BUDGET_SECONDS * 1000,
        "endpoints": results,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
