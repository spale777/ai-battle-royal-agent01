"""Pytest config: make the project root importable and expose shared fixtures.

Run from the project root:  .venv/bin/python -m pytest -q
"""
import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so `import app` / `import research` work
# regardless of the current working directory pytest is invoked from.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def app_module():
    import app
    return app


@pytest.fixture
def client(app_module):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def network_mocks(monkeypatch, app_module):
    """Patch every outbound-network dependency with a valid in-memory payload so
    tests are hermetic and fast. With these in place, /api/health should report
    a fully green (10/10) API surface."""
    import research as research_module

    monkeypatch.setattr(
        app_module, "get_stats",
        lambda: {"pageviews": 10, "visitors": 2, "visits": 3, "bounces": 0,
                 "total_time_seconds": 42, "referrers": [{"host": "example", "visitors": 2}]},
    )
    monkeypatch.setattr(
        app_module, "get_notebook",
        lambda: {"edition": 1, "built_at": "2026-01-01T00:00:00+00:00",
                 "entries": [{"agent": "agent-02", "timestamp": "2026-01-01", "body": "hello"}]},
    )
    monkeypatch.setattr(
        research_module, "get_research_digest",
        lambda: {"cached_at": "2026-01-01T00:00:00+00:00",
                 "papers": [{"title": "Test Paper", "arxiv_id": "1", "published": "2026-01-01",
                             "authors": "A", "summary": "s", "categories": ["cs.AI"],
                             "primary_category": "cs.AI"}],
                 "total": 1, "stale": False},
    )
    yield
