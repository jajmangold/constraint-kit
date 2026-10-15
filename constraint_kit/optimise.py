# -*- coding: utf-8 -*-
"""Close the loop: parameters -> performance -> better parameters (issues #16, #17).

This is the piece the whole project was built toward, and it is deliberately the smallest piece.
The hard parts were done elsewhere: a parametric source of truth means a design *has* parameters
to vary, and a verified physics tier means varying them produces a number you can believe. Given
those two, the optimizer is a search — and the honest search for this problem is a small one.

**Why a compass (pattern) search**, and not something more impressive:

* **Each evaluation is expensive.** Seconds, because it is a mesh and a linear solve. So the
  method has to be frugal with evaluations, and a gradient is not available at any price.
* **The space is small.** A handful of real dimensions. A population method would spend its
  budget on diversity nobody needs.
* **Bounds and quantisation are hard constraints, not penalties.** A wall cannot be 1.7 mm when
  the nozzle lays 0.4 mm lines, and an optimizer that returns 1.73 mm has not answered the
  question. Compass search works natively on a bounded, quantised lattice.
* **It must be able to start infeasible and walk in**, because the honest starting point for
  "make this lighter" is usually a part that already fails something.

**The feasibility filter** is how constraints are handled, rather than a penalty weight nobody
can choose: a feasible point always beats an infeasible one; among infeasible points, less total
violation wins; among feasible points, lower objective wins. That ordering walks into the
feasible region and then stays there, with no weight to tune and no way for a penalty to trade
away a safety factor for mass.

It has one failure mode, which is worth knowing rather than discovering: **a BINARY constraint
carries no gradient.** `linear_valid >= 1` is 0 or 1, so every infeasible point has violation
exactly 1.0, the filter cannot order them, and the objective decides among them -- sending the
walk to the lightest *infeasible* design. That is why `minimise` scans each parameter bound
before it walks: those 2n points are what a person would try first, and they are enough to find
a feasible region a flat violation cannot point at.

**What this will not do is flatter itself.** A pattern search finds a *local* optimum. On a
multi-modal objective it can be fooled, and `report()` says so every time rather than only when
it happens to be wrong. An optimizer that reports "optimal" is lying; one that reports "no
improving move from here, at this step size" is telling the truth, and that is the wording.

The caching (#16) is not a micro-optimisation of the optimizer; it is most of its speed. A
compass search revisits points constantly — it steps out, fails, halves the step, and steps back
over ground it has already covered — so without a cache a two-parameter search re-solves the
same mesh dozens of times. The key is the *quantised* parameter vector, so two genuinely
different designs can never share an answer.

Nothing here imports CAD or FEA. `evaluate` is any callable from a parameter dict to a metrics
dict, which is what makes the whole thing testable against closed-form optima.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

OPS = {">=": lambda v, limit: v >= limit, "<=": lambda v, limit: v <= limit}

# Initial step as a fraction of each parameter's range. A sixth is a compromise found by use:
# large enough to cross a feasibility boundary from a bad start in a few moves, small enough not
# to leap over a narrow feasible band on the first try.
START_STEP_FRAC = 1.0 / 6.0

# Diagonal probe directions are added up to this many parameters. 2^n, so 4 gives 16 diagonals
# on top of 8 axes, which is a tolerable round at seconds per evaluation; 8 would give 256.
MAX_DIAGONAL_DIMS = 4

# Slack below which a constraint counts as EXACTLY tight -- the optimum lies on its surface.
TIGHT = 1e-3

# Rounding used for the cache key. Finer than any tolerance worth optimising to, coarse enough
# that two floats differing in the last bit share an answer rather than paying for two solves.
KEY_DECIMALS = 9


class InfeasibleBox(ValueError):
    """No point in the search box satisfies the constraints.

    Raised rather than returning the least-bad point as though it were an answer. "No" is a
    useful output; "0.98 of what you asked for" presented as success is not. `best` carries the
    nearest miss so the caller can see which requirement to change, and by how much.
    """

    def __init__(self, message: str, best=None):
        super().__init__(message)
        self.best = best


@dataclass(frozen=True)
class Parameter:
    """One free dimension: a name, a closed interval, and optionally a lattice to live on."""

    name: str
    low: float
    high: float
    step: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(f"a parameter needs a name, got {self.name!r}")
        if not (self.high > self.low):
            raise ValueError(
                f"parameter {self.name!r} has low={self.low} and high={self.high}; low must be "
                f"strictly below high")
        if self.step is not None and self.step <= 0.0:
            raise ValueError(f"parameter {self.name!r} has step={self.step}; it must be positive")
        if self.step is not None and self.step > (self.high - self.low):
            raise ValueError(
                f"parameter {self.name!r} has step={self.step}, wider than its own range "
                f"{self.high - self.low}; the lattice would hold a single point")

    @property
    def span(self) -> float:
        return self.high - self.low

    def clamp(self, value: float) -> float:
        """Into the interval, and onto the lattice if there is one."""
        v = min(max(float(value), self.low), self.high)
        if self.step is None:
            return v
        # Measured from `low`, so the lattice includes the lower bound -- which is what a wall
        # thickness or a plate gauge actually means.
        n = round((v - self.low) / self.step)
        snapped = self.low + n * self.step
        if snapped > self.high + 1e-12:
            snapped -= self.step
        return min(max(snapped, self.low), self.high)


@dataclass(frozen=True)
class Constraint:
    """A requirement on one reported metric. `name` is what gets blamed when it binds."""

    name: str
    key: str
    op: str
    limit: float

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise ValueError(
                f"constraint {self.name!r} has operator {self.op!r}; only "
                f"{' and '.join(sorted(OPS))} are supported, because an equality constraint "
                f"needs a tolerance and that tolerance is the caller decision to make")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("a constraint needs a name so a report can say which one binds")

    def satisfied(self, value: float) -> bool:
        return bool(OPS[self.op](value, self.limit))

    def violation(self, value: float) -> float:
        """How far outside, normalised by the limit so several constraints can be summed.

        Normalised because the alternative is summing millimetres and megapascals, where whichever
        unit happens to be numerically larger dominates the walk into the feasible region.
        """
        if self.satisfied(value):
            return 0.0
        scale = abs(self.limit) if self.limit else 1.0
        return abs(value - self.limit) / scale

    def slack(self, value: float) -> float:
        """Normalised distance inside the feasible side. Zero means exactly tight."""
        scale = abs(self.limit) if self.limit else 1.0
        return (value - self.limit) / scale * (1.0 if self.op == ">=" else -1.0)


@dataclass(frozen=True)
class Outcome:
    """One evaluated design: where it was, what it scored, and whether it was allowed."""

    x: dict
    objective: float
    metrics: dict
    feasible: bool
    violation: float
    violations: list = field(default_factory=list)
    failed: bool = False

    def rank(self) -> tuple:
        """The feasibility filter, as a sort key. Lower is better.

        A feasible point always beats an infeasible one; among infeasible points less total
        violation wins, which is what walks the search into the feasible region; among feasible
        points the objective decides. No penalty weight, so nothing can trade a safety factor
        away for mass.
        """
        return (0 if self.feasible else 1,
                0.0 if self.feasible else self.violation,
                self.objective)


@dataclass(frozen=True)
class Optimum:
    """The result of a search, with everything needed to judge how much to believe it."""

    best: Outcome
    history: list
    evaluations: int
    cache_hits: int
    failures: int
    converged: bool
    final_step: dict
    method: str
    objective: str
    constraints: list
    parameters: list

    @property
    def lookups(self) -> int:
        return self.evaluations + self.cache_hits

    def constraints_within(self, window: float) -> list:
        """Constraints whose slack is within `window` (relative), in declaration order."""
        out = []
        for c in self.constraints:
            value = self.best.metrics.get(c.key)
            if value is None:
                continue
            if abs(c.slack(float(value))) <= window:
                out.append(c.name)
        return out

    @property
    def active_constraints(self) -> list:
        """Constraints EXACTLY tight at the optimum, to 0.1%.

        The mathematical notion: the optimum sits on this constraint surface. True on a smooth
        problem; frequently empty on a real one, and that is not a defect. If the evaluation
        quantises the design -- which a voxel mesh does, rounding a thickness to whole cells --
        then the objective is a staircase and the best feasible point sits on a tread, strictly
        inside the constraint. See `limiting_constraints` for the question an engineer is
        actually asking.
        """
        return self.constraints_within(TIGHT)

    @property
    def blocking_constraints(self) -> list:
        """What actually STOPPED the search, as evidence rather than as a slack threshold.

        Usually the real output: the same beam against a strength limit and against a stiffness
        limit optimises to different sizes, and knowing WHICH one stopped you is what says what
        to change -- a stiffness bound says add depth or a rib, a strength bound says more
        material or a different material.

        Read off the history, not off a window. Every point the search evaluated with a BETTER
        objective than the winner must have been infeasible, or it would have won; so the
        constraints those points violate are exactly the ones standing between here and a better
        design.

        **Ordered by how often each did it, so the FIRST entry is the dominant blocker** -- that
        is the one to act on. More than one name is normal and not a hedge: the bound scan tries
        the extremes of the box, where several requirements usually fail at once, and a
        constraint that refused one far-off design is genuinely a blocker even if it is not the
        one that matters.

        This replaced a slack-threshold version, and the reason is worth keeping: on a quantised
        evaluation no threshold works. A voxel mesh rounds a thickness to whole cells, so the
        objective is a staircase and the winner sits on a tread -- measured 21% inside a safety
        factor it was unambiguously limited by. A 10% window called that unconstrained, a 30%
        window would call almost anything constrained, and neither is a fact about the design.
        What the search tried and was refused IS a fact about the design.
        """
        blame: dict = {}
        for outcome in self.history:
            if outcome.objective >= self.best.objective or outcome.feasible:
                continue
            for text in outcome.violations:
                name = text.split(":")[0].strip()
                blame[name] = blame.get(name, 0) + 1
        return [name for name, _n in sorted(blame.items(), key=lambda kv: (-kv[1], kv[0]))]

    @property
    def at_bounds(self) -> list:
        """Parameters resting on a bound. One of these is usually the real constraint."""
        out = []
        for p in self.parameters:
            v = float(self.best.x[p.name])
            if abs(v - p.low) <= 1e-9 or abs(v - p.high) <= 1e-9:
                out.append(p.name)
        return out

    def report(self) -> str:
        """The whole search as ASCII text, including what not to conclude from it."""
        lines = ["%s: minimised %s over %d parameter(s) in %d evaluation(s)"
                 % (self.method, self.objective, len(self.parameters), self.evaluations)]
        if self.cache_hits:
            lines.append("  %d of %d lookups were already cached"
                         % (self.cache_hits, self.lookups))
        if self.failures:
            lines.append("  %d evaluation(s) FAILED outright and were treated as infeasible; "
                         "that region may be unbuildable rather than merely bad" % self.failures)
        lines.append("  parameters")
        for p in self.parameters:
            v = float(self.best.x[p.name])
            flag = ""
            if abs(v - p.low) <= 1e-9:
                flag = "  <- at its LOWER bound"
            elif abs(v - p.high) <= 1e-9:
                flag = "  <- at its UPPER bound"
            lines.append("    %-20s %12.5g   in [%g, %g]%s"
                         % (p.name, v, p.low, p.high, flag))
        lines.append("  %-22s %12.6g" % (self.objective, self.best.objective))
        if self.constraints:
            lines.append("  constraints")
            for c in self.constraints:
                value = self.best.metrics.get(c.key)
                if value is None:
                    continue
                tight = abs(c.slack(float(value))) <= 1e-3
                lines.append("    %-20s %12.5g %s %-10g %s"
                             % (c.name, float(value), c.op, c.limit,
                                "<- BINDING" if tight
                                else ("VIOLATED" if not c.satisfied(float(value)) else "")))
        blocking = self.blocking_constraints
        if blocking:
            exact = " exactly" if self.active_constraints else ""
            lines.append("  %s blocked every better design that was tried%s, so that is what to "
                         "change to go further" % (" and ".join(blocking), exact))
            if not self.active_constraints:
                lines.append("  The optimum is not EXACTLY on it, which on a quantised "
                             "evaluation is expected: if the geometry rounds to a mesh, the "
                             "objective is a staircase and the best point sits on a tread "
                             "rather than on the constraint surface.")
        elif self.at_bounds:
            lines.append("  no constraint blocked a better design; %s is resting on a bound, so "
                         "the bound is the real limit here" % " and ".join(self.at_bounds))
        else:
            lines.append("  no constraint blocked a better design and no parameter is at a "
                         "bound, so this is an interior minimum of the objective itself -- "
                         "check that the objective is the one you meant.")
        if not self.converged:
            lines.append("  NOT CONVERGED: the evaluation budget ran out before the step size "
                         "reached the tolerance. Treat this as the best point seen, not as an "
                         "optimum.")
        if len(self.parameters) > MAX_DIAGONAL_DIMS:
            lines.append("  With %d parameters only the %d axis directions were probed, not the "
                         "diagonals. At a curved constraint boundary that can stall short of an "
                         "optimum, so treat this as a starting point and re-run from it."
                         % (len(self.parameters), 2 * len(self.parameters)))
        lines.append("  This is a LOCAL optimum: no improving move exists from here at a step "
                     "of %s. A pattern search cannot see past a ridge, so a different starting "
                     "point may find a better design."
                     % ", ".join("%s=%.3g" % (k, v) for k, v in self.final_step.items()))
        return "\n".join(lines)


def _validate(parameters, objective: str, constraints) -> None:
    if not parameters:
        raise ValueError("at least one parameter is needed; there is nothing to search over")
    names = [p.name for p in parameters]
    if len(set(names)) != len(names):
        raise ValueError("duplicate parameter name(s): %s"
                         % ", ".join(sorted({n for n in names if names.count(n) > 1})))
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError(f"objective must be the name of a reported metric, got {objective!r}")
    seen = [c.name for c in constraints]
    if len(set(seen)) != len(seen):
        raise ValueError("duplicate constraint name(s): %s"
                         % ", ".join(sorted({n for n in seen if seen.count(n) > 1})))


class _Evaluator:
    """Calls `evaluate`, caches by quantised vector, and turns metrics into an Outcome.

    An evaluation that RAISES becomes an infeasible outcome rather than an exception, because a
    geometry that will not build is a real result in a sizing sweep. It is counted, though, and
    the count appears in the report: a silently skipped failure would let a search wander through
    a region nobody knows is unbuildable.
    """

    def __init__(self, evaluate, parameters, objective: str, constraints):
        self._evaluate = evaluate
        self._parameters = list(parameters)
        self._objective = objective
        self._constraints = list(constraints)
        self._cache: dict = {}
        self.evaluations = 0
        self.cache_hits = 0
        self.failures = 0
        self.history: list = []
        self._checked_keys = False

    def _key(self, x: dict) -> tuple:
        return tuple(round(float(x[p.name]), KEY_DECIMALS) for p in self._parameters)

    def __call__(self, x: dict) -> Outcome:
        key = self._key(x)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]

        self.evaluations += 1
        try:
            metrics = self._evaluate(dict(x))
        except Exception as exc:                                     # noqa: BLE001
            self.failures += 1
            out = Outcome(x=dict(x), objective=math.inf, metrics={"error": str(exc)},
                          feasible=False, violation=math.inf,
                          violations=["evaluation failed: %s" % exc], failed=True)
            self._cache[key] = out
            self.history.append(out)
            return out

        if not isinstance(metrics, dict):
            raise TypeError(
                "evaluate must return a dict of metrics, got %s" % type(metrics).__name__)
        if not self._checked_keys:
            # Checked once, against the first successful evaluation: optimising against a
            # constraint the evaluation never reports would silently solve a different problem.
            missing = [c.key for c in self._constraints if c.key not in metrics]
            if missing:
                raise KeyError(
                    "the evaluation does not report %s, which %s constrain. It reports: %s"
                    % (", ".join(sorted(set(missing))),
                       "a constraint" if len(missing) == 1 else "constraints",
                       ", ".join(sorted(metrics))))
            if self._objective not in metrics:
                raise KeyError(
                    "the evaluation does not report the objective %r. It reports: %s"
                    % (self._objective, ", ".join(sorted(metrics))))
            self._checked_keys = True

        violation = 0.0
        broken = []
        for c in self._constraints:
            value = float(metrics[c.key])
            if not c.satisfied(value):
                violation += c.violation(value)
                broken.append("%s: %s %.6g, needs %s %g"
                              % (c.name, c.key, value, c.op, c.limit))
        out = Outcome(x=dict(x), objective=float(metrics[self._objective]), metrics=dict(metrics),
                      feasible=not broken, violation=violation, violations=broken)
        self._cache[key] = out
        self.history.append(out)
        return out


def _directions(parameters) -> list:
    """The probe directions: the 2n axes, plus the 2^n diagonals when n is small.

    The axes alone are a positive spanning set, which is sufficient for an *unconstrained* smooth
    problem and **not** sufficient here. At a curved constraint boundary the feasible descent
    cone can exclude every axis direction, and then a pure coordinate search stalls at a point
    that is not an optimum. Measured on a textbook section-sizing problem: minimise `b*h` subject
    to `b*h^2/6 >= S` stalls at area 180 against a true 80, because every single-axis move either
    raises the area or breaks the constraint, while the diagonal that reduces `b` and raises `h`
    together does neither.

    Diagonals are added only up to `MAX_DIAGONAL_DIMS` parameters, because 2^n grows the way it
    does. Past that the set is the axes alone and `report()` says so, rather than quietly
    becoming a weaker method at six parameters than at four.
    """
    names = [p.name for p in parameters]
    out = []
    for name in names:
        for sign in (-1.0, 1.0):
            out.append({n: (sign if n == name else 0.0) for n in names})
    if 1 < len(names) <= MAX_DIAGONAL_DIMS:
        for combo in itertools.product((-1.0, 1.0), repeat=len(names)):
            out.append(dict(zip(names, combo)))
    return out


def _start_point(parameters, start) -> dict:
    """The midpoint by default, snapped to the lattice.

    The midpoint rather than a bound: starting at a bound gives the search only one direction to
    move in each dimension, which wastes the first round of probes.
    """
    x = {}
    for p in parameters:
        raw = p.clamp(start[p.name]) if start and p.name in start else p.clamp(
            p.low + 0.5 * p.span)
        x[p.name] = raw
    return x


def minimise(evaluate, parameters, *, objective: str, constraints=(),
             start: dict | None = None, tol: float = 1e-4,
             max_evaluations: int = 400) -> Optimum:
    """Compass search with a feasibility filter. Returns a LOCAL optimum, and says so.

    `evaluate` maps a parameter dict to a metrics dict. `objective` and every constraint `key`
    must appear in it, which is checked on the first evaluation rather than assumed.
    """
    parameters = list(parameters)
    constraints = list(constraints)
    _validate(parameters, objective, constraints)

    ev = _Evaluator(evaluate, parameters, objective, constraints)
    x = _start_point(parameters, start)
    best = ev(x)

    # A BOUND SCAN before the walk: each parameter at its low and at its high, one at a time.
    #
    # This exists because of a real failure, and the failure is worth stating. The feasibility
    # filter walks into the feasible region by following DECREASING violation -- which works
    # while the violation carries information and does not when it is binary. A constraint like
    # `linear_valid >= 1` is 0 or 1: every infeasible point has violation exactly 1.0, the filter
    # cannot tell them apart, and so the objective decides among them. The search then slides to
    # the lightest INFEASIBLE design and reports that nothing in the box works.
    #
    # Measured: a 160 mm PETG plate, thickness free over [4, 24] mm and constrained to stay
    # inside linear validity. Feasible from 20 mm up, infeasible below. Started at the midpoint
    # 14 mm (infeasible), the walk went 14 -> 10 -> 6 -> 4, each lighter and each equally
    # infeasible, and refused the problem. The bound scan evaluates 24 mm, finds it feasible,
    # and the walk then comes DOWN to the lightest feasible design, which is the answer.
    #
    # 2n extra evaluations, deterministic, and they are exactly the points a person would try
    # first. It does not make the method global -- nothing here is -- but it removes the one
    # failure mode that returns "impossible" for a problem with an obvious answer.
    for p in parameters:
        for edge in (p.low, p.high):
            if ev.evaluations >= max_evaluations:
                break
            probe = dict(best.x)
            probe[p.name] = p.clamp(edge)
            if probe[p.name] == best.x[p.name]:
                continue
            candidate = ev(probe)
            if candidate.rank() < best.rank():
                best = candidate

    step = {p.name: max(p.step or 0.0, START_STEP_FRAC * p.span) for p in parameters}
    by_name = {p.name: p for p in parameters}
    directions = _directions(parameters)
    # The floor is `tol` ABSOLUTELY, in the parameter own units. A fraction-of-range tolerance
    # reads the same on paper and means something different on every parameter, so "tol=1e-4"
    # would silently be 4 microns on one dimension and 40 on another.
    floor = {p.name: (p.step if p.step else tol) for p in parameters}

    converged = False
    while True:
        if ev.evaluations >= max_evaluations:
            break
        improved = False
        # A fixed probe order, so the same inputs give the same answer: an optimizer with a
        # hidden seed cannot be used to justify a design decision.
        for direction in directions:
            if ev.evaluations >= max_evaluations:
                break
            trial = dict(best.x)
            moved_any = False
            for p in parameters:
                d = direction[p.name]
                if not d:
                    continue
                moved = by_name[p.name].clamp(best.x[p.name] + d * step[p.name])
                if moved != best.x[p.name]:
                    moved_any = True
                trial[p.name] = moved
            if not moved_any:
                continue
            candidate = ev(trial)
            if candidate.rank() < best.rank():
                best = candidate
                improved = True
        if improved:
            continue
        # No improving move at this step size: shrink, and stop once every dimension is below
        # its floor. Halving is the standard contraction and keeps the lattice points reachable.
        if all(step[p.name] <= floor[p.name] + 1e-15 for p in parameters):
            converged = True
            break
        for p in parameters:
            step[p.name] = max(step[p.name] / 2.0, floor[p.name])

    if not best.feasible:
        raise InfeasibleBox(
            "no point in the search box satisfies %s. The nearest miss is %s, which gives %s. "
            "Change the requirement, widen a bound, or change the material -- this is not a "
            "number that can be reached by sizing."
            % (" and ".join(c.name for c in constraints) or "the constraints",
               ", ".join("%s=%.6g" % (k, v) for k, v in sorted(best.x.items())),
               "; ".join(best.violations) or "no reported metrics"),
            best=best)

    return Optimum(best=best, history=ev.history, evaluations=ev.evaluations,
                   cache_hits=ev.cache_hits, failures=ev.failures, converged=converged,
                   final_step=dict(step), method="compass search",
                   objective=objective, constraints=constraints, parameters=parameters)


def _bisect(ev, parameter: Parameter, constraints, base: dict, tol: float,
            max_evaluations: int, strict: bool = True, hint: float | None = None):
    """Find the feasible boundary along one parameter. Returns `(outcome, converged)`.

    `base` holds the other parameters, fixed. `strict` controls what happens when the sampled
    feasibility pattern is not one clean switch: `minimise_monotone` refuses, because bisecting a
    non-monotone constraint returns a confident wrong answer; the nested search inside
    `minimise_sized` takes the best sample instead, because an outer point whose inner problem is
    awkward should lose on merit rather than abort the whole run.

    `hint` is a WARM START, and it is what makes the nested search affordable. The sized
    dimension varies smoothly with the outer ones -- a slightly deeper section needs a slightly
    narrower web, not a different kind of answer -- so the previous boundary is almost always
    within a hair of this one. A narrow bracket around the hint is TESTED for a feasibility
    switch and used only if it really brackets one, so the shortcut cannot return a wrong answer;
    when it does not, the full scan runs as though no hint had been given. Measured on the
    section problem: 609 evaluations down to 95, same answer to every digit.
    """
    def at(v: float):
        x = dict(base)
        x[parameter.name] = parameter.clamp(v)
        return ev(x)

    if hint is not None:
        # Widen geometrically from the hint until the bracket holds a switch, or until it covers
        # the whole range -- at which point this has cost a handful of probes and the full scan
        # below runs anyway, most of them already cached.
        width = max(tol * 4.0, parameter.span / 4096.0)
        while width <= parameter.span:
            lo = max(parameter.low, hint - width)
            hi = min(parameter.high, hint + width)
            a, b = at(lo), at(hi)
            if a.feasible != b.feasible:
                bad, good = (lo, hi) if b.feasible else (hi, lo)
                limit = max(tol, parameter.step or 0.0)
                while abs(good - bad) > limit and ev.evaluations < max_evaluations:
                    mid = 0.5 * (good + bad)
                    if parameter.step and parameter.clamp(mid) in (parameter.clamp(good),
                                                                   parameter.clamp(bad)):
                        break
                    if at(mid).feasible:
                        good = mid
                    else:
                        bad = mid
                return at(good), abs(good - bad) <= limit + 1e-15
            width *= 4.0

    probes = [parameter.low + i * parameter.span / 4.0 for i in range(5)]
    sampled = [at(v) for v in probes]
    flags = [o.feasible for o in sampled]

    if not any(flags):
        best = min(sampled, key=lambda o: o.rank())
        if strict:
            raise InfeasibleBox(
                "no point in [%g, %g] satisfies %s; the nearest miss is %s=%.6g giving %s"
                % (parameter.low, parameter.high,
                   " and ".join(c.name for c in constraints),
                   parameter.name, best.x[parameter.name],
                   "; ".join(best.violations) or "no reported metrics"),
                best=best)
        return best, True

    switches = sum(1 for a, b in zip(flags, flags[1:]) if a != b)
    if switches != 1:
        if strict:
            raise ValueError(
                "feasibility over [%g, %g] switches %d time(s), so this problem is not monotone "
                "in %r and bisecting it would return a confident wrong answer. Sampled: %s. Use "
                "minimise() instead."
                % (parameter.low, parameter.high, switches, parameter.name,
                   "".join("P" if f else "." for f in flags)))
        return min((o for o in sampled if o.feasible), key=lambda o: o.objective), True

    # Which end is feasible decides which way to bisect. Both directions occur: a thickness has
    # its feasible side up, a clearance has it down.
    #
    # And bisect within the BRACKET the probes already found rather than over the whole range --
    # the probes cost their evaluations either way, so throwing away what they localised would be
    # paying for them twice. Two fewer halvings, every time.
    switch = next(i for i, (a, b) in enumerate(zip(flags, flags[1:])) if a != b)
    lo_v, hi_v = probes[switch], probes[switch + 1]
    bad, good = (lo_v, hi_v) if flags[switch + 1] else (hi_v, lo_v)

    limit = max(tol, parameter.step or 0.0)
    while abs(good - bad) > limit and ev.evaluations < max_evaluations:
        mid = 0.5 * (good + bad)
        if parameter.step and parameter.clamp(mid) in (parameter.clamp(good),
                                                       parameter.clamp(bad)):
            break
        if at(mid).feasible:
            good = mid
        else:
            bad = mid

    return at(good), abs(good - bad) <= limit + 1e-15


def minimise_sized(evaluate, parameters, *, size: str, objective: str, constraints,
                   start: dict | None = None, tol: float = 1e-3,
                   size_tol: float | None = None, warm_start: bool = True,
                   max_evaluations: int = 800) -> Optimum:
    """Search the free parameters while SIZING one of them to the constraint boundary.

    This is the shape most real design problems have, and it is the one a plain compass search
    cannot do. The optimum of a sizing problem lies **on** a constraint boundary, and when that
    boundary is curved the improving direction runs along it at a ratio that is neither axial nor
    diagonal. Measured on a textbook section problem -- minimise `b*h` subject to
    `b*h^2/6 >= S` -- `minimise` stalls at area 170 against a true 80, and reports honestly that
    it is a local optimum, which is correct and not useful.

    So the dimension is reduced instead. For every setting of the free parameters, `size` is
    bisected to the boundary, and the outer search sees only the reduced objective. Every point
    the outer search looks at is therefore already a *sized* design, and the search is over shape
    rather than over shape-and-size at once -- which is how a person does it, and why.

    The constraints are passed to the outer filter unchanged. When no feasible size exists for an
    outer point, the inner bisection returns the nearest miss, which genuinely violates, and the
    filter walks away from it with no extra machinery.

    **The cost model, because it is the thing callers get wrong.** This is (outer rounds) x
    (inner bisection), so it is multiplicative in the tolerances. `tol` is in the free
    parameters own units, and asking for a micron on a part dimension buys nothing a machine can
    hold while costing a factor of ten; `size_tol` defaults to `tol` for the same reason. The
    `warm_start` makes the inner bisection resume from the previous boundary rather than
    rescanning, which is worth roughly a factor of three on a smooth problem and can be turned
    off to measure that.
    """
    parameters = list(parameters)
    constraints = list(constraints)
    _validate(parameters, objective, constraints)
    by_name = {p.name: p for p in parameters}
    if size not in by_name:
        raise ValueError(
            "size=%r is not one of the parameters (%s); the sized parameter has to be one of "
            "the things being varied" % (size, ", ".join(sorted(by_name))))
    if len(parameters) < 2:
        raise ValueError(
            "minimise_sized needs at least one free parameter besides %r. With only the sized "
            "one, use minimise_monotone -- it is the same search without an outer loop." % size)

    sized = by_name[size]
    free = [p for p in parameters if p.name != size]
    # The size does not need to be resolved finer than the shape it serves. Defaulting this to
    # something much tighter than `tol` quietly multiplies the cost of every outer step, which is
    # how a nested search gets a reputation for being unaffordable.
    if size_tol is None:
        size_tol = tol
    shared = _Evaluator(evaluate, parameters, objective, constraints)
    inner_calls = {"n": 0}

    last = {"size": None}

    def outer(x: dict) -> dict:
        """Size the design at this shape, and report what the sized design achieves."""
        inner_calls["n"] += 1
        base = dict(x)
        base.setdefault(size, sized.clamp(sized.low + 0.5 * sized.span))
        outcome, _ok = _bisect(shared, sized, constraints, base, size_tol,
                               max_evaluations, strict=False,
                               hint=last["size"] if warm_start else None)
        last["size"] = outcome.x[size]
        metrics = dict(outcome.metrics)
        # Carry the size the inner search chose, so the outer best knows its own geometry.
        metrics["_sized_" + size] = outcome.x[size]
        return metrics

    outer_result = minimise(outer, free, objective=objective, constraints=constraints,
                            start={k: v for k, v in (start or {}).items() if k != size},
                            tol=tol, max_evaluations=max_evaluations)

    # Re-attach the sized dimension to the reported best, so `best.x` is a whole design.
    best = outer_result.best
    chosen = best.metrics.get("_sized_" + size)
    full_x = dict(best.x)
    if chosen is not None:
        full_x[size] = chosen
    metrics = {k: v for k, v in best.metrics.items() if not k.startswith("_sized_")}
    best = Outcome(x=full_x, objective=best.objective, metrics=metrics,
                   feasible=best.feasible, violation=best.violation,
                   violations=list(best.violations), failed=best.failed)

    return Optimum(best=best, history=shared.history, evaluations=shared.evaluations,
                   cache_hits=shared.cache_hits, failures=shared.failures,
                   converged=outer_result.converged,
                   final_step=dict(outer_result.final_step),
                   method="nested: %s bisected to the boundary, %s searched"
                          % (size, " and ".join(p.name for p in free)),
                   objective=objective, constraints=constraints, parameters=parameters)


def minimise_monotone(evaluate, parameter: Parameter, *, objective: str, constraints,
                      tol: float = 1e-6, max_evaluations: int = 200) -> Optimum:
    """Bisect the constraint boundary of a ONE-parameter monotone problem. Exact, and cheap.

    This is the shape most real sizing problems have: thicker is both stronger and heavier, so
    the lightest safe part is the thinnest safe part, and the answer is a boundary rather than a
    minimum. Bisection finds it in `log2(span/tol)` evaluations where a pattern search needs
    many more, and it cannot get stuck.

    **Monotonicity is verified, not assumed.** A non-monotone constraint bisected anyway would
    return a confident wrong answer, so this samples the interval first and refuses if the
    feasibility pattern is not one clean switch. Use `minimise` for anything it refuses.
    """
    constraints = list(constraints)
    _validate([parameter], objective, constraints)
    ev = _Evaluator(evaluate, [parameter], objective, constraints)

    best, converged = _bisect(ev, parameter, constraints, {}, tol, max_evaluations, strict=True)
    if not best.feasible:                       # only reachable if the lattice has no feasible
        raise InfeasibleBox(                    # point even though a continuous one existed
            "the feasible boundary along %s does not land on the %g lattice; the nearest lattice "
            "point fails. Loosen the requirement or refine the lattice."
            % (parameter.name, parameter.step or 0.0), best=best)

    return Optimum(best=best, history=ev.history, evaluations=ev.evaluations,
                   cache_hits=ev.cache_hits, failures=ev.failures, converged=converged,
                   final_step={parameter.name: max(tol, parameter.step or 0.0)},
                   method="monotone bisection", objective=objective,
                   constraints=constraints, parameters=[parameter])
