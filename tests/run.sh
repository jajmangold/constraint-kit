#!/usr/bin/env bash
# Run the constraint-kit regression suite inside the live cadkit container.
# Exit code is the suite's (0 = all pass), so this can gate changes / CI.
set -euo pipefail
exec docker exec cadkit python3 ${CK_WORK_DIR:-/tmp/constraint-kit}/tests/test_kernel.py
