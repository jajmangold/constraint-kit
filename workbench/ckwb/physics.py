# -*- coding: utf-8 -*-
"""Bridge from a FreeCAD part to the verified `constraint_kit.fea` tier.

This runs the structural solver *inside FreeCAD*, in-process, which is only possible because the
physics tier was deliberately built on numpy and scipy alone — both of which FreeCAD 1.0 already
ships (numpy 1.26.4, scipy 1.16.0). Nothing is installed, no service is started, and no geometry
is marshalled anywhere.

The bridge itself is small: tessellate the part's shape, hand the triangles to the voxeliser, run
the solve, report. All the verification lives in the tier's own suite — element symmetry, six
rigid-body modes, `PL/AE` and `P/A` exact to 1e-9, bending convergent to Timoshenko — so there is
nothing to re-prove here beyond the plumbing.

Mesh deflection is tessellated at a quarter of the voxel pitch: meshing finer than the sampling
grid is wasted work, and meshing coarser puts facet error into the geometry the solver sees.
"""
from __future__ import annotations

import os
import sys

import FreeCAD

#: Material table, matching drivers/fea_part.py so a number means the same thing in both places.
#: E (MPa), nu, density (g/cm3), allowable (MPa), note.
MATERIALS = {
    "petg": (2000.0, 0.40, 1.27, 15.0, "printed, in-plane; creep-aware allowable"),
    "pla": (3300.0, 0.36, 1.24, 20.0, "printed, in-plane; poor above 50 C"),
    "abs": (2000.0, 0.35, 1.04, 15.0, "printed, in-plane"),
    "al6061": (68900.0, 0.33, 2.70, 165.0, "T6 wrought, yield 276 MPa at SF 1.67"),
    "steel": (200000.0, 0.30, 7.85, 150.0, "mild steel, yield 250 MPa at SF 1.67"),
}


def _import_fea():
    """Import the physics tier, adding the repo root to sys.path if it is not already importable.

    The workbench normally lives at `<constraint-kit>/workbench`, so the tier is one directory up.
    `CK_REPO` overrides that for an installed copy (a symlink into FreeCAD's Mod directory, say),
    and the error names the fix rather than leaving an ImportError to interpret.
    """
    try:
        from constraint_kit import fea
        return fea
    except ImportError:
        pass
    # realpath so a linked install resolves to the real checkout rather than to FreeCAD's
    # Mod directory, which has no constraint_kit beside it.
    here = os.path.realpath(__file__)
    candidates = [os.environ.get("CK_REPO"),
                  os.path.dirname(os.path.dirname(os.path.dirname(here)))]
    for root in candidates:
        if root and os.path.isdir(os.path.join(root, "constraint_kit")):
            if root not in sys.path:
                sys.path.insert(0, root)
            try:
                from constraint_kit import fea
                return fea
            except ImportError:
                continue
    raise ImportError(
        "cannot import constraint_kit.fea. It needs only numpy and scipy, both of which FreeCAD "
        "ships, so this is a path problem: set CK_REPO to the constraint-kit checkout.")


_DIRECT_OK: bool | None = None


def direct_solver_available() -> bool:
    """Is scipy's sparse DIRECT solver usable in this interpreter? Probed once, then cached.

    It is not, inside FreeCAD 1.0.2. The bundle pairs numpy 1.26.4 with scipy 1.16.0, and
    scipy's compiled SuperLU extension was built against a different numpy ABI, so `spsolve`
    and `factorized` both fail with the memorable `argument 3 must be numpy.ndarray, not
    numpy.ndarray` — two distinct ndarray types in one process. Pure-Python iterative solvers
    (`cg`, `minres`) and sparse matvec are unaffected, because they never cross that boundary.

    This is probed rather than hardcoded on "are we in FreeCAD", so a future bundle that ships a
    matched pair gets the faster direct path automatically with no code change.

    It also means the physics tier works here *only* because its CG path uses a Jacobi
    preconditioner. The ILU alternative that was removed earlier (as mathematically invalid for
    CG) would have failed here too, for this entirely separate reason: `spilu` is the same
    SuperLU extension.
    """
    global _DIRECT_OK
    if _DIRECT_OK is None:
        try:
            import numpy as np
            import scipy.sparse as sp
            import scipy.sparse.linalg as spla
            spla.spsolve(sp.csc_matrix(np.eye(3)), np.ones(3))
            _DIRECT_OK = True
        except Exception:  # noqa: BLE001 - any failure means "do not use it"
            _DIRECT_OK = False
    return _DIRECT_OK


