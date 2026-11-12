#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s [--check]\n' "$0"
}

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

check_only=false
case "${1:-}" in
    "") ;;
    --check) check_only=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { usage >&2; exit 2; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
runtime="$root/.runtime"
source_dir="$runtime/tau-official-source"
venv="$runtime/tau-official-venv"
manifest="$root/data/tau/manifest.json"
upstream="https://github.com/sierra-research/tau2-bench.git"
export GIT_OPTIONAL_LOCKS=0
export PYTHONDONTWRITEBYTECODE=1

command -v git >/dev/null || fail "Git is required."

if [[ -x "$venv/bin/python" ]]; then
    python="$venv/bin/python"
elif "$check_only"; then
    fail "Official environment is missing. Run this script without --check to install it."
elif [[ -e "$venv" || -L "$venv" ]]; then
    fail "Existing official environment is incomplete. Move it aside before installing."
else
    command -v uv >/dev/null || fail "uv is required to install the official environment."
    uv python install 3.12
    python="$(uv python find --no-project --managed-python --system 3.12)"
fi

revision="$("$python" - "$manifest" <<'PY'
import json
import re
import sys
from pathlib import Path

revision = json.loads(Path(sys.argv[1]).read_text())["revision"]
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit("Invalid revision in the τ-bench manifest")
print(revision)
PY
)"

if [[ -e "$source_dir" || -L "$source_dir" ]]; then
    [[ -d "$source_dir/.git" ]] || fail "Existing official source is not a Git checkout."
    [[ "$(git -C "$source_dir" rev-parse HEAD)" == "$revision" ]] ||
        fail "Existing official checkout has a different revision. Move it aside to reinstall."
    [[ -z "$(git -C "$source_dir" status --porcelain)" ]] ||
        fail "Existing official checkout has local changes. Preserve or move it aside before reinstalling."
elif "$check_only"; then
    fail "Official source checkout is missing."
else
    mkdir -p "$runtime"
    checkout="$(mktemp -d "$runtime/tau-official-checkout.XXXXXX")"
    trap 'rm -rf -- "$checkout"' EXIT
    git -C "$checkout" init -q
    git -C "$checkout" remote add origin "$upstream"
    git -C "$checkout" fetch --depth 1 origin "$revision"
    git -C "$checkout" checkout --detach FETCH_HEAD
    [[ "$(git -C "$checkout" rev-parse HEAD)" == "$revision" ]] ||
        fail "Downloaded checkout has an unexpected revision."
    [[ ! -e "$source_dir" && ! -L "$source_dir" ]] ||
        fail "Official source appeared during checkout. Re-run the check."
    mv "$checkout" "$source_dir"
    trap - EXIT
fi

"$python" - "$manifest" "$source_dir" <<'PY'
from hashlib import sha256
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
for name, spec in manifest["files"].items():
    upstream_path = spec["source_url"].split(f'/{manifest["revision"]}/', 1)[1]
    if sha256((root / upstream_path).read_bytes()).hexdigest() != spec["sha256"]:
        raise SystemExit(f"Official source hash mismatch for {name}")
print(f'Official source {manifest["revision"]} verified ({len(manifest["files"])} file hashes).')
PY

if [[ ! -x "$venv/bin/python" ]]; then
    UV_PROJECT_ENVIRONMENT="$venv" uv sync --project "$source_dir" \
        --locked --no-dev --no-install-project --python "$python"
fi

"$venv/bin/python" - "$source_dir/uv.lock" <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

if sys.version_info[:2] != (3, 12):
    raise SystemExit("Official environment requires Python 3.12")

import tomllib

lock = tomllib.loads(Path(sys.argv[1]).read_text())
for name in ("litellm", "openai", "pydantic", "tiktoken"):
    expected = {pkg["version"] for pkg in lock["package"] if pkg["name"] == name}
    try:
        actual = version(name)
    except PackageNotFoundError:
        raise SystemExit(f"Official environment is missing {name}") from None
    if actual not in expected:
        raise SystemExit(f"Official environment has {name} {actual}, outside the upstream lock")
print("Official Python 3.12 and campaign dependency versions verified.")
PY
