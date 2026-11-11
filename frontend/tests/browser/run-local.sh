#!/usr/bin/env bash
set -euo pipefail

runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
frontend_dir="$(cd "$runner_dir/../.." && pwd)"
cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/aftercare-agent/browser-fixtures"
mkdir -p "$cache_dir"
stage="$(mktemp -d "$cache_dir/run.XXXXXX")"
trap 'rm -rf -- "$stage"' EXIT

python3 - "$frontend_dir" "$stage/frontend" <<'PY'
from pathlib import Path
import sys

source, target = map(Path, sys.argv[1:])
paths = [
    Path(name) for name in (
        "package.json", "package-lock.json", "index.html", "eslint.config.js",
        "tsconfig.json", "vite.config.ts",
    )
]
for folder, extensions in (
    ("src", {".ts", ".tsx", ".css"}),
    ("public", {".svg"}),
    ("tests/browser/specs", {".mjs"}),
):
    paths.extend(
        path.relative_to(source)
        for path in (source / folder).rglob("*")
        if path.suffix in extensions
    )
paths.extend(
    Path("tests/browser") / name for name in (
        "package.json", "package-lock.json", "fixture-server.mjs",
        "fixture-settings.mjs", "playwright.config.mjs",
    )
)
for path in paths:
    destination = target / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((source / path).read_bytes())
PY

cd "$stage/frontend"
npm ci --prefer-offline --no-audit --no-fund
npm run lint
npm run build

cd tests/browser
npm ci --prefer-offline --no-audit --no-fund
if [[ "${AFTERCARE_INSTALL_FIXTURE_BROWSER:-0}" == 1 ]]; then
    npm run install:browser
fi
echo "Temporary browser fixture build: $(du -sk "$stage" | cut -f1) KiB"
npm test -- "$@"
