#!/usr/bin/env bash
# constraint-kit CI — the canonical "run all checks" entrypoint (gates pushes via .githooks/pre-push).
# exit 0 = all green.
#
# Three tiers, each with a different dependency, and the point of this script is that it never
# silently reports a tier it did not run:
#
#   host      numpy + scipy only          physics, convergence, workbench core  -- ALWAYS gated
#   freecad   freecadcmd                  geometry, document, commands, agents  -- gated if found
#   gui       a real FreeCAD window       toolbar, panels, the planner UI       -- CK_GUI=1
#   container docker exec cadkit          the 113 kernel tests                  -- gated if up
#
# The gui tier opens a window for ~30 s, so it is opt-in (CK_GUI=1) rather than on by default --
# but it is not optional in spirit. Two defects shipped in one day while the Qt layer had no
# coverage, both invisible to every headless test, because FreeCADGui cannot be imported without
# a display. Run it before calling GUI work done.
#
# A tier that cannot run is reported as SKIPPED, never as passed. A CI run that covers less than
# you think is worse than one that fails.
set -uo pipefail
cd "$(dirname "$0")/.."

FREECADCMD="${FREECADCMD:-/c/Program Files/FreeCAD 1.0/bin/freecadcmd.exe}"

fail=0
ran=0
skipped=()

run() {   # run <label> <cmd...>
  local label="$1"; shift
  echo "[ci] $label ..."
  if "$@" >/tmp/ck-ci.$$.log 2>&1; then
    echo "[ci] $label PASS  ($(grep -cE '^  PASS' /tmp/ck-ci.$$.log) tests)"
    ran=$((ran + 1))
  else
    echo "[ci] $label FAIL"
    grep -E '^  FAIL|AssertionError|Error' /tmp/ck-ci.$$.log | head -12
    fail=1
  fi
  rm -f /tmp/ck-ci.$$.log
}

echo "=== host tier (numpy + scipy) ==="
run "physics          tests/test_fea.py"            python tests/test_fea.py
run "convergence      tests/test_convergence.py"    python tests/test_convergence.py
run "workbench core   workbench/tests/test_core.py" python workbench/tests/test_core.py

echo "=== freecad tier ==="
if [ -x "$FREECADCMD" ]; then
  run "geometry        workbench/tests/test_geometry.py" "$FREECADCMD" workbench/tests/test_geometry.py
  run "document        workbench/tests/test_document.py" "$FREECADCMD" workbench/tests/test_document.py
  run "commands        workbench/tests/test_commands.py" "$FREECADCMD" workbench/tests/test_commands.py
  run "planner         workbench/tests/test_planner.py"  "$FREECADCMD" workbench/tests/test_planner.py
  run "step import     workbench/tests/test_import.py"   "$FREECADCMD" workbench/tests/test_import.py
  run "motion          workbench/tests/test_motion.py"   "$FREECADCMD" workbench/tests/test_motion.py
  run "agent end-to-end workbench/tests/test_agent_end_to_end.py" \
      python workbench/tests/test_agent_end_to_end.py
else
  echo "[ci] freecad tier SKIPPED — freecadcmd not found at:"
  echo "[ci]   $FREECADCMD"
  echo "[ci]   set FREECADCMD to its path. The workbench geometry, document, command and agent"
  echo "[ci]   suites are NOT covered by this run."
  skipped+=("freecad")
fi

echo "=== gui tier ==="
if [ "${CK_GUI:-0}" = "1" ]; then
  run "qt layer       workbench/tests/run_gui_tests.py"       python workbench/tests/run_gui_tests.py
else
  echo "[ci] gui tier SKIPPED — set CK_GUI=1 to run it (opens a FreeCAD window for ~30 s)."
  echo "[ci]   the toolbar, the three panels and the planner UI are NOT covered by this run."
  skipped+=("gui")
fi

echo "=== container tier ==="
if docker exec cadkit true >/dev/null 2>&1; then
  run "kernel          tests/run.sh (in cadkit)" bash tests/run.sh
else
  echo "[ci] container tier SKIPPED — the cadkit container is not running."
  echo "[ci]   start it with: docker compose -f cadkit/docker-compose.yaml up -d"
  echo "[ci]   the 113 kernel tests are NOT covered by this run."
  skipped+=("container")
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "[ci] FAILED"
  exit 1
fi
echo "[ci] OK — $ran suite(s) green${skipped[*]+, skipped: ${skipped[*]}}"
