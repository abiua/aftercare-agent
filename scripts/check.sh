#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

case "${1:-pure}" in
  pure)
    if [[ $# -gt 1 ]]; then
      printf '%s\n' 'Usage: scripts/check.sh [pure|pg]' >&2
      exit 2
    fi
    PYTHONPATH="$repo/src:$repo/backend/src:$repo" uv run --project "$repo/backend" --frozen --no-sync pytest -q tests
    uv run --project "$repo/backend" --frozen --no-sync python scripts/verify_dataset.py
    (cd backend && uv run --frozen --no-sync pytest -m 'not pg')
    (cd frontend && npm run lint && npm test && npm run build)
    ;;
  pg)
    if [[ $# -ne 1 || "${AFTERCARE_RUN_PG:-0}" != 1 || "${AFTERCARE_CONTAINER:-0}" != 0 ]]; then
      printf '%s\n' 'Run only on the host with AFTERCARE_RUN_PG=1 and the isolated test profile running.' >&2
      exit 2
    fi
    (
      cd backend
      AFTERCARE_PROFILE=test uv run --frozen --no-sync python - <<'PY'
import fcntl
import os
import subprocess
import sys

from aftercare.config import ROOT, load_settings
from aftercare.db import guarded_engine

settings = load_settings("test")
if settings.model_enabled:
    raise SystemExit("Refusing PostgreSQL checks with live models enabled.")
lock_path = ROOT / ".runtime" / "test-check.lock"
with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another isolated test check holds the lock.") from None
    engine = guarded_engine(settings, test_only=True)
    engine.dispose()
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-m", "pg"]))
PY
    )
    ;;
  *)
    printf '%s\n' 'Usage: scripts/check.sh [pure|pg]' >&2
    exit 2
    ;;
esac
