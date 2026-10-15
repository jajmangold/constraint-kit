#!/usr/bin/env python3
"""Commands, the agent server and the physics bridge — runs INSIDE FreeCAD.

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_commands.py

The rule under test is the workbench's one architectural commitment: **everything the interface
does is a command, and agents call the same ones.** So the tests check that the registry is
complete and self-describing, that an agent reaching it over a socket gets identical results to
an in-process call, and that discovery never assumes a port — this project has already lost a
day to two FreeCAD instances silently sharing 9875 under SO_REUSEADDR.

The physics bridge is checked against beam theory rather than against itself, reusing the
already-verified `constraint_kit.fea` tier. That tier needs only numpy and scipy, which is
exactly why it can run in here at all.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import commands as K  # noqa: E402
from ckwb import document as D  # noqa: E402
from ckwb import server as S  # noqa: E402
from ckwb.registry import CommandError, UnknownCommand, registry  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_doc(name="cmd"):
    if name in FreeCAD.listDocuments():
        FreeCAD.closeDocument(name)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(name)
    return doc


# ------------------------------------------------------------------------------- the registry

@test
def test_the_registry_is_populated_and_self_describing():
    schema = registry.schema()
    names = {t["name"] for t in schema}
    for expected in ("list_generators", "insert_part", "set_param", "get_tree",
                     "get_params", "run_checks", "join", "export", "save_params"):
        assert expected in names, f"{expected} is not registered; have {sorted(names)}"
    for tool in schema:
        assert tool["description"], f"{tool['name']} has no description for an agent to read"
        assert "input_schema" in tool and "properties" in tool["input_schema"], tool["name"]


@test
def test_read_commands_are_not_marked_as_writes():
    by_name = {t["name"]: t for t in registry.schema()}
    for read_only in ("list_generators", "get_tree", "get_params", "run_checks", "export"):
        assert by_name[read_only]["writes"] is False, read_only
    for writer in ("insert_part", "set_param", "join", "promote_param"):
        assert by_name[writer]["writes"] is True, writer


@test
def test_list_generators_reports_every_part_with_its_defaults():
    out = registry.call("list_generators")
    assert "plate" in out and "spur_gear" in out, sorted(out)
    assert out["plate"]["defaults"]["width"] == 60.0, out["plate"]


@test
def test_insert_part_then_get_tree_sees_it():
    new_doc()
    made = registry.call("insert_part", generator="spacer", name="Sp")
    assert made["name"] == "Sp" and made["generator"] == "spacer", made
    tree = registry.call("get_tree")
    assert [p["name"] for p in tree["parts"]] == ["Sp"], tree
    assert tree["parts"][0]["volume_mm3"] > 0


@test
def test_get_params_exposes_provenance_so_an_agent_sees_the_guesses():
    new_doc()
    registry.call("insert_part", generator="spacer", name="Sp")
    out = registry.call("get_params", part="Sp")
    assert out["generator"] == "spacer"
    assert out["summary"].endswith("guessed"), out["summary"]
    assert out["params"]["od"]["provenance"] == "default", out["params"]["od"]


@test
def test_set_param_rebuilds_geometry_and_marks_the_value_stated():
    doc = new_doc()
    registry.call("insert_part", generator="spacer", name="Sp")
    before = doc.getObject("Sp").Shape.Volume
    registry.call("set_param", part="Sp", name="height", value=20.0)
    after = doc.getObject("Sp").Shape.Volume
    assert after > before, (before, after)
    assert registry.call("get_params", part="Sp")["params"]["height"]["provenance"] == "stated"


@test
def test_promote_param_accepts_a_guess():
    new_doc()
    registry.call("insert_part", generator="spacer", name="Sp")
    registry.call("promote_param", part="Sp", name="od")
    assert registry.call("get_params", part="Sp")["params"]["od"]["provenance"] == "stated"


@test
def test_join_seats_two_parts_with_a_zero_gap():
    doc = new_doc()
    registry.call("insert_part", generator="plate", name="Base")
    registry.call("insert_part", generator="spacer", name="Sp")
    out = registry.call("join", kind="seat", target="Base", target_frame="top",
                        moving="Sp", moving_frame="bottom")
    assert abs(out["gap_mm"]) < 1e-9, out
    assert abs(doc.getObject("Sp").Placement.Base.z - 8.0) < 1e-9


@test
def test_join_reports_what_frames_exist_when_given_a_bad_one():
    new_doc()
    registry.call("insert_part", generator="plate", name="Base")
    registry.call("insert_part", generator="spacer", name="Sp")
    try:
        registry.call("join", kind="seat", target="Base", target_frame="ceiling",
                      moving="Sp", moving_frame="bottom")
    except Exception as exc:
        assert "ceiling" in str(exc) and "top" in str(exc), str(exc)
        return
    raise AssertionError("a bad frame name must raise and list the real ones")


@test
def test_run_checks_returns_structured_results_with_a_fingerprint():
    new_doc()
    registry.call("insert_part", generator="plate", name="A")
    out = registry.call("run_checks")
    assert out["ok"] is True, out
    assert set(out["checks"]) >= {"solids", "interference", "mass"}, sorted(out["checks"])
    assert out["checks"]["interference"]["fingerprint"], out["checks"]["interference"]


@test
def test_get_checks_reports_staleness_rather_than_a_cached_green():
    doc = new_doc()
    registry.call("insert_part", generator="plate", name="A")
    registry.call("run_checks")
    fresh = registry.call("get_checks")
    assert fresh["stale"] is False, fresh
    registry.call("set_param", part="A", name="thickness", value=20.0)
    after = registry.call("get_checks")
    assert after["stale"] is True, "an edit must make the stored verdict stale"


@test
def test_commands_fail_clearly_with_no_document_open():
    for name in list(FreeCAD.listDocuments()):
        FreeCAD.closeDocument(name)
    try:
        registry.call("get_tree")
    except Exception as exc:
        assert "document" in str(exc).lower(), str(exc)
        return
    raise AssertionError("with no document open, a command must say so")


@test
def test_export_writes_a_step_file():
    new_doc()
    registry.call("insert_part", generator="plate", name="A")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "a.step")
        out = registry.call("export", path=path)
        assert os.path.exists(path) and os.path.getsize(path) > 500, out
        assert out["parts"] == 1


@test
def test_save_params_writes_beside_the_document_and_reloads():
    doc = new_doc()
    registry.call("insert_part", generator="spacer", name="Sp")
    registry.call("set_param", part="Sp", name="od", value=33.0)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "doc.ckparts.json")
        registry.call("save_params", path=path)
        raw = json.load(open(path, encoding="utf-8"))
        assert raw["parts"][0]["params"]["od"]["value"] == 33.0, raw
        fresh = new_doc("cmd2")
        registry.call("load_params", path=path)
        assert abs(fresh.getObject("Sp").Shape.Volume
                   - doc.getObject("Sp").Shape.Volume) < 1e-6


# ----------------------------------------------------------------------------- agent server

@test
def test_server_picks_a_free_port_and_publishes_where_it_is():
    """Never assume a port. Two instances sharing 9875 under SO_REUSEADDR cost a day here once."""
    new_doc()
    srv = S.Server(base_port=19875)
    srv.start()
    try:
        assert 19875 <= srv.port < 19975, srv.port
        entry = json.load(open(srv.registry_file, encoding="utf-8"))
        assert entry["port"] == srv.port and entry["pid"] == os.getpid(), entry
        assert entry["workbench"] == "ConstraintKit", entry
    finally:
        srv.stop()
    assert not os.path.exists(srv.registry_file), "stopping must remove the registry entry"


@test
def test_two_servers_do_not_land_on_the_same_port():
    new_doc()
    a, b = S.Server(base_port=19875), S.Server(base_port=19875)
    a.start()
    try:
        b.start()
        try:
            assert a.port != b.port, (a.port, b.port)
        finally:
            b.stop()
    finally:
        a.stop()


def _rpc(port, method, params=None):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}}) + "\n"
        sock.sendall(msg.encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


@test
def test_an_agent_over_the_socket_gets_the_same_answers_as_an_in_process_call():
    new_doc()
    srv = S.Server(base_port=19875)
    srv.start()
    try:
        listed = _rpc(srv.port, "tools/list")
        assert "result" in listed, listed
        names = {t["name"] for t in listed["result"]["tools"]}
        assert names == set(registry.names()), names ^ set(registry.names())

        made = _rpc(srv.port, "tools/call",
                    {"name": "insert_part", "arguments": {"generator": "plate", "name": "Ag"}})
        assert made["result"]["name"] == "Ag", made
        tree = _rpc(srv.port, "tools/call", {"name": "get_tree", "arguments": {}})
        assert [p["name"] for p in tree["result"]["parts"]] == ["Ag"], tree
    finally:
        srv.stop()


@test
def test_the_server_reports_errors_as_json_rpc_not_as_a_dropped_connection():
    new_doc()
    srv = S.Server(base_port=19875)
    srv.start()
    try:
        bad = _rpc(srv.port, "tools/call", {"name": "nope", "arguments": {}})
        assert "error" in bad and "nope" in bad["error"]["message"], bad
        missing = _rpc(srv.port, "tools/call",
                       {"name": "set_param", "arguments": {"part": "X"}})
        assert "error" in missing, missing
        unknown = _rpc(srv.port, "frobnicate")
        assert "error" in unknown, unknown
    finally:
        srv.stop()


# --------------------------------------------------------------------------- physics bridge

@test
def test_physics_bridge_reproduces_beam_theory_on_a_plate():
    """A cantilever from the document, meshed and solved by the already-verified fea tier."""
    from ckwb import physics

    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Beam")
    obj.Width = 120.0
    obj.Depth = 12.0
    obj.Thickness = 12.0
    doc.recompute()

    out = physics.stiffness(obj, material="al6061", pitch=2.0,
                            fix="x<=-59", load="x>=59", force=(0.0, 0.0, -200.0))
    analytic = 200.0 * 120.0 ** 3 / (3.0 * 68900.0 * (12.0 ** 4 / 12.0))
    print(f"        voxel {out['max_disp_mm']:.4f} mm vs beam theory {analytic:.4f} mm "
          f"({out['max_disp_mm'] / analytic:.0%})")
    assert 0.45 * analytic < out["max_disp_mm"] < 1.10 * analytic, (out, analytic)
    assert out["elements"] > 500 and out["dof"] > 1000, out
    assert out["stress_p99_mpa"] > 0 and out["mass_g"] > 0


@test
def test_physics_refuses_a_selector_that_matches_no_material():
    from ckwb import physics
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Beam")
    doc.recompute()
    try:
        physics.stiffness(obj, material="petg", pitch=3.0,
                          fix="x<=-9999", load="x>=59", force=(0.0, 0.0, -10.0))
    except Exception as exc:
        assert "no nodes" in str(exc).lower() or "match" in str(exc).lower(), str(exc)
        return
    raise AssertionError("a restraint that selects nothing must raise, not solve a free body")


# --------------------------------------------------------------------------- the interface spec

@test
def test_every_toolbar_button_is_backed_by_a_registered_command():
    """The architectural rule, enforced. A button that bypasses the registry is unreachable by
    an agent, and the two interfaces would drift apart from that moment on."""
    from ckwb.gui import spec
    for cmd in spec.backing_commands():
        assert cmd in registry, f"toolbar runs {cmd!r}, which is not a registered command"


@test
def test_the_toolbar_is_eight_entries_with_no_duplicates():
    """Eight, deliberately. The rest of the vocabulary lives in the palette, not in a button farm
    of greyed-out groups."""
    from ckwb.gui import spec
    ids = spec.toolbar_ids()
    assert len(ids) == 8, ids
    assert len(set(ids)) == len(ids), "duplicate toolbar id"
    for row in spec.TOOLBAR:
        assert len(row) == 6, row
        assert row[1] and row[2], f"{row[0]} needs a label and a tooltip"


@test
def test_palette_only_commands_all_exist():
    from ckwb.gui import spec
    for cmd in spec.PALETTE_ONLY:
        assert cmd in registry, f"palette lists {cmd!r}, which is not registered"


@test
def test_every_registered_command_is_reachable_from_the_interface():
    """No orphans: a command reachable by agents but by no human is a half-built feature."""
    from ckwb.gui import spec
    reachable = set(spec.backing_commands()) | set(spec.PALETTE_ONLY)
    # These are opened through a picker or dialog rather than run directly by one button.
    via_dialog = {"insert_part", "join", "export", "run_checks"}
    orphans = set(registry.names()) - reachable - via_dialog
    assert not orphans, f"registered but unreachable from the UI: {sorted(orphans)}"


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
    if failed:
        sys.exit(1)


# No __main__ guard: freecadcmd imports a script as a module named after its file.
main()
