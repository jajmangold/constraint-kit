#!/usr/bin/env python3
"""Mesh-convergence estimation — written BEFORE constraint_kit/fea/convergence.py exists.

    python tests/test_convergence.py

Why this module has to exist: Phase 0 reports a ladder of pitches and a human reads the trend.
An optimizer cannot read a trend. Before a parameter sweep is allowed to compare two designs it
has to know whether the difference it sees is structural or is just mesh error, and that needs a
number: the Richardson-extrapolated limit, the observed order of convergence, and an error bar.

Every case below is a sequence whose exact answer is known by construction (f = limit + C*h^p),
or a pathology that must be reported rather than smoothed over. No case asserts against this
module's own output.
"""
from __future__ import annotations

import math
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constraint_kit.fea.convergence import ConvergenceError, richardson  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def ladder(limit, coeff, order, pitches):
    """Exact values of f(h) = limit + coeff * h**order."""
    return [limit + coeff * h ** order for h in pitches]


# ------------------------------------------------------------------ recovering a known answer

@test
def test_recovers_limit_and_order_from_a_second_order_sequence():
    """The textbook case: h = 4, 2, 1 with error ~ h^2 must give back p = 2 and the exact limit."""
    pitches = [4.0, 2.0, 1.0]
    vals = ladder(10.0, -2.0, 2.0, pitches)          # -22, 2, 8
    c = richardson(pitches, vals)
    assert abs(c.order - 2.0) < 1e-9, c.order
    assert abs(c.extrapolated - 10.0) < 1e-9, c.extrapolated
    assert c.monotone is True


@test
def test_recovers_a_first_order_sequence():
    pitches = [3.0, 1.5, 0.75]
    vals = ladder(5.0, 1.3, 1.0, pitches)
    c = richardson(pitches, vals)
    assert abs(c.order - 1.0) < 1e-9, c.order
    assert abs(c.extrapolated - 5.0) < 1e-9, c.extrapolated


@test
def test_recovers_a_fractional_order_sequence():
    """A stair-stepped voxel boundary does not give a clean integer order; it must still work."""
    pitches = [2.0, 1.0, 0.5]
    vals = ladder(-3.25, 0.8, 1.37, pitches)
    c = richardson(pitches, vals)
    assert abs(c.order - 1.37) < 1e-8, c.order
    assert abs(c.extrapolated + 3.25) < 1e-8, c.extrapolated


@test
def test_error_estimate_brackets_the_true_remaining_error():
    """rel_error must be a real bound on how far the finest value still is from the limit."""
    pitches = [4.0, 2.0, 1.0]
    vals = ladder(10.0, -2.0, 2.0, pitches)
    c = richardson(pitches, vals)
    true_rel = abs(vals[-1] - 10.0) / 10.0            # |8 - 10| / 10 = 0.20
    assert abs(c.rel_error - true_rel) < 1e-9, (c.rel_error, true_rel)
    assert c.converged is False, "20% remaining error must not be called converged"


@test
def test_declares_convergence_when_the_error_is_inside_tolerance():
    pitches = [4.0, 2.0, 1.0]
    vals = ladder(10.0, -0.02, 2.0, pitches)          # finest is 0.2% low
    c = richardson(pitches, vals, tol=0.05)
    assert c.converged is True, (c.rel_error, c.note)
    assert c.rel_error < 0.05


# ------------------------------------------------------------------------------- pathologies

@test
def test_non_monotone_sequence_is_reported_not_extrapolated():
    """The real gantry-plate ladder did this at coarse pitch: 1.31, 1.10, 0.84, 0.93.

    A sequence that turns around is not converging, and Richardson on it produces a confident
    number from noise. It must refuse.

    The pitches here are geometric (2.0 / 1.6 / 1.28, ratio 1.25 twice) on purpose. The first
    draft of this test used the measured pitches 2.4 / 2.0 / 1.6, whose ratios are 1.2 and 1.25,
    and it failed against correct code: the constant-ratio precondition fires first. That
    ordering is deliberate -- a malformed ladder is the caller's bug and raises, while a
    non-monotone one is a finding about the mesh and returns.
    """
    c = richardson([2.0, 1.6, 1.28], [1.308, 1.102, 1.180])
    assert c.monotone is False
    assert c.converged is False
    assert c.extrapolated is None, "must not extrapolate a non-monotone sequence"
    assert "monoton" in c.note.lower(), c.note


