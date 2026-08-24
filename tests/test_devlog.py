"""Dev-log journal contract — the public /devlog must have no session gap.

The journal is a visitor-facing surface (session records for sessions 1..N,
newest first). The failure mode this locks in: entries stop being written for
a stretch of sessions, leaving a hole that only the private HISTORY.md fills
(flagged by the observer in 2026-08-24: sessions 14-26 were missing). The file
is committed, so asserting against it is real, not mocked.
"""
import json
import re
from pathlib import Path

DEVLOG = Path(__file__).resolve().parent.parent / "devlog.jsonl"


def _entries():
    entries = []
    for line in DEVLOG.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def test_devlog_entries_are_valid_json_with_required_shape():
    entries = _entries()
    assert entries, "devlog.jsonl must not be empty"
    for e in entries:
        assert isinstance(e.get("date"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", e["date"]), e
        assert isinstance(e.get("session"), int), e
        notes = e.get("notes")
        assert isinstance(notes, list) and notes, f"session {e.get('session')}: notes must be a non-empty list"
        assert all(isinstance(n, str) and n.strip() for n in notes)


def test_devlog_sessions_are_strictly_ascending_with_no_gap():
    sessions = [e["session"] for e in _entries()]
    assert sessions == sorted(sessions), f"sessions out of order: {sessions}"
    assert len(sessions) == len(set(sessions)), f"duplicate sessions: {sessions}"
    # Continuous 1..N — a missing session number is a journal hole.
    assert sessions == list(range(1, len(sessions) + 1)), (
        f"gap in devlog sessions: {sessions} (must be 1..N continuous)"
    )


def test_devlog_page_renders_newest_first(client):
    resp = client.get("/devlog")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    newest = _entries()[-1]  # file is oldest-first; page renders newest-first
    assert f"session {newest['session']}" in html
    # Newest entry must appear BEFORE the session-1 entry in the page.
    pos_newest = html.index(f"session {newest['session']}")
    assert "session 1" in html
    assert pos_newest < html.index("session 1")
