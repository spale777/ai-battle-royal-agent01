#!/usr/bin/env bash
# Run the project's test suite. Exits non-zero on any failure so it can be
# wired into a git pre-push hook (see .githooks/pre-push) or a CI step.
#
# Rationale: several earlier sessions shipped endpoints that returned null or
# 404 and were caught only after the fact. Making the suite a hard gate on
# push turns "shipped" into "shipped and verified".
set -euo pipefail
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi

echo "Running test suite with $PY ..."
"$PY" -m pytest -q
