#!/usr/bin/env python3
"""MCP stdio bridge: lets Claude Code, Codex or opencode drive a running FreeCAD workbench.

    python workbench/mcp_bridge.py            # serve MCP on stdio (what an agent runs)
    python workbench/mcp_bridge.py --list     # show which FreeCAD instances are reachable

Why a bridge rather than MCP inside FreeCAD: MCP's stdio transport owns a process's stdin and
stdout, and FreeCAD's belong to FreeCAD. So the workbench publishes a loopback JSON-RPC socket
and this script — plain stdlib, runs on any Python 3.9+ — speaks MCP on stdio and forwards.

**It discovers the instance, it never assumes a port.** `~/.ck/instances/*.json` is written by
each running workbench. That matters because several FreeCAD windows are a normal way to work,
and guessing a fixed port is how this project previously spent a day on two instances silently
sharing 9875 under SO_REUSEADDR: netstat showed two PIDs listening, the OS gave every connection
to one of them, and the other was unreachable with no error anywhere.

Register it with Claude Code as:

    claude mcp add freecad -- python <path>/workbench/mcp_bridge.py

The tool list is generated from the workbench's own command registry, so an agent sees exactly
the vocabulary the toolbar uses — including `get_params`, which reports where every value came
from, and `get_checks`, which reports whether a stored verdict has gone stale.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

PROTOCOL_VERSION = "2024-11-05"
NAME = "freecad-constraint-kit"
VERSION = "0.1.0"
TIMEOUT = 120.0


def registry_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".ck", "instances")


def instances() -> list:
    out = []
    d = registry_dir()
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            continue
        entry["_file"] = os.path.join(d, name)
        if _reachable(entry.get("host", "127.0.0.1"), entry.get("port")):
            out.append(entry)
    return out


def _reachable(host: str, port) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def pick(prefer_document: str | None = None) -> dict:
    found = instances()
    if not found:
        raise RuntimeError(
            "no running Constraint Kit workbench found. Start FreeCAD, switch to the Constraint "
            "Kit workbench, then Settings > Start agent access. Entries appear in "
            f"{registry_dir()}")
    if prefer_document:
        for entry in found:
            if entry.get("document") == prefer_document:
                return entry
    return found[0]


def call(entry: dict, method: str, params: dict | None = None) -> dict:
    """One JSON-RPC round trip to the workbench, newline framed."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}}) + "\n"
    with socket.create_connection((entry.get("host", "127.0.0.1"), int(entry["port"])),
                                  timeout=TIMEOUT) as sock:
        sock.sendall(payload.encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise RuntimeError("the workbench closed the connection without replying")
    return json.loads(buf.decode("utf-8"))


def _tools() -> list:
    """The workbench's registry schema, renamed to MCP's camelCase."""
    reply = call(pick(), "tools/list")
    if "error" in reply:
        raise RuntimeError(reply["error"]["message"])
    out = []
    for tool in reply["result"]["tools"]:
        description = tool["description"]
        if tool.get("writes"):
            description += "  [modifies the model; one undo transaction]"
        out.append({"name": tool["name"], "description": description,
                    "inputSchema": tool["input_schema"]})
    return out


def handle(msg: dict) -> dict | None:
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": VERSION},
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None                      # notifications take no reply
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        try:
            return _ok(rid, {"tools": _tools()})
        except Exception as exc:  # noqa: BLE001
            return _err(rid, -32000, str(exc))
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            reply = call(pick(), "tools/call", {"name": name, "arguments": args})
        except Exception as exc:  # noqa: BLE001
            return _ok(rid, _text(f"{type(exc).__name__}: {exc}", error=True))
        if "error" in reply:
            return _ok(rid, _text(reply["error"]["message"], error=True))
        return _ok(rid, _text(json.dumps(reply["result"], indent=2)))
    return _err(rid, -32601, f"unknown method {method!r}")


def _text(body: str, error: bool = False) -> dict:
    # Tool failures come back as content with isError, not as a protocol error: the agent is
    # meant to read the message and try something else, not treat the server as broken.
    return {"content": [{"type": "text", "text": body}], "isError": bool(error)}


def _ok(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            sys.stdout.write(json.dumps(_err(None, -32700, f"bad JSON: {exc}")) + "\n")
            sys.stdout.flush()
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list reachable workbench instances")
    args = ap.parse_args(argv)

    if args.list:
        found = instances()
        if not found:
            print(f"No reachable instance. Looked in {registry_dir()}")
            return 1
        for entry in found:
            print(f"pid {entry['pid']:>7}  {entry['host']}:{entry['port']}  "
                  f"doc={entry.get('document') or '-'}  started {entry.get('started', '?')}")
        return 0

    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
