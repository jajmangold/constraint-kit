#!/usr/bin/env python3
"""End-to-end: an agent drives a live FreeCAD workbench through the MCP bridge.

    python workbench/tests/test_agent_end_to_end.py

Runs on the HOST interpreter — Python 3.13 here — and talks to FreeCAD's Python 3.11 through the
real boundary: a discovery file, a loopback socket, and `mcp_bridge.py` speaking MCP on stdio.
Nothing is mocked, because the thing most likely to break is exactly the boundary a mock removes.

Starts `serve_for_test.py` under freecadcmd itself, so the test is self-contained.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.dirname(HERE)
BRIDGE = os.path.join(WB, "mcp_bridge.py")
SERVER_SCRIPT = os.path.join(HERE, "serve_for_test.py")

FREECADCMD = os.environ.get(
    "FREECADCMD", r"C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe")

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def mcp(*messages, timeout=90):
    """Run the bridge, feed it MCP messages on stdin, return the parsed replies."""
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run([sys.executable, BRIDGE], input=stdin, capture_output=True,
                          text=True, timeout=timeout)
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    if not out and proc.stderr:
        raise RuntimeError(f"bridge produced nothing; stderr: {proc.stderr[:400]}")
    return out


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}


@test
def test_the_bridge_speaks_mcp_without_any_freecad_running():
    """initialize must succeed before a workbench exists; an agent handshakes first, then looks."""
    replies = mcp(INIT)
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05", replies
    assert replies[0]["result"]["capabilities"]["tools"] == {}, replies
    assert replies[0]["result"]["serverInfo"]["name"] == "freecad-constraint-kit"


@test
def test_tools_list_fails_helpfully_with_nothing_running():
    replies = mcp(INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    err = replies[-1]
    assert "error" in err, err
    assert "no running" in err["error"]["message"].lower(), err


@test
def test_an_agent_builds_and_inspects_a_model_in_a_live_freecad():
    """The whole point: a separate process, over MCP, builds geometry and reads provenance back."""
    assert os.path.exists(FREECADCMD), f"freecadcmd not found at {FREECADCMD}"
    env = dict(os.environ, CK_SERVE_SECONDS="90")
    proc = subprocess.Popen([FREECADCMD, SERVER_SCRIPT], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # Wait on the DISCOVERY FILE, not on stdout. freecadcmd buffers its output when it is
        # not a tty, so a readline loop can block past any sane timeout -- and polling the
        # registry directory exercises the mechanism an agent actually uses to find a workbench.
        from importlib.machinery import SourceFileLoader
        bridge = SourceFileLoader("ck_bridge", BRIDGE).load_module()
        deadline = time.time() + 90
        found = []
        while time.time() < deadline and not found:
            found = bridge.instances()
            if not found:
                time.sleep(0.5)
        assert found, (
            f"no instance appeared in {bridge.registry_dir()} within 90 s; "
            f"freecad output so far: {proc.stdout.read(400) if proc.poll() else '(running)'}")
        port = found[0]["port"]

        listed = mcp(INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[-1]
        tools = {t["name"]: t for t in listed["result"]["tools"]}
        assert "insert_part" in tools and "get_params" in tools, sorted(tools)
        # MCP's camelCase, translated from the registry's snake_case
        assert "inputSchema" in tools["insert_part"], tools["insert_part"]
        assert "modifies the model" in tools["insert_part"]["description"], \
            tools["insert_part"]["description"]
        assert "modifies" not in tools["get_tree"]["description"]

        def call(name, args, mid):
            reply = mcp(INIT, {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})[-1]
            assert not reply["result"].get("isError"), reply["result"]["content"][0]["text"]
            return json.loads(reply["result"]["content"][0]["text"])

        made = call("insert_part", {"generator": "spur_gear", "name": "G1"}, 3)
        assert made["name"] == "G1", made
        assert made["guessed"], "a gear built from defaults should report guessed values"

        call("set_param", {"part": "G1", "name": "teeth", "value": 30}, 4)
        params = call("get_params", {"part": "G1"}, 5)
        assert params["params"]["teeth"]["value"] == 30, params["params"]["teeth"]
        assert params["params"]["teeth"]["provenance"] == "stated", params["params"]["teeth"]
        assert params["params"]["module"]["provenance"] == "default"

        tree = call("get_tree", {}, 6)
        assert [p["name"] for p in tree["parts"]] == ["G1"], tree
        assert tree["parts"][0]["volume_mm3"] > 0

        checks = call("run_checks", {}, 7)
        assert checks["ok"] is True, checks
        state = call("get_checks", {}, 8)
        assert state["stale"] is False, state

        # An error must arrive as a readable message, not as a dead connection.
        reply = mcp(INIT, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                           "params": {"name": "get_params",
                                      "arguments": {"part": "Nope"}}})[-1]
        assert reply["result"]["isError"] is True, reply
        assert "Nope" in reply["result"]["content"][0]["text"], reply
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


@test
def test_the_discovery_file_is_removed_when_freecad_exits():
    """A stale entry would send the next agent at a dead port."""
    from importlib.machinery import SourceFileLoader
    bridge = SourceFileLoader("ck_bridge", BRIDGE).load_module()
    assert bridge.instances() == [], (
        f"a stale instance is still advertised in {bridge.registry_dir()}")


def main():
    passed, failed = 0, []
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{passed}/{passed + len(failed)} passed" + (f"; FAILED: {failed}" if failed else ""))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
