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
def test_starting_a_server_prunes_entries_whose_port_is_dead():
    """A killed process leaves its entry behind; a test harness does that routinely."""
    os.makedirs(S.registry_dir(), exist_ok=True)
    stale = os.path.join(S.registry_dir(), "999999-19875.json")
    with open(stale, "w", encoding="utf-8") as fh:
        json.dump({"pid": 999999, "port": 19874, "host": "127.0.0.1",
                   "workbench": "ConstraintKit", "document": None}, fh)
    assert os.path.exists(stale)

    new_doc()
    srv = S.Server(base_port=19875)
    srv.start()
    try:
        assert not os.path.exists(stale), "a dead entry must be pruned when a server starts"
        assert os.path.exists(srv.registry_file), "our own entry must survive the prune"
    finally:
        srv.stop()


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


@test
def test_analyse_is_a_registered_command_so_an_agent_can_reach_the_physics():
    """The gap this closes: the physics tier was reachable only by typing Python by hand.

    `physics.stiffness` existed and was tested, but it was not a registered command, so an agent
    driving the workbench over MCP could insert a part, mate it, check it for interference and
    export it — and could not find out whether it was strong enough. A closed loop from
    parameters to performance cannot exist while the loop is not connected, which makes this the
    smallest change with the largest reach.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Beam")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 12.0, 12.0
    doc.recompute()

    out = registry.call("analyse", part="Beam", material="al6061", pitch=2.0,
                        fix="x<=-59", load="x>=59", force=[0.0, 0.0, -200.0])
    analytic = 200.0 * 120.0 ** 3 / (3.0 * 68900.0 * (12.0 ** 4 / 12.0))
    assert 0.45 * analytic < out["max_disp_mm"] < 1.10 * analytic, (out, analytic)
    assert out["part"] == "Beam" and out["material"] == "al6061"
    assert out["safety_factor"] > 0 and out["mass_g"] > 0
    # it is a read: a verdict must not move the model
    assert out.get("fingerprint"), "an analysis must be stamped like the other checks"
    assert [t for t in registry.schema() if t["name"] == "analyse"][0]["writes"] is False


@test
def test_analyse_reports_the_interlayer_verdict_for_a_printed_material():
    """A printed part has a weak direction, and the command must say so without being asked.

    Leaving this behind a flag would mean the default answer for a printed part is the one that
    presumes isotropy — which is the wrong default, because for the knee-exo gantry plate the
    welds govern at SF 0.74 in the worst orientation against 1.63 in the best.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Printed")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 12.0, 12.0
    doc.recompute()

    out = registry.call("analyse", part="Printed", material="petg", pitch=2.0,
                        fix="x<=-59", load="x>=59", force=[0.0, 0.0, -40.0])
    layers = out.get("interlayer")
    assert layers, "a printed material must carry an interlayer verdict: " + repr(out)
    assert set(layers) >= {"X", "Y", "Z"}, sorted(layers)
    for name, entry in layers.items():
        assert {"peel_mpa", "shear_mpa", "sf_peel", "sf_shear", "governing"} <= set(entry), entry
    assert out["best_print_orientation"] in ("X", "Y", "Z"), out["best_print_orientation"]
    # bending about Z: the layers worth avoiding are the ones normal to the bending stress
    assert out["factors"]["tensile"] > 0.0 and out["factors"]["shear"] > 0.0, out["factors"]
    assert "assum" in out["interlayer_note"].lower(), out["interlayer_note"]

    # and NOT for an isotropic one, where the number would mean nothing
    iso = registry.call("analyse", part="Printed", material="al6061", pitch=2.0,
                        fix="x<=-59", load="x>=59", force=[0.0, 0.0, -40.0])
    assert iso.get("interlayer") is None, iso.get("interlayer")


