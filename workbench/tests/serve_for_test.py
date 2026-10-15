# -*- coding: utf-8 -*-
"""Hold a FreeCAD process open with agent access running, so the MCP bridge can be tested for real.

    CK_SERVE_SECONDS=45 freecadcmd.exe workbench/tests/serve_for_test.py

Used by `test_agent_end_to_end.py`, which runs on the HOST interpreter and talks to this process
through `mcp_bridge.py`. The point is to exercise the actual boundary an agent crosses — two
processes, two Python versions, a socket and a discovery file — rather than a mock of it.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import commands  # registers the vocabulary  # noqa: E402,F401
from ckwb import server  # noqa: E402

# Duration comes from the environment, not argv. Under freecadcmd, sys.argv[1] is the SCRIPT
# PATH -- passing "45" after the filename gets read as the script and the real argument is lost.
SECONDS = float(os.environ.get("CK_SERVE_SECONDS", "45"))

doc = FreeCAD.newDocument("AgentTest")
FreeCAD.setActiveDocument("AgentTest")

srv = server.Server(base_port=19931)
srv.start()
print(f"SERVING port={srv.port} file={srv.registry_file}", flush=True)
try:
    time.sleep(SECONDS)
finally:
    srv.stop()
    print("STOPPED", flush=True)
