#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
target="${1:-all}"
tag="${AFTERCARE_IMAGE_TAG:-local}"
case "$target" in
  all|backend|frontend) ;;
  *) printf '%s\n' 'Usage: scripts/build_images.sh [all|backend|frontend]' >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then
  printf '%s\n' 'Usage: scripts/build_images.sh [all|backend|frontend]' >&2
  exit 2
fi

if [[ -z "${DOCKER_BUILDKIT+x}" ]] && ! docker buildx version >/dev/null 2>&1; then
  export DOCKER_BUILDKIT=0
fi

stage_root="$(mktemp -d "${TMPDIR:-/tmp}/aftercare-images.XXXXXX")"
trap 'rm -r "$stage_root"' EXIT

build_image() {
  local image="$1" context="$stage_root/$1"
  mkdir "$context"
  python3 - "$repo" "$context" "$image" <<'PY'
from pathlib import Path
import sys

root, context = map(Path, sys.argv[1:3])
image = sys.argv[3]
fixed = {
    "backend": (
        "infra/Dockerfile.backend",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/alembic.ini",
        "backend/migrations/README",
        "backend/migrations/script.py.mako",
        "data/v0.1/manifest.json",
        "data/v0.1/agent/business.json",
        "data/v0.2/manifest.json",
    ),
    "frontend": (
        "infra/Dockerfile.frontend",
        "infra/nginx.conf",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/index.html",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "frontend/public/favicon.svg",
    ),
}
patterns = {
    "backend": ("backend/src/aftercare/**/*.py", "backend/migrations/**/*.py"),
    "frontend": ("frontend/src/**/*.ts", "frontend/src/**/*.tsx", "frontend/src/**/*.css"),
}

inputs = {root / name for name in fixed[image]}
for pattern in patterns[image]:
    inputs.update(root.glob(pattern))
for source in sorted(inputs):
    relative = source.relative_to(root)
    if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
        continue
    if not source.is_file() or source.resolve() != source:
        raise SystemExit(f"Build input must be a regular file without symlinks: {relative}")
    target = context / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
print(f"Staged allowlisted {image} inputs.", flush=True)
PY
  docker build --tag "aftercare-$image:$tag" --file "$context/infra/Dockerfile.$image" "$context"
}

case "$target" in
  backend|frontend) build_image "$target" ;;
  all) build_image backend; build_image frontend ;;
esac
