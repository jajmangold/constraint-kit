#!/usr/bin/env bash
# constraint-kit CI — the canonical "run all checks" entrypoint (gates pushes via .githooks/pre-push).
# exit 0 = all green.
#
# Two tiers, on purpose. The physics tier needs only numpy + scipy, so it runs anywhere and is
# ALWAYS gated. The kernel suite needs the container stack (build123d, bd_warehouse, cq_gears, z3,
# python_solvespace, trimesh); on a bare host it fails 27 of 113 on imports, which is expected and
# must not be read as a regression. So it runs only when cadkit is actually up, and its absence is
# reported as skipped rather than passed -- a CI run that silently tests less than you think is
# worse than one that fails.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
run() {   # run <label> <cmd...>
  local label="$1"; shift
  echo "[ci] $label ..."
  if "$@"; then
    echo "[ci] $label PASS"
  else
    echo "[ci] $label FAIL"
    fail=1
  fi
}

run "physics tier (tests/test_fea.py)"          python tests/test_fea.py
run "convergence (tests/test_convergence.py)"   python tests/test_convergence.py

if docker exec cadkit true >/dev/null 2>&1; then
  run "kernel suite (tests/run.sh, in cadkit)"  bash tests/run.sh
else
  echo "[ci] kernel suite SKIPPED — the cadkit container is not running."
  echo "[ci]   start it with: docker compose -f cadkit/docker-compose.yaml up -d"
  echo "[ci]   the 113 kernel tests are NOT covered by this run."
fi

if [ "$fail" -ne 0 ]; then
  echo "[ci] FAILED"
  exit 1
fi
echo "[ci] OK"