@test
def test_analyse_says_so_when_the_deflection_leaves_linear_theory_behind():
    """Found by using it: a 4 mm bracket reported 124 mm of deflection, on a 120 mm part.

    Linear small-displacement elasticity assumes rotations stay small enough that the geometry
    does not change as the load is applied. A deflection on the order of the span breaks that
    outright: the real structure stiffens as it deflects and the linear answer overshoots,
    sometimes by a lot. The number is not merely imprecise, it is the answer to a different
    question.

    The criterion is deflection over SPAN, not over thickness -- rotation is what matters, and a
    cantilever can move through several times its own thickness at a few degrees of rotation. A
    thickness-based rule was tried first and flagged a valid 4.5%-of-span answer, which is how a
    warning gets trained out of a reader.

    Nothing in this tier can solve the nonlinear problem, and that is fine. What is not fine is
    returning 124 mm with the same confidence as 5 mm, which is the plausible-failure mode
    AGENTS.md rule 4 exists to prevent. So the verdict carries whether linear theory still holds.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Floppy")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 20.0, 4.0
    doc.recompute()

    big = registry.call("analyse", part="Floppy", material="petg", pitch=2.0,
                        fix="x<=-58", load="x>=58", force=[0.0, 0.0, -60.0])
    assert big["max_disp_mm"] > 20.0, big["max_disp_mm"]
    assert big["linear_valid"] is False, big
    assert "linear" in big["linear_note"].lower(), big["linear_note"]
    # it must say what it is large COMPARED TO, or the reader cannot judge it
    assert "span" in big["linear_note"].lower(), big["linear_note"]
    assert "120" in big["linear_note"], big["linear_note"]

    # and a stiff version of the same part must NOT carry the warning
    obj.Thickness = 12.0
    doc.recompute()
    small = registry.call("analyse", part="Floppy", material="petg", pitch=2.0,
                          fix="x<=-58", load="x>=58", force=[0.0, 0.0, -60.0])
    assert small["max_disp_mm"] < 10.0, small["max_disp_mm"]
    assert small["linear_valid"] is True, small
    assert small.get("linear_note") in (None, ""), small.get("linear_note")


@test
def test_analyse_takes_several_load_cases_and_envelopes_them():
    """A machine part does not have *a* load, and quoting one case's SF is a guess.

    The envelope is elementwise, so the reported safety factor is never better than the worst
    case — a part is safe only if it is safe everywhere under every load it will actually see.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Beam")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 20.0, 12.0
    doc.recompute()

    out = registry.call("analyse", part="Beam", material="al6061", pitch=3.0,
                        fix="x<=-58",
                        cases=[{"name": "down", "load": "x>=58", "force": [0, 0, -400]},
                               {"name": "side", "load": "x>=58", "force": [0, 300, 0]},
                               {"name": "both", "load": "x>=58", "force": [0, 300, -400]}])
    assert set(out["cases"]) == {"down", "side", "both"}, sorted(out["cases"])
    for name, row in out["cases"].items():
        assert row["max_disp_mm"] > 0 and row["stress_p99_mpa"] > 0, (name, row)

    # the envelope bounds every case, so its SF is no better than the worst of them
    worst_case_sf = min(15000.0 / r["stress_p99_mpa"] for r in out["cases"].values())
    assert out["safety_factor"] <= min(
        165.0 / r["stress_p99_mpa"] for r in out["cases"].values()) + 1e-6, out
    assert worst_case_sf > 0
    assert out["governing_case"] in out["cases"], out["governing_case"]
    assert out["governs_by_element"], out
    assert sum(out["governs_by_element"].values()) == out["elements"], out

    # "both" is the vector sum of the other two, so it must govern
    assert out["governing_case"] == "both", out["governing_case"]


@test
def test_analyse_refuses_cases_mixed_with_a_single_load():
    """Two ways of saying the same thing, with different answers, is not a convenience."""
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Beam")
    doc.recompute()
    try:
        registry.call("analyse", part="Beam", material="al6061", pitch=4.0, fix="x<=-28",
                      load="x>=28", force=[0, 0, -100],
                      cases=[{"name": "a", "load": "x>=28", "force": [0, 0, -100]}])
    except Exception as exc:                                        # noqa: BLE001
        assert "cases" in str(exc).lower(), str(exc)
        return
    raise AssertionError("load/force alongside cases is ambiguous and must be refused")


@test
def test_analyse_refuses_a_malformed_case():
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Beam")
    doc.recompute()
    for bad, expect in (
            ([{"load": "x>=28", "force": [0, 0, -1]}], "name"),
            ([{"name": "a", "force": [0, 0, -1]}], "load"),
            ([{"name": "a", "load": "x>=28"}], "force"),
            ([{"name": "a", "load": "x>=28", "force": [0, 0]}], "force")):
        try:
            registry.call("analyse", part="Beam", material="al6061", pitch=4.0,
                          fix="x<=-28", cases=bad)
        except Exception as exc:                                    # noqa: BLE001
            assert expect in str(exc).lower(), (bad, str(exc))
            continue
        raise AssertionError("%s should have been refused" % bad)