def triangles_of(shape, deflection: float):
    """Tessellate a shape into an (n, 3, 3) array of triangle vertices, in world coordinates."""
    import numpy as np

    verts, facets = shape.tessellate(deflection)
    if not facets:
        raise ValueError("the shape tessellated to no triangles; is it an empty or non-solid shape?")
    pts = np.array([[v.x, v.y, v.z] for v in verts], dtype=float)
    idx = np.array(facets, dtype=int)
    return pts[idx]


def _selector(expr: str):
    """Reuse the driver's parsed (never eval'd) selector language: `x<=-59 & z>=0`."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from drivers.fea_part import parse_fix
    return parse_fix(expr)


def prepare(obj, pitch: float = 2.0, material: str = "petg") -> dict:
    """Everything that must happen on the GUI thread, and nothing that need not.

    Tessellation is OCCT, so it has to run where OCCT is safe. The solve after it is numpy and
    scipy only and is safe anywhere — which is the whole reason the physics tier was built with
    no CAD dependency, and it pays off exactly here: a 60k-DOF solve can go to a worker thread
    while the triangles are produced on the thread that owns the geometry.

    Returns the argument bundle `solve_prepared` wants. Call it from the GUI thread, hand the
    result to a worker.
    """
    key = str(material).lower()
    if key not in MATERIALS:
        raise ValueError(f"unknown material {material!r}; have {', '.join(sorted(MATERIALS))}")
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(f"{getattr(obj, 'Name', obj)} has no shape to analyse")
    return {
        "part": getattr(obj, "Name", ""),
        "material": key,
        "pitch": float(pitch),
        "triangles": triangles_of(shape, max(float(pitch) / 4.0, 1e-3)),
        # The EXACT B-rep volume, captured here because this is where the shape is. Mass used to
        # be reported from the voxel count, which is a staircase: at 4 mm pitch a plate 7 mm
        # thick and one 10 mm thick voxelise identically and weigh the same to the gram. An
        # optimizer minimising that is minimising a step function and stops on a plateau; worse,
        # the number was being quoted, and it is up to 10% out (+10.7% measured on the gantry
        # plate at 1.82 mm). The exact volume is free, smooth in the parameters, and correct.
        "shape_volume_mm3": float(shape.Volume),
    }


def stiffness(obj, material: str = "petg", pitch: float = 2.0, *,
              fix: str, load: str = "", force=(0.0, 0.0, -100.0), cases=None,
              buckling: int = 0) -> dict:
    """Solve one part for deflection and stress. Boundary conditions are arguments, never guessed.

    `fix` and `load` are selector expressions over node coordinates in millimetres, in the part's
    own placed position — e.g. `x<=-59`, or `x>=24@xz` to restrain only two axes, which is what a
    roller or a V-wheel actually does.

    Returns a structured verdict with the mesh size, so a number is never quoted without the
    resolution it came from. For a converged answer run several pitches and extrapolate with
    `constraint_kit.fea.richardson`, which declines rather than inventing a limit.
    """
    bundle = prepare(obj, pitch=pitch, material=material)
    return solve_prepared(bundle, fix=fix, load=load, force=force, cases=cases,
                          buckling=buckling)


def _check_cases(cases) -> list:
    """Validate load cases before any geometry is touched, and name what is wrong.

    Each entry is `{"name", "load", "force"}`. Refused rather than defaulted: a case missing its
    force would otherwise be solved as zero and report an infinite safety factor for a load
    that was meant to be applied.
    """
    out = []
    for i, entry in enumerate(cases):
        if not isinstance(entry, dict):
            raise ValueError("load case %d must be an object with name, load and force" % i)
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError("load case %d has no name; every case must be named so the "
                             "envelope can say which one governs" % i)
        where = str(entry.get("load") or "").strip()
        if not where:
            raise ValueError("load case %r has no load selector" % name)
        force = entry.get("force")
        if force is None or len(force) != 3:
            raise ValueError("load case %r needs a force of exactly three components "
                             "[fx, fy, fz] in newtons" % name)
        if not any(float(v) for v in force):
            raise ValueError("load case %r has a zero force, so it would report an infinite "
                             "safety factor for a load that was meant to be applied" % name)
        out.append((name, where, tuple(float(v) for v in force)))
    if not out:
        raise ValueError("cases was given but is empty")
    seen = [n for n, _, _ in out]
    if len(set(seen)) != len(seen):
        raise ValueError("duplicate load case name(s): %s"
                         % ", ".join(sorted({n for n in seen if seen.count(n) > 1})))
    return out


def solve_prepared(bundle: dict, *, fix: str, load: str = "",
                   force=(0.0, 0.0, -100.0), cases=None, buckling: int = 0) -> dict:
    """The numpy half: voxelise the triangles and solve. Safe on any thread.

    Either one `load`/`force`, or several named `cases` -- never both. The two are different
    questions with different answers, and picking a reading on the caller behalf is how a
    safety factor ends up meaning something that was never intended.
    """
    fea = _import_fea()
    key = bundle["material"]
    E, nu, density, allowable, _note = MATERIALS[key]
    pitch = float(bundle["pitch"])

    parsed = _check_cases(cases) if cases else None
    grid = fea.occupancy_from_triangles(bundle["triangles"], pitch, pad=1)

    model = fea.Model(grid, E, nu)
    for expr in (fix,) if isinstance(fix, str) else fix:
        sel, axes = _selector(expr)
        model.fix(sel, axes=axes)

    # "auto" now probes for itself -- `fea.voxel.direct_solver_available` is the same check
    # this module used to make on its own, moved into the tier so every caller gets it. Inside
    # FreeCAD it resolves to CG, because the bundled SuperLU is ABI-broken.
    method = "auto"
    envelope = None
    if parsed:
        for name, where, vec in parsed:
            sel, _axes = _selector(where)
            model.load(sel, vec, case=name)
        envelope = model.solve_cases(method=method)
        res = envelope
    else:
        load_sel, _axes = _selector(load)
        model.load(load_sel, tuple(float(v) for v in force))
        res = model.solve(method=method)
    voxel_cm3 = grid.material_volume / 1000.0
    exact_cm3 = float(bundle.get("shape_volume_mm3") or 0.0) / 1000.0
    volume_cm3 = exact_cm3 or voxel_cm3
    peak = res.von_mises_percentile(99.0)
    out = {
        "part": bundle.get("part", ""),
        "material": key,
        "pitch_mm": pitch,
        "elements": int(len(res.active_elements)),
        "dof": int(res.n_dof),
        "method": res.method,
        "residual": (res.residual if envelope is None
                     else envelope.cases[envelope.worst_case_for_stress].residual),
        "volume_cm3": round(volume_cm3, 4),
        "mass_g": round(volume_cm3 * density, 3),
        # The voxel figure beside the exact one, because their DIFFERENCE is a mesh-quality
        # signal worth having: a staircase that adds 10% material is a staircase whose stress
        # and stiffness are not to be trusted either.
        "voxel_mass_g": round(voxel_cm3 * density, 3),
        "voxel_volume_error_pct": (round(100.0 * (voxel_cm3 / exact_cm3 - 1.0), 2)
                                   if exact_cm3 else None),
        "max_disp_mm": round(res.max_displacement, 6),
        "stress_p99_mpa": round(peak, 4),
        "stress_peak_mpa": round(res.peak_von_mises, 4),
        "allowable_mpa": allowable,
        "safety_factor": round(allowable / peak, 3) if peak > 0 else None,
        "compliance": (res.compliance if envelope is None
                       else envelope.cases[envelope.worst_case_for_stress].compliance),
        "direct_solver": direct_solver_available(),
        "note": "one mesh is not a converged answer; run a ladder and extrapolate. mass_g is "
                "from the exact B-rep volume; voxel_mass_g is what the grid sampled, and a "
                "large gap between them means this pitch is not resolving the geometry.",
    }
    if envelope is not None:
        out.update(_envelope_rows(envelope))
    out.update(_linearity(res.max_displacement, grid))
    if buckling:
        out.update(_buckling(model, int(buckling)))
    if key in PRINTED:
        out.update(_interlayer(fea, res, allowable))
    return out


# Deflection beyond this fraction of the part's SPAN means linear theory has been left behind.
#
# Against the span, not the thickness -- which was the first thing tried here and is the wrong
# criterion. What linear elasticity actually assumes is that ROTATIONS stay small enough for
# sin(theta) ~ theta and for the geometry not to change as the load is applied, and rotation
# scales with deflection over span. A cantilever can deflect through several times its own
# thickness while its rotations are still a few degrees; a thickness-based rule flagged a
# perfectly valid 4.5%-of-span answer and would have taught everyone to ignore the warning.
#
# A tenth of the span is the conventional line: about 6 degrees of rotation.
LINEAR_LIMIT = 0.10


def _linearity(deflection: float, grid) -> dict:
    """Does the answer still satisfy the assumption it was computed under?

    Found by using the thing: a 4 mm bracket reported 124 mm of deflection on a 120 mm part, with
    the same composure as a 5 mm answer. Linear small-displacement elasticity assumes the
    geometry does not change as the load is applied; a deflection comparable to the part's own
    thickness breaks that outright. The number is then not imprecise, it is the answer to a
    different question.

    Nothing here can solve the nonlinear problem, and that is fine. Reporting the linear number
    without saying so is not -- it is exactly the believable-wrong-answer failure that has been
    the expensive kind in this codebase.
    """
    import numpy as np

    occupied = np.argwhere(grid.density > 0.0)
    if occupied.size == 0:
        return {"linear_valid": True, "linear_note": ""}
    extent = (occupied.max(axis=0) - occupied.min(axis=0) + 1) * np.asarray(grid.pitch)
    span = float(extent.max())
    ratio = deflection / span if span > 0 else 0.0
    if ratio <= LINEAR_LIMIT:
        return {"linear_valid": True, "linear_note": ""}
    return {
        "linear_valid": False,
        "linear_note": (
            "deflection is %.3g mm across a span of %.3g mm (%.0f%%), so rotations are no longer "
            "small and linear small-displacement theory does not hold here. The real part "
            "stiffens as it deflects, so this figure OVERSTATES the movement -- read it as 'far "
            "too floppy', not as a number. Stiffen it and run again; nothing in this tier solves "
            "the large-displacement problem." % (deflection, span, 100.0 * ratio)),
    }


def _envelope_rows(envelope) -> dict:
    """Per-case numbers plus which case governs each element.

    The share-by-element line is the one worth reading: when more than one case appears in it,
    no single case is the design case and the envelope is the only field that bounds the part.
    """
    governing = envelope.governing_case_per_element()
    counts = {name: int((governing == name).sum()) for name in envelope.cases}
    return {
        "cases": {name: {"max_disp_mm": round(r.max_displacement, 6),
                         "stress_p99_mpa": round(r.von_mises_percentile(99.0), 4),
                         "stress_peak_mpa": round(r.peak_von_mises, 4),
                         "applied_force_n": [round(v, 4) for v in r.applied_force]}
                  for name, r in envelope.cases.items()},
        "governing_case": envelope.worst_case_for_stress,
        "governs_by_element": dict(counts),
        "envelope_note": (
            "stress is the ELEMENTWISE maximum over the cases, so the safety factor is never "
            "better than the worst case. More than one name in governs_by_element means no "
            "single case is the design case."),
    }


def _buckling(model, n_modes: int) -> dict:
    """Critical load factors, for the failure mode a stress check cannot see.

    Off by default because an eigensolve costs several times a static solve, and on by choice
    for anything slender -- which is to say, for anything an optimizer has been asked to make
    light. A stress check is blind to stability: the member sits nowhere near its allowable
    right up until it folds.

    `buckling_factor` is reported as a plain number so an optimizer can constrain it, and
    `{"metric": "buckling_factor", "op": ">=", "limit": 2.0}` is the constraint to write. Two
    rather than one, because linear buckling is an upper bound on a perfect member and a real
    printed part has a bowed wall and an eccentric load.
    """
    from constraint_kit.fea.buckling import BucklingFailure, buckling as _solve

    try:
        out = _solve(model, n_modes=n_modes)
    except BucklingFailure as exc:
        # "It does not buckle" is a real answer. Reported as an unbounded factor so a
        # constraint on it still resolves, with the reason beside it rather than inferred.
        return {"buckling_factor": None,
                "buckling_note": str(exc),
                "buckles": False}
    return {
        "buckling_factor": round(out.critical_factor, 4),
        "buckling_factors": [round(f, 4) for f in out.factors],
        "buckling_localisation": [round(v, 4) for v in out.localisation],
        "buckling_suspect_modes": [i + 1 for i in out.suspect],
        "buckles": True,
        "buckling_note": (
            "a LOAD FACTOR: 1.0 means the applied load is exactly critical. Linear buckling is "
            "an UPPER BOUND on a perfect member -- a real part with a bowed wall or an "
            "eccentric load fails below it, so keep 2.0 or more on anything slender. A "
            "localisation near 1.0 means the mode sits in a few voxels and is probably a "
            "staircase artefact rather than a structural finding."),
    }


# Which materials are PRINTED, and so have a weak direction no isotropic measure can see. An
# interlayer check on wrought aluminium or mild steel would be a number that means nothing.
PRINTED = {"petg", "pla", "abs"}


def _interlayer(fea, res, allowable: float) -> dict:
    """Rank the three printable orientations by whether the WELDS survive.

    Reported by default rather than behind a flag, because for a printed part the isotropic
    answer is the wrong default. On the knee-exo gantry plate the welds govern at SF 0.74 printed
    on its end against 1.63 printed flat, from the same load and the same geometry -- so the
    orientation is a structural decision, and one a von Mises number cannot express.
    """
    from constraint_kit.fea import printed

    if getattr(res, "stress", None) is None:
        return {}
    ranked = printed.best_orientation(res.stress, in_plane_allowable=allowable)
    table = {}
    for name, check in ranked:
        table[name] = {
            "peel_mpa": round(check.peel, 4),
            "shear_mpa": round(check.shear, 4),
            "sf_peel": None if check.sf_peel == float("inf") else round(check.sf_peel, 3),
            "sf_shear": None if check.sf_shear == float("inf") else round(check.sf_shear, 3),
            "governing": None if check.governing == float("inf") else round(check.governing, 3),
            "governing_mode": check.governing_mode,
            "passes": check.passes,
        }
    return {
        "interlayer": table,
        "best_print_orientation": ranked[0][0],
        "worst_print_orientation": ranked[-1][0],
        "factors": dict(ranked[0][1].factors),
        "interlayer_note": (
            "across-layer allowables are ASSUMED from the in-plane one by the factors above: "
            "literature-typical for unannealed FDM, not measured for this filament, nozzle "
            "temperature, layer height or printer. Measure them if the margin is thin. The "
            "orientation named is the layer-plane normal to print with."),
    }
