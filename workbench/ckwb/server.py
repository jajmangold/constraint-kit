# -*- coding: utf-8 -*-
"""The agent server: MCP-shaped JSON-RPC over a loopback socket, plus honest discovery.

Agents (Claude Code, Codex, opencode) reach the workbench through this, and they reach exactly
the same `registry` the toolbar does — `tools/list` is the registry's generated schema and
`tools/call` is `registry.call`. There is no separate agent API to drift out of step.

**Discovery never assumes a port, and this is not a theoretical concern.** This project has
already lost a day to it: `SimpleXMLRPCServer` sets `allow_reuse_address`, so on Windows every
FreeCAD instance bound 127.0.0.1:9875 with SO_REUSEADDR, `netstat` showed two PIDs LISTENING on
the same port, the OS handed every connection to one of them, and the other was unreachable with
no error anywhere. So:

* `allow_reuse_address` is off on Windows (POSIX needs it, and there SO_REUSEADDR does not permit
  stealing a listening socket).
* The port is found by walking upward from a base, **connect-probing before binding** — because a
  bind alone cannot see a wildcard listener. On Windows a socket on 0.0.0.0:P does not block a
  second bind on 127.0.0.1:P even with SO_EXCLUSIVEADDRUSE, which is how one process can silently
  shadow another's port for loopback traffic.
* Each instance publishes `{pid, port, host, document, workbench, started}` to
  `~/.ck/instances/<pid>.json`, and removes it on stop. An agent reads the directory; it never
  guesses a number.

Loopback only. This binds 127.0.0.1 and nothing else: it executes geometry commands, and that is
not something to expose on a network interface.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import socketserver
import threading
import time

import FreeCAD

from .registry import CommandError, UnknownCommand, registry

HOST = "127.0.0.1"
BASE_PORT = 9931
PORT_SCAN = 100
WORKBENCH = "ConstraintKit"

#: JSON-RPC 2.0 error codes, plus a private one for a command that raised.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
COMMAND_FAILED = -32000


def registry_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".ck", "instances")


def list_instances() -> list:
    """Every live instance that has published itself. Stale entries are skipped, not trusted."""
    out = []
    d = registry_dir()
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def _port_is_free(port: int) -> bool:
    """Connect-probe, then trial-bind. Both, because neither alone is sufficient on Windows."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.08)
    try:
        if probe.connect_ex((HOST, port)) == 0:
            return False          # something is listening and talking
    finally:
        probe.close()

    test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            test.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        test.bind((HOST, port))
        return True
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", 10048)):
            return False
        raise
    finally:
        test.close()


class _Handler(socketserver.StreamRequestHandler):
    """One newline-delimited JSON-RPC message per line, one response per line."""

    timeout = 30

    def handle(self) -> None:
        for raw in self.rfile:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            self.wfile.write((json.dumps(self._dispatch(line)) + "\n").encode("utf-8"))
            self.wfile.flush()

    def _dispatch(self, line: str) -> dict:
        try:
            msg = json.loads(line)
        except ValueError as exc:
            return _err(None, PARSE_ERROR, f"malformed JSON: {exc}")
        if not isinstance(msg, dict):
            return _err(None, INVALID_REQUEST, "a request must be a JSON object")

        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        try:
            if method == "tools/list":
                return _ok(rid, {"tools": registry.schema()})
            if method == "instances":
                return _ok(rid, {"instances": list_instances()})
            if method == "ping":
                return _ok(rid, {"workbench": WORKBENCH, "pid": os.getpid()})
            if method == "tools/call":
                name = params.get("name")
                if not name:
                    return _err(rid, INVALID_PARAMS, "tools/call needs a tool name")
                return _ok(rid, registry.call(name, **(params.get("arguments") or {})))
            return _err(rid, METHOD_NOT_FOUND,
                        f"unknown method {method!r}; have tools/list, tools/call, instances, ping")
        except UnknownCommand as exc:
            return _err(rid, METHOD_NOT_FOUND, str(exc))
        except CommandError as exc:
            return _err(rid, INVALID_PARAMS, str(exc))
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            return _err(rid, COMMAND_FAILED, f"{type(exc).__name__}: {exc}")


def _ok(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


class _TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # Deliberately NOT True on Windows: SO_REUSEADDR there permits a silent duplicate bind, which
    # is the exact failure this module's docstring describes.
    allow_reuse_address = os.name != "nt"


class Server:
    """A running agent endpoint for this FreeCAD process."""

    def __init__(self, base_port: int = BASE_PORT, scan: int = PORT_SCAN):
        self.base_port = int(base_port)
        self.scan = int(scan)
        self.port: int | None = None
        self._srv = None
        self._thread = None

    @property
    def registry_file(self) -> str:
        return os.path.join(registry_dir(), f"{os.getpid()}-{self.base_port}.json")

    def start(self) -> int:
        if self._srv is not None:
            raise RuntimeError(f"already serving on {self.port}")
        for port in range(self.base_port, self.base_port + self.scan):
            if not _port_is_free(port):
                continue
            try:
                self._srv = _TCPServer((HOST, port), _Handler)
            except OSError:
                continue          # lost a race between the probe and the bind
            self.port = port
            break
        if self._srv is None:
            raise RuntimeError(
                f"no free port in {self.base_port}..{self.base_port + self.scan - 1}")

        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        name="ck-agent-server", daemon=True)
        self._thread.start()
        self._publish()
        return self.port

    def prune_dead(self) -> list:
        """Delete registry entries whose port no longer answers. Returns what was removed.

        A process killed rather than closed leaves its entry behind -- which a test harness does
        routinely, and a crash does too. Readers already filter by reachability so nobody is sent
        at a dead port, but the files would otherwise accumulate forever.

        Liveness is decided by connect-probing the port, NEVER by `os.kill(pid, 0)`: on Windows
        Python maps that to TerminateProcess, so the "is it alive" check would kill the very
        instance it was asking about.
        """
        removed = []
        d = registry_dir()
        if not os.path.isdir(d):
            return removed
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    entry = json.load(fh)
            except (OSError, ValueError):
                entry = None
            if entry and entry.get("pid") == os.getpid():
                continue                      # our own, possibly not yet listening
            port = (entry or {}).get("port")
            alive = False
            if port:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.settimeout(0.3)
                try:
                    alive = probe.connect_ex((entry.get("host", HOST), int(port))) == 0
                finally:
                    probe.close()
            if not alive:
                try:
                    os.remove(path)
                    removed.append(name)
                except OSError:
                    pass
        return removed

    def _publish(self) -> None:
        os.makedirs(registry_dir(), exist_ok=True)
        self.prune_dead()
        doc = FreeCAD.ActiveDocument
        payload = {
            "pid": os.getpid(),
            "port": self.port,
            "host": HOST,
            "workbench": WORKBENCH,
            "document": doc.Name if doc is not None else None,
            "file": getattr(doc, "FileName", "") if doc is not None else "",
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = self.registry_file + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.registry_file)   # atomic: a reader never sees a half-written entry

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            os.remove(self.registry_file)
        except OSError:
            pass
        self.port = None


#: The one server per FreeCAD process, started by the GUI command or by `ensure_started()`.
_INSTANCE: Server | None = None


def ensure_started(base_port: int = BASE_PORT) -> Server:
    global _INSTANCE
    if _INSTANCE is None or _INSTANCE.port is None:
        _INSTANCE = Server(base_port=base_port)
        _INSTANCE.start()
    return _INSTANCE


def current() -> Server | None:
    return _INSTANCE


def stop() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        _INSTANCE.stop()
        _INSTANCE = None
