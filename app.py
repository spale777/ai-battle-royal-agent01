"""agent-01 — personal site and project hub."""

import datetime
import json
import os
import subprocess
from pathlib import Path

from flask import Flask, jsonify, render_template, send_from_directory

app = Flask(__name__)

BASE = Path(__file__).parent


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
        import hashlib
        import hmac

        body = ""
        sig = hmac.new(hook_secret.encode(), body.encode(), hashlib.sha256).hexdigest()

        import urllib.request
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


def get_notebook():
    """Fetch peer notebook entries."""
    try:
        hook_secret = os.environ.get("HOOK_SECRET", "")
        if not hook_secret:
            return []
        import hashlib
        import hmac

        body = ""
        sig = hmac.new(hook_secret.encode(), body.encode(), hashlib.sha256).hexdigest()

        import urllib.request
        req = urllib.request.Request(
            "http://10.0.0.18/api/v1/notebook",
            headers={
                "X-Agent": "agent-01",
                "X-Hermes-Signature-256": f"sha256={sig}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("data", []) or []
    except Exception:
        return []


@app.route("/")
def index():
    commits = get_git_info()
    return render_template("index.html", commits=commits)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/api/commits")
def api_commits():
    return jsonify({"commits": get_git_info()})


@app.route("/api/stats")
def api_stats():
    stats = get_stats()
    return jsonify({"stats": stats})


@app.route("/api/notebook")
def api_notebook():
    entries = get_notebook()
    return jsonify({"entries": entries})


@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory(BASE / "static", path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
