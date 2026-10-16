# -*- coding: utf-8 -*-
"""Mechanism mobility and loop closure from the joint graph (closes #11-#14).

Those four issues asked for `prismatic`, `screw`, `belt` and `gear_rack` mates. `ckwb/motion.py`
already delivered the **motion** -- a nut at a travel, a carriage at a position, a pulley at an
angle, all in closed form and swept for interference through the travel. What the comments on
those issues said was still missing was *"a real joint with a degree of freedom the solver
understands, which is what would give DOF analysis and loop closure"*, and that was taken to be
blocked behind #28 and FreeCAD's undocumented `Reference` path format.

It was not. **Mobility is a property of the joint GRAPH, not of the geometry**: how many bodies,
connected by what joints, each removing how many degrees of freedom. Grubler and Kutzbach wrote
it down in the 1880s and it is arithmetic. So the capability arrives without the blocker, and #28
is no longer in the way of the thing it was blocking.

    M = d*(n - 1) - sum over joints of (constraints removed)

with `d = 6` spatial or `3` planar and `n` bodies.

**Two kinds of joint remove constraints differently, and conflating them is the trap.** A LOWER
PAIR -- a revolute, a prismatic, a bolt in a threaded hole -- is surface contact between two
bodies, leaving `f` freedoms and so removing `(d - f)`. A COUPLING -- a gear mesh, a belt, a
ballscrew nut -- is a single scalar relation between two bodies that are already connected some
other way, and removes exactly **one** freedom whatever `d` is.

That distinction was found the hard way, by this module getting it wrong: counting a belt as a
lower pair that "leaves one freedom" removes `(3-1) = 2` in the plane, and a two-pulley belt
drive then counts as a STRUCTURE -- zero mobility, for a mechanism anybody can turn by hand. The
same mistake made a ballscrew axis read as over-constrained by one. See `LOWER_PAIRS` and
`COUPLINGS`.

**What this is not, and the distinction is the whole value of the module.** Grubler **counts**;
it does not analyse. The formula assumes the constraints are independent, and for special
geometries they are not -- a Bennett linkage counts as zero mobility and moves perfectly well.
Those paradoxes live in closed loops, so the loop count is reported beside the mobility and the
report says the number is a count rather than a proof whenever a loop exists. A mobility figure
quoted without that caveat is exactly the confident wrong answer this project spends most of its
effort avoiding.

It is also why a **floating body is named rather than summed**. Mobility is a sum, so one
disconnected part with six free degrees of freedom and a mechanism over-constrained by six cancel
out exactly, and the total looks perfect while the model is wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: LOWER PAIRS: surface-contact joints between two bodies. Each leaves `f` freedoms and so
#: removes `(d - f)` of the `d` a free body has.
LOWER_PAIRS = {
    "fixed": 0,
    "revolute": 1,
    "prismatic": 1,
    "screw": 1,
    "cylindrical": 2,
    "universal": 2,
    "spherical": 3,
    "planar": 3,
}

#: COUPLINGS: a single scalar relation between two bodies that are already connected some other
#: way. A gear mesh, a belt, a rack and pinion, a ballscrew nut -- each ties one motion to
#: another by a fixed ratio and so removes exactly ONE degree of freedom, whatever `d` is.
#:
#: **This distinction is the thing people get wrong, and it got me.** Counting a belt as a lower
#: pair that "leaves one freedom" removes `(3-1) = 2` in the plane, and a two-pulley belt drive
#: then counts as a STRUCTURE -- zero mobility, for a mechanism anybody can turn by hand. The
#: same mistake made the ballscrew axis count as over-constrained. A coupling is not a joint in
#: the Grubler sense; it is one equation.
#:
#: So `screw` appears in BOTH tables, with different meanings, and the difference is real:
#:
#: * `screw` as a LOWER PAIR is a bolt turning in a threaded hole -- the only connection between
#:   the two bodies, leaving the one helical freedom.
#: * `screw_coupling` is a ballscrew nut -- the shaft already has a revolute to the frame and the
#:   carriage already has a prismatic, and the nut ties those two together. One equation.
COUPLINGS = {
    "gear": 1,
    "belt": 1,
    "screw_coupling": 1,
}

#: Names people actually use. `gear_rack` had its own issue (#14) and so has to be sayable.
ALIASES = {
    "gear_rack": "gear",
    "rack": "gear",
    "rack_and_pinion": "gear",
    "pulley": "belt",
    "nut": "screw_coupling",
    "ballscrew": "screw_coupling",
    "ballnut": "screw_coupling",
    "pin": "revolute",
    "hinge": "revolute",
    "slider": "prismatic",
    "slide": "prismatic",
    "rail": "prismatic",
    "ball": "spherical",
    "weld": "fixed",
    "rigid": "fixed",
    "helical": "screw",
}

#: Kept for callers that only care about lower pairs, and because the table reads better as one
#: name. A coupling has no meaningful "freedoms left" -- it is a constraint, not a pair.
FREEDOMS = dict(LOWER_PAIRS)


def canonical(kind: str) -> str:
    """The table name for a joint kind, resolving aliases. Refuses an unknown kind."""
    key = str(kind or "").strip().lower()
    key = ALIASES.get(key, key)
    if key not in LOWER_PAIRS and key not in COUPLINGS:
        raise ValueError(
            "no joint kind %r. Lower pairs: %s. Couplings: %s. Aliases: %s."
            % (kind, ", ".join(sorted(LOWER_PAIRS)), ", ".join(sorted(COUPLINGS)),
               ", ".join(sorted(ALIASES))))
    return key


def is_coupling(kind: str) -> bool:
    return canonical(kind) in COUPLINGS


def freedoms_of(kind: str) -> int:
    """Freedoms a LOWER PAIR leaves. A coupling reports 1, being one freedom tied to another."""
    key = canonical(kind)
    return COUPLINGS[key] if key in COUPLINGS else LOWER_PAIRS[key]


def constraints_of(kind: str, d: int) -> int:
    """How many degrees of freedom this joint removes in a `d`-dimensional world.

    `(d - f)` for a lower pair; exactly 1 for a coupling, regardless of `d` -- which is the
    whole point of separating the two.
    """
    key = canonical(kind)
    if key in COUPLINGS:
        return COUPLINGS[key]
    return d - LOWER_PAIRS[key]


@dataclass(frozen=True)
class Mobility:
    """How many degrees of freedom a mechanism has, and how much to believe the number."""

    mobility: int
    bodies: int
    joints: int
    loops: int
    spatial: bool
    grounded: list
    floating: list = field(default_factory=list)
    terms: list = field(default_factory=list)

    @property
    def over_constrained(self) -> bool:
        return self.mobility < 0

    @property
    def is_structure(self) -> bool:
        return self.mobility == 0

    def report(self) -> str:
        d = 6 if self.spatial else 3
        counts: dict = {}
        for kind, _removed in self.terms:
            counts[kind] = counts.get(kind, 0) + 1
        inventory = ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())) or "none"

        lines = [
            "%s mobility: %d body(ies), %d joint(s), %d independent loop(s)"
            % ("spatial" if self.spatial else "planar", self.bodies, self.joints, self.loops),
            "  joints: " + inventory,
            "  M = %d*(%d - 1) - [%s] = %d"
            % (d, self.bodies,
               " + ".join("%s %d" % (k, c) for k, c in self.terms) or "0",
               self.mobility),
            "  a lower pair removes %d minus the freedoms it leaves; a coupling (gear, belt, "
            "ballscrew nut) is one equation and removes exactly 1" % d,
        ]
        if not self.grounded:
            lines.append("  nothing is grounded, so the whole assembly is free to move as a "
                         "rigid body and six of these freedoms are that.")
        if self.floating:
            lines.append("  NOT CONNECTED: %s. Mobility is a SUM, so a free body with six "
                         "freedoms and a mechanism over-constrained by six cancel out exactly "
                         "-- the total would look right while the model was wrong."
                         % ", ".join(self.floating))
        if self.over_constrained:
            lines.append("  OVER-CONSTRAINED by %d. Some joints are redundant: the assembly "
                         "cannot move, and cannot be assembled at all without forcing something "
                         "out of position." % -self.mobility)
        elif self.is_structure:
            lines.append("  a STRUCTURE rather than a mechanism: nothing moves, which is "
                         "correct for a frame and wrong for an axis.")
        elif self.mobility == 1:
            lines.append("  one degree of freedom: one input fully determines the pose, which is "
                         "what a drive train should be.")
        else:
            lines.append("  %d degrees of freedom, so %d independent inputs are needed to "
                         "determine the pose." % (self.mobility, self.mobility))
        if self.loops:
            lines.append("  This is a COUNT, not a proof. The formula assumes the joint "
                         "constraints are independent, and in special geometries they are not --")
            lines.append("  a Bennett linkage counts as zero and moves anyway. Those paradoxes "
                         "live in closed loops, and this mechanism has %d. Treat the number as "
                         "a strong hint and the geometry as the authority." % self.loops)
        else:
            lines.append("  No loops, so the count is exact: an open chain has no redundant "
                         "constraints to hide.")
        return "\n".join(lines)


def _components(bodies, edges) -> list:
    """Connected components of the body graph, as lists of names. Plain union-find."""
    parent = {b: b for b in bodies}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups: dict = {}
    for b in bodies:
        groups.setdefault(find(b), []).append(b)
    return [sorted(v) for v in groups.values()]


def mobility(*, bodies, joints, grounded=(), spatial: bool = True) -> Mobility:
    """Grubler/Kutzbach mobility of the joint graph, with the loop count beside it.

    `bodies` are names; `joints` are `{kind, a, b}`; `grounded` are the bodies fixed to the world.
    `spatial=False` for a planar mechanism, which is most of what is on this bench -- a four-bar,
    a slider-crank and a belt drive are all planar, and using the spatial formula on them gives a
    number three times too large.
    """
    names = [str(b) for b in bodies]
    if not names:
        raise ValueError("at least one body is needed")
    if len(set(names)) != len(names):
        duplicated = sorted({n for n in names if names.count(n) > 1})
        raise ValueError("duplicate body name(s): %s" % ", ".join(duplicated))
    known = set(names)

    for who in grounded:
        if who not in known:
            raise ValueError(
                "cannot ground %r, which is not one of the bodies (%s)"
                % (who, ", ".join(sorted(known))))

    edges, terms = [], []
    for i, joint in enumerate(joints):
        if not isinstance(joint, dict):
            raise ValueError("joint %d must be an object with kind, a and b" % i)
        a, b = joint.get("a"), joint.get("b")
        for who in (a, b):
            if who not in known:
                raise ValueError(
                    "joint %d connects %r, which is not one of the bodies (%s)"
                    % (i, who, ", ".join(sorted(known))))
        if a == b:
            raise ValueError("joint %d connects %r to itself" % (i, a))
        kind = canonical(joint.get("kind"))
        edges.append((a, b))
        terms.append(kind)

    d = 6 if spatial else 3
    # The ground counts as ONE body however many parts are fixed to it -- that is what grounding
    # means. With nothing grounded, every body is free and the n-1 term is the whole assembly
    # floating as a unit.
    ground = [b for b in names if b in set(grounded)]
    n_bodies = len(names) - max(len(ground) - 1, 0)
    if not ground:
        n_bodies = len(names) + 1          # the world is a body nothing is attached to

    removed = [(kind, constraints_of(kind, d)) for kind in terms]
    value = d * (n_bodies - 1) - sum(c for _k, c in removed)

    # Loops: for each connected component, edges - nodes + 1. Bodies fixed to ground are one
    # node, so a closed chain through the ground still counts as the one loop it is.
    merged = list(edges)
    if len(ground) > 1:
        merged += [(ground[0], other) for other in ground[1:]]
    comps = _components(names, merged)
    loops = 0
    for group in comps:
        inside = [e for e in merged if e[0] in group and e[1] in group]
        loops += max(len(inside) - len(group) + 1, 0)

    floating = []
    if ground:
        reachable = next((g for g in comps if ground[0] in g), [])
        floating = sorted(set(names) - set(reachable))

    return Mobility(mobility=int(value), bodies=len(names), joints=len(terms), loops=int(loops),
                    spatial=bool(spatial), grounded=sorted(ground), floating=floating,
                    terms=removed)