@test
def test_oscillating_sign_difference_does_not_return_nan():
    """(f1-f2)/(f2-f3) < 0 makes the order a log of a negative number."""
    c = richardson([4.0, 2.0, 1.0], [1.0, 3.0, 2.0])
    assert c.extrapolated is None
    assert c.order is None
    assert not any(x is not None and math.isnan(x)
                   for x in (c.order, c.extrapolated, c.rel_error))


@test
def test_identical_values_are_converged_with_no_order():
    """A sequence that has stopped moving is converged; the order is undefined, not zero."""
    c = richardson([4.0, 2.0, 1.0], [7.5, 7.5, 7.5])
    assert c.converged is True, c.note
    assert c.rel_error == 0.0
    assert c.order is None, "0/0 must not be reported as an order"


@test
def test_rejects_a_non_geometric_pitch_ladder():
    """Richardson assumes a constant refinement ratio. 1.82/1.30/0.91 is NOT constant.

    That is exactly the ladder `drivers/fea_part.py` produced when it used 0.7 instead of 1/1.4,
    and silently accepting it would put a 2% error in the ratio straight into the exponent.
    """
    try:
        richardson([1.82, 1.30, 0.91], [0.696, 1.005, 1.173])
    except ConvergenceError as exc:
        assert "ratio" in str(exc).lower(), str(exc)
        return
    raise AssertionError("a non-constant refinement ratio must be refused")


@test
def test_accepts_the_corrected_geometric_ladder():
    r = 1.4
    pitches = [1.3 * r, 1.3, 1.3 / r]
    c = richardson(pitches, ladder(1.35, -0.5, 2.0, pitches))
    assert abs(c.extrapolated - 1.35) < 1e-9, c.extrapolated


@test
def test_requires_at_least_three_points():
    for bad in ([], [1.0], [2.0, 1.0]):
        try:
            richardson(bad, [1.0] * len(bad))
        except ConvergenceError:
            continue
        raise AssertionError(f"{len(bad)} points must be refused, cannot estimate an order")


@test
def test_rejects_mismatched_input_lengths():
    try:
        richardson([4.0, 2.0, 1.0], [1.0, 2.0])
    except ConvergenceError:
        return
    raise AssertionError("mismatched pitches/values must be refused")


@test
def test_rejects_unsorted_pitches():
    """Coarse-to-fine is the contract; silently sorting would hide a caller's bug."""
    try:
        richardson([1.0, 2.0, 4.0], [8.0, 2.0, -22.0])
    except ConvergenceError as exc:
        assert "descend" in str(exc).lower() or "order" in str(exc).lower(), str(exc)
        return
    raise AssertionError("ascending pitches must be refused")


@test
def test_rejects_nonpositive_pitch():
    try:
        richardson([4.0, 2.0, 0.0], [1.0, 2.0, 3.0])
    except ConvergenceError:
        return
    raise AssertionError("a zero pitch must be refused")


# ------------------------------------------------------------------------- more than 3 points

@test
def test_uses_the_three_finest_of_a_longer_ladder():
    """Extra coarse points are history; the estimate must come from the finest three."""
    pitches = [8.0, 4.0, 2.0, 1.0]
    vals = ladder(10.0, -2.0, 2.0, pitches)
    c4 = richardson(pitches, vals)
    c3 = richardson(pitches[1:], vals[1:])
    assert abs(c4.extrapolated - c3.extrapolated) < 1e-9
    assert abs(c4.extrapolated - 10.0) < 1e-9


@test
def test_reports_the_inputs_it_used():
    """The result has to be self-describing, or a logged number cannot be audited later."""
    pitches = [4.0, 2.0, 1.0]
    vals = ladder(10.0, -2.0, 2.0, pitches)
    c = richardson(pitches, vals)
    assert c.pitches == (4.0, 2.0, 1.0), c.pitches
    assert c.values == tuple(vals), c.values
    assert "10" in format(c)


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