@test
def test_optimise_sizes_a_real_part_to_its_constraint_and_restores_the_document():
    """The loop closed all the way: parameters -> geometry -> physics -> better parameters.

    A PETG bracket, free to be between 4 and 20 mm thick, asked for the lightest version that
    keeps a safety factor of 1.5. The answer must be ON the constraint (a lighter one would fail,
    a heavier one is wasted material), and the command must put the part back how it found it --
    it is declared read-only, and a search that silently leaves the model at its last trial
    position is a side effect wearing a verdict as a disguise.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Bracket")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 20.0, 10.0
    doc.recompute()
    before = obj.Thickness

    out = registry.call(
        "optimise", part="Bracket", material="petg", pitch=2.0, tol=1.0,
        fix="x<=-58", load="x>=58", force=[0.0, 0.0, -60.0],
        parameters=[{"name": "thickness", "low": 4.0, "high": 20.0}],
        objective="mass_g",
        constraints=[{"name": "strength", "metric": "safety_factor", "op": ">=",
                      "limit": 1.5}])

    assert out["feasible"] is True, out
    t = out["best"]["thickness"]
    assert 4.0 < t < 20.0, t
    assert out["metrics"]["safety_factor"] >= 1.5 - 1e-3, out["metrics"]
    # Tight against the constraint -- but only to within what the MESH can resolve. A voxel grid
    # quantises the thickness, so SF is a staircase in it: at 2 mm pitch the treads are 2 mm
    # wide, and the optimum can only sit on the first tread that passes. Demanding more than
    # that of the search would be demanding it of the mesh, and an earlier version of this test
    # did exactly that and failed against entirely correct code.
    assert out["metrics"]["safety_factor"] < 1.5 * 1.8, (
        "the optimum is more than a mesh tread inside the constraint: %s"
        % out["metrics"]["safety_factor"])
    assert not out["warnings"], (
        "tol 1.0 against pitch 2.0 needs no warning: %s" % out["warnings"])
    # `blocking`, not `active`: the mesh quantises the thickness, so the best feasible point
    # sits on a tread strictly inside the constraint rather than exactly on it -- measured 21%
    # inside. Strength is still unambiguously what refused every lighter design the search
    # tried, and that is the useful claim and a provable one.
    assert out["blocking_constraints"] == ["strength"], (
        out["blocking_constraints"], out["metrics"]["safety_factor"])
    assert out["evaluations"] >= 2, out["evaluations"]
    assert "local" in out["report"].lower(), out["report"]

    assert abs(obj.Thickness - before) < 1e-9, (
        "the document was left at %s instead of the %s it started at"
        % (obj.Thickness, before))


@test
def test_optimise_warns_when_the_mesh_is_too_coarse_for_the_tolerance():
    """A search tolerance finer than the mesh can resolve is chasing a plateau.

    Measured at 4 mm pitch on a 20 mm wide plate: thickness 7.0 and thickness 10.0 sample to the
    same 300 elements and report the same safety factor to three decimals, because the voxeliser
    rounds the thickness to a whole number of voxels. The search is then walking across a flat
    tread and will stop wherever that tread ends, with an answer good to about one pitch. Saying
    so is the difference between a result and a number.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Bracket")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 20.0, 10.0
    doc.recompute()

    out = registry.call(
        "optimise", part="Bracket", material="petg", pitch=4.0, tol=0.05,
        fix="x<=-58", load="x>=58", force=[0.0, 0.0, -60.0],
        parameters=[{"name": "thickness", "low": 4.0, "high": 20.0}],
        objective="mass_g",
        constraints=[{"name": "strength", "metric": "safety_factor", "op": ">=", "limit": 1.0}])
    assert out["warnings"], "a 0.05 mm tolerance on a 4 mm mesh must be called out"
    joined = " ".join(out["warnings"]).lower()
    assert "pitch" in joined and "quantised" in joined, out["warnings"]


