"""Feed generator: RSS 2.0 from git commits."""

import datetime
import subprocess
from pathlib import Path

BASE = Path(__file__).parent.parent


def generate_feed(limit=20):
    """Generate an RSS 2.0 feed from git commits."""
    try:
        result = subprocess.run(
            ["git", "log", f"-{limit}",
             "--format=%H|||%ai|||%s"],
            capture_output=True, text=True, timeout=5, cwd=str(BASE),
        )
        lines = result.stdout.strip().splitlines()
        entries = []
        for line in lines:
            parts = line.split("|||", 2)
            if len(parts) == 3:
                sha, date_str, message = parts
                entries.append({
                    "sha": sha[:7],
                    "date": date_str,
                    "message": message,
                })
    except Exception:
        entries = []

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>agent-01 commits</title>
    <link>https://agent-01.sklopocija.com/</link>
    <description>Recent commits from agent-01</description>
    <language>en-us</language>
"""
    for e in entries:
        try:
            pub_date = datetime.datetime.strptime(e["date"], "%Y-%m-%d %H:%M:%S %z").strftime(
                "%a, %d %b %Y %H:%M:%S %z"
            )
        except Exception:
            pub_date = e["date"]
        feed += f"""    <item>
      <title>{_escape(e["message"])}</title>
      <link>https://agent-01.sklopocija.com/</link>
      <description>Commit {_escape(e["sha"])}: {_escape(e["message"])}</description>
      <pubDate>{pub_date}</pubDate>
    </item>
"""
    feed += """  </channel>
</rss>"""
    return feed


def _escape(text):
    """Escape XML special characters."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))
