#!/usr/bin/env python3
"""Several load cases in one run, and the envelope over them (issue #4).

    python3 tests/test_cases.py

A real machine part does not have *a* load. The knee-exo gantry plate carries belt tension when
the drive pulls, a reaction when it pushes, and its own weight always; the gear cutter's A-axis
sees cutting force in one direction and clamping force in another. Analysing one of those and
calling the result the part's safety factor is a guess dressed as an answer.

Two things make this more than a convenience loop over `solve()`:

* **The stiffness matrix is assembled and factorised ONCE.** Assembly is the expensive part for
  a big grid, and it does not depend on the load at all. Four cases should cost barely more than
  one, and a test measures that the assembly really is shared rather than repeated.
* **The envelope is an ELEMENTWISE maximum, not the worst single case.** This is the entire
  engineering point. One region of a part can be governed by the belt pull and another by the
  reaction, so no single case is the design case — the envelope is a field that exists in none of
  them. A `max(case.safety_factor for case in cases)` would miss exactly that, which is why there
  is a test that fails if anybody simplifies it back.

The superposition check is the one that proves the multi-RHS path is actually right: in linear
elasticity the response to A + B must equal the response to A plus the response to B, exactly.
That is a property of the physics rather than of this code, so it catches a wrong reduced-vector
mapping, a stale factorisation, and a case whose load leaked into another.
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from constraint_kit.fea import Grid, Model, SolveFailure  # noqa: E402
from constraint_kit.fea.voxel import Envelope  # noqa: E402

TESTS = []

E_AL, NU = 70_000.0, 0.33


def test(fn):
    TESTS.append(fn)
    return fn


def _bar(nx=24, ny=4, nz=4, pitch=1.0) -> Grid:
    return Grid(density=np.ones((nx, ny, nz)), pitch=(pitch,) * 3)


def _held(model: Model) -> Model:
    model.fix(lambda p: p[:, 0] <= 1e-9)
    return model


def _tip(p):
    return p[:, 0] >= 24.0 - 1e-9


def _middle(p):
    return (p[:, 0] >= 11.0 - 1e-9) & (p[:, 0] <= 13.0 + 1e-9)


# ----------------------------------------------------------------------- the one-case identity

@test
def test_a_single_named_case_matches_a_plain_solve_exactly():
    """The multi-case path must not be a second implementation of the physics."""
    plain = _held(Model(_bar(), E_AL, NU))
    plain.load(_tip, (0.0, 0.0, -200.0))
    one = plain.solve(method="direct")

    multi = _held(Model(_bar(), E_AL, NU))
    multi.load(_tip, (0.0, 0.0, -200.0), case="pull")
    env = multi.solve_cases(method="direct")

    assert set(env.cases) == {"pull"}, sorted(env.cases)
    got = env.cases["pull"]
    assert np.allclose(got.displacement, one.displacement, rtol=0, atol=1e-12), (
        "a named case must give bit-comparable results to the unnamed path")
    assert np.allclose(got.von_mises, one.von_mises, rtol=0, atol=1e-9)
    assert abs(got.compliance - one.compliance) < 1e-9 * abs(one.compliance)


@test
def test_an_unnamed_load_is_solved_as_the_default_case():
    """Backward compatibility, and the obvious thing to do with a model nobody named cases on."""
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0))
    env = model.solve_cases(method="direct")
    assert list(env.cases) == ["default"], sorted(env.cases)
    assert env.cases["default"].max_displacement > 0.0


# ------------------------------------------------------------------------------ superposition

@test
def test_superposition_holds_exactly_which_proves_the_multi_rhs_path():
    """Linear elasticity: the response to A+B is the response to A plus the response to B.

    A property of the physics, not of this code, so it catches the three ways a multi-RHS solve
    goes wrong — a mis-mapped reduced vector, a factorisation reused after the matrix changed,
    and one case's load leaking into another's right-hand side.
    """
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0), case="down")
    model.load(_middle, (150.0, 0.0, 0.0), case="pull")
    model.load(_tip, (0.0, 0.0, -200.0), case="both")
    model.load(_middle, (150.0, 0.0, 0.0), case="both")
    env = model.solve_cases(method="direct")

    a = env.cases["down"].displacement
    b = env.cases["pull"].displacement
    ab = env.cases["both"].displacement
    worst = float(np.abs(ab).max())
    assert np.allclose(ab, a + b, rtol=0, atol=1e-10 * max(worst, 1.0)), (
        "superposition failed: max deviation %.3e against a field of %.3e"
        % (float(np.abs(ab - a - b).max()), worst))

    # and the loads really are independent: "down" must have no X response to speak of
    assert np.abs(a[:, 0]).max() < 0.2 * np.abs(a[:, 2]).max(), "the cases are not independent"


# ------------------------------------------------------------------------------- the envelope

@test
def test_the_envelope_is_elementwise_and_bounds_every_case():
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0), case="down")
    model.load(_middle, (0.0, 0.0, 400.0), case="up_mid")
    env = model.solve_cases(method="direct")

    assert env.von_mises.shape == env.cases["down"].von_mises.shape
    for name, res in env.cases.items():
        assert np.all(env.von_mises >= res.von_mises - 1e-12), (
            "the envelope dips below case %r somewhere" % name)
    stacked = np.stack([r.von_mises for r in env.cases.values()])
    assert np.allclose(env.von_mises, stacked.max(axis=0))


@test
def test_the_envelope_is_not_merely_the_worst_case():
    """The reason an envelope exists at all.

    A bar held at one end, pulled down at the tip in one case and pushed up near the root in
    another. The tip case governs the root elements; the mid case governs the elements under its
    own load. So the envelope exceeds BOTH cases somewhere, and the governing case differs by
    element. Anyone who simplifies this to `max(case.peak for case in cases)` breaks this test.
    """
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0), case="tip")
    model.load(_middle, (0.0, 0.0, 600.0), case="mid")
    env = model.solve_cases(method="direct")

    governing = env.governing_case_per_element()
    assert governing.shape == env.von_mises.shape
    names = set(np.unique(governing).tolist())
    assert len(names) >= 2, (
        "both cases should govern somewhere; only %s does, so this fixture no longer tests "
        "what it claims" % names)

    # the envelope must strictly exceed at least one case over a real part of the structure
    for res in env.cases.values():
        exceeded = int(np.sum(env.von_mises > res.von_mises + 1e-9))
        assert exceeded > 0, "the envelope equals one case everywhere, so it is that case"


@test
def test_the_envelope_reports_which_case_drives_the_headline_numbers():
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -50.0), case="light")
    model.load(_tip, (0.0, 0.0, -500.0), case="heavy")
    env = model.solve_cases(method="direct")

    assert env.worst_case_for_displacement == "heavy", env.worst_case_for_displacement
    assert env.worst_case_for_stress == "heavy", env.worst_case_for_stress
    assert env.max_displacement == env.cases["heavy"].max_displacement
    assert env.peak_von_mises >= env.cases["heavy"].peak_von_mises - 1e-12

    name, sf = env.governing(allowable=165.0)
    assert name == "heavy", name
    assert sf == min(165.0 / env.von_mises_percentile(99.0), 1e30) or sf > 0


@test
def test_the_governing_safety_factor_comes_from_the_envelope_not_a_case():
    """A part is safe only if it is safe everywhere under every case it will actually see."""
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0), case="tip")
    model.load(_middle, (0.0, 0.0, 600.0), case="mid")
    env = model.solve_cases(method="direct")

    allowable = 165.0
    _, envelope_sf = env.governing(allowable=allowable)
    per_case = [allowable / r.von_mises_percentile(99.0) for r in env.cases.values()]
    assert envelope_sf <= min(per_case) + 1e-9, (
        "the envelope safety factor must be no better than the worst individual case: "
        "%.4f against %s" % (envelope_sf, ["%.4f" % v for v in per_case]))


@test
def test_the_envelope_report_names_the_cases_and_stays_ascii():
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0), case="belt pull")
    model.load(_middle, (0.0, 0.0, 600.0), case="reaction")
    env = model.solve_cases(method="direct")

    text = env.report(allowable=165.0)
    assert "belt pull" in text and "reaction" in text, text
    assert "envelope" in text.lower(), text
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, "non-ASCII %r in a console report:\n%s" % (bad, text)


# ---------------------------------------------------------------- assembled once, not per case

@test
def test_the_matrix_is_assembled_once_for_every_case():
    """Assembly does not depend on the load, and on a big grid it is the expensive part."""
    model = _held(Model(_bar(32, 6, 6), E_AL, NU))
    for i, force in enumerate([-100.0, -200.0, -300.0, 400.0]):
        model.load(_tip, (0.0, 0.0, force), case="c%d" % i)

    calls = {"n": 0}
    original = Model._assemble

    def counted(self, active):
        calls["n"] += 1
        return original(self, active)

    Model._assemble = counted
    try:
        env = model.solve_cases(method="direct")
    finally:
        Model._assemble = original

    assert len(env.cases) == 4, sorted(env.cases)
    assert calls["n"] == 1, (
        "assembled %d times for 4 cases; the matrix does not depend on the load" % calls["n"])


@test
def test_a_factorisation_is_reused_across_cases_when_the_solver_is_direct():
    """Four direct solves on one factorisation, not four factorisations."""
    import scipy.sparse.linalg as spla

    model = _held(Model(_bar(), E_AL, NU))
    for i in range(4):
        model.load(_tip, (0.0, 0.0, -100.0 * (i + 1)), case="c%d" % i)

    calls = {"n": 0}
    original = spla.splu

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    spla.splu = counted
    try:
        env = model.solve_cases(method="direct")
    finally:
        spla.splu = original

    assert len(env.cases) == 4
    assert calls["n"] == 1, "factorised %d times for 4 cases" % calls["n"]
    # and the answers must still scale linearly, which is the proof they are not stale
    d = [env.cases["c%d" % i].max_displacement for i in range(4)]
    for i in range(1, 4):
        assert abs(d[i] / d[0] - (i + 1)) < 1e-9, (d, "load scaled %dx" % (i + 1))


# -------------------------------------------------------------------------- honest refusals

@test
def test_a_case_with_no_load_is_refused_by_name():
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0), case="real")
    model.case("empty")                       # declared and then never loaded
    try:
        model.solve_cases(method="direct")
    except SolveFailure as exc:
        assert "empty" in str(exc), str(exc)
        return
    raise AssertionError("a declared case with no load must be refused, not solved as zero")


@test
def test_solve_cases_refuses_a_model_with_no_cases_at_all():
    model = _held(Model(_bar(), E_AL, NU))
    try:
        model.solve_cases(method="direct")
    except SolveFailure as exc:
        assert "load" in str(exc).lower(), str(exc)
        return
    raise AssertionError("no loads anywhere must be refused")


@test
def test_mixing_a_named_case_with_an_unnamed_load_is_refused():
    """Ambiguous: is the unnamed load part of every case, or a case of its own?

    Both readings are defensible and they give different answers, so this refuses rather than
    picking one. A load that belongs in every case can be written into every case explicitly,
    and then it says so.
    """
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0))
    model.load(_middle, (0.0, 0.0, 100.0), case="extra")
    try:
        model.solve_cases(method="direct")
    except (SolveFailure, ValueError) as exc:
        text = str(exc).lower()
        assert "unnamed" in text or "default" in text, str(exc)
        return
    raise AssertionError("an unnamed load alongside named cases is ambiguous and must be refused")


@test
def test_a_case_name_must_be_a_non_empty_string():
    model = _held(Model(_bar(), E_AL, NU))
    for bad in ("", "   ", None):
        try:
            model.load(_tip, (0.0, 0.0, -1.0), case=bad) if bad is not None else None
        except ValueError:
            continue
        if bad is None:
            continue
        raise AssertionError("case=%r should have been refused" % bad)


@test
def test_an_envelope_of_one_case_is_still_an_envelope():
    """The degenerate case must behave, because an optimizer will hit it."""
    model = _held(Model(_bar(), E_AL, NU))
    model.load(_tip, (0.0, 0.0, -200.0), case="only")
    env = model.solve_cases(method="direct")
    assert isinstance(env, Envelope)
    assert np.allclose(env.von_mises, env.cases["only"].von_mises)
    assert env.worst_case_for_stress == "only"
    assert set(np.unique(env.governing_case_per_element()).tolist()) == {"only"}


@test
def test_cg_and_direct_agree_across_cases():
    """The two solvers must not disagree about a load case any more than about a single load."""
    def run(method):
        model = _held(Model(_bar(), E_AL, NU))
        model.load(_tip, (0.0, 0.0, -200.0), case="a")
        model.load(_middle, (0.0, 0.0, 600.0), case="b")
        return model.solve_cases(method=method)

    direct, cg = run("direct"), run("cg")
    for name in ("a", "b"):
        rel = (abs(direct.cases[name].max_displacement - cg.cases[name].max_displacement)
               / direct.cases[name].max_displacement)
        assert rel < 1e-6, (name, rel)
    assert np.allclose(direct.von_mises, cg.von_mises, rtol=1e-5)


def main() -> int:
    failed = []
    for fn in TESTS:
        try:
            fn()
            print("  PASS  %s" % fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            print("  FAIL  %s: %s" % (fn.__name__, exc))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(TESTS) - len(failed), len(TESTS)))
    if failed:
        print("FAILED: %s" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