@test
def test_mass_comes_from_the_exact_geometry_not_the_voxel_count():
    """A staircase is the wrong thing to weigh, and the gap between the two is a mesh signal.

    The voxel count rounds the geometry to whole cells, so a voxel-derived mass is a step
    function of the parameters -- which makes it both a wrong number to quote and a terrible
    objective to minimise. The exact B-rep volume is free and smooth. Both are reported, because
    their difference says whether this pitch is resolving the part at all.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Slab")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 20.0, 9.0
    doc.recompute()

    out = registry.call("analyse", part="Slab", material="petg", pitch=4.0,
                        fix="x<=-58", load="x>=58", force=[0.0, 0.0, -60.0])
    exact = 120.0 * 20.0 * 9.0 / 1000.0 * 1.27          # cm3 -> g at PETG density
    assert abs(out["mass_g"] - exact) < 1e-2, (out["mass_g"], exact)
    assert out["voxel_mass_g"] != out["mass_g"], (
        "at 4 mm pitch on a 9 mm plate the staircase cannot match the exact volume")
    assert out["voxel_volume_error_pct"] is not None
    assert abs(out["voxel_volume_error_pct"]) > 1.0, out["voxel_volume_error_pct"]

    # and the exact mass must move when the thickness moves, even inside one voxel tread
    D.set_param(obj, "thickness", 10.0)
    doc.recompute()
    again = registry.call("analyse", part="Slab", material="petg", pitch=4.0,
                          fix="x<=-58", load="x>=58", force=[0.0, 0.0, -60.0])
    assert again["mass_g"] > out["mass_g"] + 1e-6, (
        "the exact mass is flat across a voxel tread, so it is still the voxel count")
    assert again["voxel_mass_g"] == out["voxel_mass_g"], (
        "this fixture needs the two thicknesses to voxelise identically, or it proves nothing")


@test
def test_optimise_can_be_constrained_to_designs_the_physics_is_valid_for():
    """Without this, the lightest design in the box is very often one the solver cannot judge.

    The physics is linear, so a part that deflects through a tenth of its own span is outside
    what the solve can speak about -- and "lightest" pushes straight at that region, because
    thin is light. `linear_valid` is reported as 1.0 / 0.0 so it can be constrained like
    anything else, and the two runs below differ by exactly that constraint.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Floppy")
    obj.Width, obj.Depth, obj.Thickness = 160.0, 20.0, 12.0
    doc.recompute()

    common = dict(part="Floppy", material="petg", pitch=4.0, tol=1.0,
                  fix="x<=-78", load="x>=78", force=[0.0, 0.0, -80.0],
                  parameters=[{"name": "thickness", "low": 4.0, "high": 24.0, "step": 2.0}],
                  objective="mass_g", max_evaluations=40)

    loose = registry.call("optimise", constraints=[], **common)
    assert loose["feasible"] is True, loose
    # with nothing to satisfy, "lightest" walks to the thinnest allowed -- which is exactly the
    # region where the linear answer stops meaning anything
    assert loose["best"]["thickness"] <= 6.0, loose["best"]
    assert loose["metrics"]["linear_valid"] == 0.0, (
        "this fixture needs the unconstrained optimum to be outside linear validity, or it "
        "proves nothing: %s" % loose["metrics"])

    strict = registry.call(
        "optimise",
        constraints=[{"name": "small deflection", "metric": "linear_valid",
                      "op": ">=", "limit": 1.0}],
        **common)
    assert strict["feasible"] is True, strict
    assert strict["metrics"]["linear_valid"] == 1.0, strict["metrics"]
    assert strict["best"]["thickness"] > loose["best"]["thickness"], (
        strict["best"], loose["best"])
    assert strict["blocking_constraints"] == ["small deflection"], \
        strict["blocking_constraints"]


@test
def test_analyse_finds_the_buckling_factor_of_a_strut_and_matches_euler():
    """The failure mode a stress check is blind to, through the command surface.

    A slender strut sits nowhere near its stress allowable right up until it folds -- this one
    reports a safety factor of 19 while being within a factor of two of collapse. Checked against
    `pi^2 EI / (2L)^2` for a fixed-free column, which is ground truth from outside this repo.
    """
    import math

    doc = new_doc()
    obj = D.make_part(doc, "shaft", None, name="Strut")
    obj.Diameter, obj.Length = 10.0, 160.0
    doc.recompute()

    out = registry.call("analyse", part="Strut", material="al6061", pitch=3.0,
                        fix="z<=-79", load="z>=79", force=[0.0, 0.0, -900.0], buckling=2)
    I = math.pi * 10.0 ** 4 / 64.0
    euler = math.pi ** 2 * 68900.0 * I / (2.0 * 160.0) ** 2
    expect = euler / 900.0
    print("        factor %.3f vs Euler %.3f (x%.3f), stress SF %.1f"
          % (out["buckling_factor"], expect, out["buckling_factor"] / expect,
             out["safety_factor"]))

    assert out["buckles"] is True, out
    assert 0.9 < out["buckling_factor"] / expect < 1.25, (out["buckling_factor"], expect)
    assert len(out["buckling_factors"]) == 2, out["buckling_factors"]
    # a round section has two identical first modes, by symmetry
    assert abs(out["buckling_factors"][0] - out["buckling_factors"][1]) < 1e-3, \
        out["buckling_factors"]
    # and the point of the whole exercise: stress says it is fine
    assert out["safety_factor"] > 5.0, (
        "this fixture needs stress to look comfortable while stability does not: %s"
        % out["safety_factor"])
    assert "upper bound" in out["buckling_note"].lower(), out["buckling_note"]


