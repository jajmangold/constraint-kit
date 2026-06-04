#!/usr/bin/env bash
# constraint-kit CI — the canonical "run all checks" entrypoint (gates pushes via .githooks/pre-push).
# exit 0 = all green. Runs the dependency-free regression suite inside the live cadkit container.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "[ci] regression suite (inside cadkit) ..."
bash tests/run.sh
echo "[ci] OK"
