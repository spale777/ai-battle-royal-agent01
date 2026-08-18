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

from flask import Flask, Response, jsonify, render_template, send_from_directory

app = Flask(__name__)

BASE = Path(__file__).parent
START_TIME = datetime.datetime.now(datetime.timezone.utc)
STATS_HISTORY_PATH = BASE / "stats_history.jsonl"


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


@app.route("/network")
def network():
    agents = get_agents_data()
    import json as _json
    return render_template("network.html", agents_json=_json.dumps(agents))


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

        records = json.loads(result.stdout)
        lang_stats = {}
        all_files = []

        for rec in records:
            lang = rec.get("language", "Unknown")
            path = rec.get("path", "")
            code = rec.get("code", 0)
            comments = rec.get("comment", 0)

            if lang not in lang_stats:
                lang_stats[lang] = {"name": lang, "files": 0, "code": 0, "comments": 0}
            lang_stats[lang]["files"] += 1
            lang_stats[lang]["code"] += code
            lang_stats[lang]["comments"] += comments

            all_files.append({
                "file": str(Path(path).relative_to(BASE)),
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


@app.route("/api/stats")
def api_stats():
    stats = get_stats()
    record_stats_history(stats)
    return jsonify({"stats": stats})


@app.route("/api/stats/history")
def api_stats_history():
    history = get_stats_history()
    return jsonify({"history": history})


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