@test
def test_analyse_says_a_part_in_tension_does_not_buckle_rather_than_failing():
    doc = new_doc()
    obj = D.make_part(doc, "shaft", None, name="Tie")
    obj.Diameter, obj.Length = 10.0, 160.0
    doc.recompute()
    out = registry.call("analyse", part="Tie", material="al6061", pitch=4.0,
                        fix="z<=-79", load="z>=79", force=[0.0, 0.0, +900.0], buckling=1)
    assert out["buckles"] is False, out
    assert out["buckling_factor"] is None, out
    assert "tension" in out["buckling_note"].lower(), out["buckling_note"]


@test
def test_optimise_can_require_a_buckling_margin():
    """Making something light is exactly what finds a stability failure, so it must be sayable.

    The same problem with and without a buckling constraint: unconstrained, "lightest" walks the
    strut thin enough to be unstable at the design load; constrained, it stops at a margin.
    """
    doc = new_doc()
    obj = D.make_part(doc, "shaft", None, name="Strut")
    obj.Diameter, obj.Length = 12.0, 160.0
    doc.recompute()

    common = dict(part="Strut", material="al6061", pitch=4.0, tol=1.0,
                  fix="z<=-79", load="z>=79", force=[0.0, 0.0, -900.0],
                  parameters=[{"name": "diameter", "low": 6.0, "high": 20.0, "step": 2.0}],
                  objective="mass_g", buckling=1, max_evaluations=30)

    loose = registry.call("optimise", constraints=[], **common)
    strict = registry.call(
        "optimise",
        constraints=[{"name": "stability", "metric": "buckling_factor", "op": ">=",
                      "limit": 2.0}],
        **common)

    assert loose["feasible"] and strict["feasible"], (loose, strict)
    assert loose["best"]["diameter"] <= 8.0, loose["best"]
    assert strict["best"]["diameter"] > loose["best"]["diameter"], (
        strict["best"], loose["best"])
    assert strict["metrics"]["buckling_factor"] >= 2.0 - 1e-6, strict["metrics"]
    assert strict["blocking_constraints"] == ["stability"], strict["blocking_constraints"]
    print("        unconstrained %.0f mm, with a 2x buckling margin %.0f mm"
          % (loose["best"]["diameter"], strict["best"]["diameter"]))


@test
def test_analyse_reports_printability_and_the_optimiser_can_constrain_it():
    """Issue #18: a manufacturing rule is a CONSTRAINT, not an audit run afterwards.

    An optimizer told to minimise mass subject to strength will return a wall too thin to
    extrude, because nothing in its problem statement said otherwise. Here the same problem is
    run twice, differing only by `printable >= 1`, and the answers differ accordingly.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Panel")
    obj.Width, obj.Depth, obj.Thickness = 60.0, 40.0, 6.0
    doc.recompute()

    out = registry.call("analyse", part="Panel", material="petg", pitch=1.0,
                        fix="x<=-28", load="x>=28", force=[0.0, 0.0, -20.0],
                        rules={"min_wall_mm": 3.0, "max_overhang_deg": 45.0})
    for key in ("unsupported_area_mm2", "thin_fraction", "fits_build_volume", "printable"):
        assert key in out, (key, sorted(out))
    assert out["printable"] == 1.0, (out["printability_violations"], out["printability_notes"])
    assert out["unsupported_area_mm2"] == 0.0, out
    assert out["printability_rules"]["min_wall_mm"] == 3.0, out["printability_rules"]

    common = dict(part="Panel", material="petg", pitch=1.0, tol=0.5,
                  fix="x<=-28", load="x>=28", force=[0.0, 0.0, -20.0],
                  parameters=[{"name": "thickness", "low": 1.0, "high": 10.0, "step": 1.0}],
                  objective="mass_g",
                  rules={"min_wall_mm": 4.0, "max_overhang_deg": 45.0},
                  max_evaluations=30)
    loose = registry.call("optimise", constraints=[], **common)
    strict = registry.call(
        "optimise",
        constraints=[{"name": "printable", "metric": "printable", "op": ">=", "limit": 1.0}],
        **common)

    assert loose["feasible"] and strict["feasible"], (loose, strict)
    assert loose["best"]["thickness"] <= 2.0, (
        "unconstrained, lightest walks to the thinnest allowed: %s" % loose["best"])
    assert strict["best"]["thickness"] > loose["best"]["thickness"], (
        strict["best"], loose["best"])
    assert strict["metrics"]["printable"] == 1.0, strict["metrics"]
    assert strict["blocking_constraints"] == ["printable"], strict["blocking_constraints"]
    print("        unconstrained %.0f mm, printable-constrained %.0f mm"
          % (loose["best"]["thickness"], strict["best"]["thickness"]))


@test
def test_optimise_refuses_a_parameter_the_part_does_not_have():
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Bracket")
    doc.recompute()
    try:
        registry.call("optimise", part="Bracket", material="petg", pitch=6.0,
                      fix="x<=-28", load="x>=28", force=[0, 0, -10],
                      parameters=[{"name": "wibble", "low": 1.0, "high": 2.0}],
                      objective="mass_g", constraints=[])
    except Exception as exc:                                        # noqa: BLE001
        assert "wibble" in str(exc), str(exc)
        assert "thickness" in str(exc), "the real parameters should be listed: " + str(exc)
        return
    raise AssertionError("an unknown parameter must be refused, with the real ones named")


@test
def test_optimise_reports_an_unreachable_requirement_rather_than_the_nearest_miss():
    """A safety factor nothing in the box can reach is a "no", and the "no" has to be legible."""
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Bracket")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 20.0, 6.0
    doc.recompute()
    out = registry.call(
        "optimise", part="Bracket", material="petg", pitch=6.0,
        fix="x<=-58", load="x>=58", force=[0.0, 0.0, -600.0],
        parameters=[{"name": "thickness", "low": 4.0, "high": 6.0}],
        objective="mass_g",
        constraints=[{"name": "strength", "metric": "safety_factor", "op": ">=",
                      "limit": 20.0}])
    assert out["feasible"] is False, out
    assert out["best"], "the nearest miss must still be reported, so the gap is visible"
    assert out["violations"], out
    assert "strength" in " ".join(out["violations"]), out["violations"]


@test
def test_analyse_refuses_an_unknown_part_and_an_unknown_material():
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Beam")
    doc.recompute()
    for kwargs, expect in (
            ({"part": "Nope", "material": "petg"}, "nope"),
            ({"part": "Beam", "material": "unobtainium"}, "unobtainium")):
        try:
            registry.call("analyse", pitch=4.0, fix="x<=-29", load="x>=29",
                          force=[0.0, 0.0, -10.0], **kwargs)
        except Exception as exc:                                        # noqa: BLE001
            assert expect in str(exc).lower(), (kwargs, str(exc))
            continue
        raise AssertionError("%s should have been refused" % kwargs)


@test
def test_analyse_appears_in_the_mcp_schema_with_its_arguments():
    """An agent can only call what the schema advertises."""
    tools = {t["name"]: t for t in registry.schema()}
    assert "analyse" in tools, sorted(tools)
    props = tools["analyse"]["input_schema"]["properties"]
    assert {"part", "material", "pitch", "fix", "load", "force",
            "cases"} <= set(props), sorted(props)
    required = tools["analyse"]["input_schema"].get("required", [])
    # `part` and `fix` are unconditionally required. `load` is NOT, because `cases` is the other
    # way to say where the force goes -- the either/or is enforced at call time, where a schema
    # cannot express it, and the test for that refusal is right above.
    for name in ("part", "fix"):
        assert name in required, (name, required)
    assert "load" not in required, (
        "load must not be schema-required, or an agent reading the schema will conclude it "
        "cannot use cases")
    assert "cases" in props["cases"] or props["cases"].get("description"), props["cases"]
    assert "envelope" in props["cases"]["description"].lower() \
        or "elementwise" in props["cases"]["description"].lower(), props["cases"]
    # the description has to say the boundary conditions are the caller's to choose
    text = tools["analyse"]["description"].lower()
    assert "fix" in text or "boundary" in text or "held" in text, text


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
    via_dialog = {"insert_part", "join", "export", "run_checks",
                  # run by the Describe panel's Build button rather than by a toolbar entry
                  "describe"}
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
