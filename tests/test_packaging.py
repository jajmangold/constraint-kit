#!/usr/bin/env python3
"""The dependency claims this repo makes about itself, checked (issue #24).

    python3 tests/test_packaging.py

Three claims are made repeatedly in the docs and nothing verified any of them:

1. **the physics tier needs only numpy and scipy**, which is why it runs inside FreeCAD's own
   interpreter with nothing installed
2. **the optimizer needs neither CAD nor FEA**, which is why its tests assert against closed-form
   optima
3. **the pins describe a stack somebody actually ran**

The first two are structural and are checked here by reading the imports with `ast` rather than
by importing anything -- which matters, because a test that imports a module to find out what it
imports can only run in an environment that already has everything.

The third is the one issue #24 was filed about, and the finding was worse than a stale version
number. `pyproject.toml` declared ONE dependency list for THREE environments: the container (a
pinned cadquery/build123d stack, 113 tests), the host (cadquery 2.8, no build123d), and FreeCAD's
bundled Python (3.11, numpy 1.26, scipy 1.16, no pip at all). So `pip install constraint-kit`
pulled cadquery, fastapi, neo4j and langgraph on somebody who only wanted the tier that
deliberately needs none of them. **The packaging contradicted the architecture**, which is a
worse problem than a wrong pin and is what these tests now prevent.
"""
from __future__ import annotations

import ast
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TESTS = []

#: Modules that must run on numpy and scipy alone, because something important rests on it.
#: `fea/tet.py` is in here too: it SHELLS OUT to gmsh and CalculiX rather than importing them,
#: which is exactly why it belongs on this list rather than being exempt from it.
CORE = (
    "constraint_kit/fea/hex8.py",
    "constraint_kit/fea/voxel.py",
    "constraint_kit/fea/sample.py",
    "constraint_kit/fea/convergence.py",
    "constraint_kit/fea/buckling.py",
    "constraint_kit/fea/printed.py",
    "constraint_kit/fea/crosscheck.py",
    "constraint_kit/fea/tet.py",
    "constraint_kit/fea/__init__.py",
    "constraint_kit/optimise.py",
    "constraint_kit/manufacture.py",
    "constraint_kit/parameters.py",
)

#: Everything a core module may import. numpy and scipy, and the standard library.
ALLOWED_THIRD_PARTY = {"numpy", "scipy"}

STDLIB = set(getattr(sys, "stdlib_module_names", ()))


def test(fn):
    TESTS.append(fn)
    return fn


def _names(node) -> set:
    if isinstance(node, ast.Import):
        return {a.name.split(".")[0] for a in node.names}
    if isinstance(node, ast.ImportFrom):
        if node.level or not node.module:      # a relative import stays inside the package
            return set()
        return {node.module.split(".")[0]}
    return set()


def _imports(path: str, *, lazy: bool = False) -> set:
    """Package names a file imports, found by reading it. Nothing is executed.

    `lazy=False` reports only the imports that run AT IMPORT TIME -- module level, including
    inside a module-level `try` or `if`. `lazy=True` reports the ones inside a function instead.

    The distinction is the whole point rather than a nicety. `fea/sample.py` imports OCP, which
    would look like a flat violation of "numpy and scipy alone" -- and it is six lines inside
    `occupancy_from_shape`, the optional path for sampling a B-rep directly. The module imports
    perfectly well without OCP and only that one function needs it, which is exactly how an
    optional dependency should be arranged. An earlier version of this test walked the whole
    tree and condemned it, which would have pushed the fix in precisely the wrong direction.
    """
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    eager, deferred = set(), set()

    def walk_module(body):
        for node in body:
            eager.update(_names(node))
            if isinstance(node, (ast.If, ast.Try)):
                for attr in ("body", "orelse", "finalbody", "handlers"):
                    for item in getattr(node, attr, []) or []:
                        inner = getattr(item, "body", None)
                        walk_module(inner if inner is not None else [item])

    walk_module(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for inner in ast.walk(node):
                deferred.update(_names(inner))
    return deferred - eager if lazy else eager


# --------------------------------------------------------------------------- the core claim

@test
def test_the_core_modules_import_nothing_but_numpy_scipy_and_the_standard_library():
    """The claim the whole architecture rests on, finally checked.

    The physics tier runs INSIDE FreeCAD, where pip is not available and whatever the host has
    installed is invisible. That is only possible because it needs nothing but what FreeCAD
    already ships, and the moment one module reaches for cadquery the tier stops working in the
    one place it most needs to.
    """
    offenders = {}
    for path in CORE:
        extra = {name for name in _imports(path)
                 if name not in STDLIB
                 and name not in ALLOWED_THIRD_PARTY
                 and name != "constraint_kit"}
        if extra:
            offenders[path] = sorted(extra)
    assert not offenders, (
        "these modules must run on numpy and scipy alone, and do not:\n  "
        + "\n  ".join("%s imports %s" % (p, ", ".join(v)) for p, v in offenders.items()))


@test
def test_the_optimizer_imports_neither_cad_nor_fea():
    """Its tests assert against closed-form optima, which is only honest if it is independent.

    If the optimizer reached into the physics tier, a test could not tell a bad search from a bad
    mesh -- and the suite would need a solver to check an arithmetic question.
    """
    names = _imports("constraint_kit/optimise.py")
    for forbidden in ("cadquery", "OCP", "FreeCAD", "Part"):
        assert forbidden not in names, (forbidden, sorted(names))
    assert "constraint_kit" not in names, (
        "the optimizer must not reach into this package at all; `evaluate` is a callable the "
        "caller supplies: %s" % sorted(names))


@test
def test_an_optional_dependency_is_imported_LAZILY_or_it_is_not_optional():
    """`fea/sample.py` needs OCP for one function, and that is fine if it stays in the function.

    This is the other half of the core claim. "Needs only numpy and scipy" is about what happens
    at import time; a function nobody calls may need whatever it likes. Moving that import to the
    top of the file would break the tier inside FreeCAD while every host test stayed green, which
    is the kind of regression worth a test.
    """
    eager = _imports("constraint_kit/fea/sample.py")
    assert "OCP" not in eager and "cadquery" not in eager, sorted(eager)
    assert "OCP" in _imports("constraint_kit/fea/sample.py", lazy=True), (
        "this test is asserting that OCP is imported lazily; if the OCC path has gone, delete it")


@test
def test_the_manufacturing_rules_do_not_need_scipy_ndimage():
    """Because it does not import inside FreeCAD, and the morphology is pure numpy for that.

    `scipy.ndimage` pulls in `scipy.special`, which raises `All ufuncs must have type
    numpy.ufunc` in FreeCAD 1.0 -- the third distinct failure from the same numpy/scipy ABI
    mismatch. A later refactor reaching back for `ndimage` would break printability inside the
    workbench while every host test stayed green, so it is asserted rather than remembered.

    Checked as an IMPORT rather than as a string: the word appears several times in the module
    docstring, explaining why it is not used, and a test that forbade the word would forbid the
    explanation.
    """
    for lazy in (False, True):
        names = _imports("constraint_kit/manufacture.py", lazy=lazy)
        assert "scipy" not in names, (lazy, sorted(names))


# ------------------------------------------------------------------------ the declared stack

def _pyproject() -> dict:
    try:
        import tomllib
    except ImportError:                        # pragma: no cover - 3.10 and earlier
        import tomli as tomllib                # type: ignore
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)


@test
def test_the_base_install_is_the_core_tier_and_nothing_else():
    """`pip install constraint-kit` must give the tier that needs nothing, not the whole stack.

    This was the real finding behind #24. One dependency list described three environments, so
    the base install pulled cadquery, fastapi, neo4j and langgraph on somebody who wanted only
    the physics tier -- the packaging contradicting the architecture, which is worse than a
    stale pin.
    """
    deps = _pyproject()["project"]["dependencies"]
    names = {d.split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("@")[0].strip()
             for d in deps}
    assert names == {"numpy", "scipy"}, (
        "the base install should be numpy and scipy only; got %s" % sorted(names))


@test
def test_every_heavier_dependency_lives_in_an_extra_that_says_what_it_is_for():
    extras = _pyproject()["project"]["optional-dependencies"]
    for needed in ("kernel", "service", "dev"):
        assert needed in extras, (needed, sorted(extras))

    flat = {name: " ".join(extras[name]) for name in extras}
    assert "cadquery" in flat["kernel"], flat["kernel"]
    assert "fastapi" in flat["service"], flat["service"]
    # and nothing heavy may sneak back into the base list
    base = " ".join(_pyproject()["project"]["dependencies"])
    for heavy in ("cadquery", "build123d", "fastapi", "neo4j", "langgraph", "z3"):
        assert heavy not in base, (heavy, base)


@test
def test_the_pinned_kernel_stack_matches_the_dockerfile_that_was_actually_tested():
    """The pins describe the CONTAINER, which is the only place that stack has ever run.

    113 kernel tests pass in there and nowhere else -- on a bare host the same suite reports
    86/113 with 27 import failures, which is expected rather than a regression. So the pins and
    the Dockerfile have to agree exactly, or the tested stack and the declared one are different
    stacks and the test count means nothing.
    """
    import re

    with open(os.path.join(ROOT, "cadkit/Dockerfile"), encoding="utf-8") as fh:
        docker = fh.read()
    pinned = dict(re.findall(r'"([A-Za-z0-9_.\-]+)==([0-9][^"]*)"', docker))
    assert pinned, "no pinned versions found in the Dockerfile"

    extras = _pyproject()["project"]["optional-dependencies"]
    declared = {}
    for group in extras.values():
        for spec in group:
            if "==" in spec:
                name, _, version = spec.partition("==")
                declared[name.strip()] = version.strip()

    for name, version in pinned.items():
        key = name.replace("_", "-")
        alt = name.replace("-", "_")
        got = declared.get(name) or declared.get(key) or declared.get(alt)
        assert got == version, (
            "the Dockerfile installs %s==%s but pyproject declares %s. The container is the only "
            "place this stack has been tested, so a disagreement means the 113 kernel tests "
            "were run against something other than what is declared."
            % (name, version, got or "nothing"))


@test
def test_requires_python_covers_every_interpreter_this_actually_runs_in():
    """Three interpreters, and FreeCAD's is the oldest and the one that cannot be changed.

    FreeCAD 1.0 bundles Python 3.11.13. The host here runs 3.13. A floor above 3.11 would
    declare the workbench unsupported by its own package metadata.
    """
    spec = _pyproject()["project"]["requires-python"]
    assert spec.startswith(">="), spec
    floor = tuple(int(p) for p in spec.lstrip(">=").strip().split("."))
    assert floor <= (3, 11), (
        "requires-python is %s, but FreeCAD 1.0 bundles Python 3.11.13 and cannot be changed"
        % spec)


@test
def test_the_three_stacks_are_written_down_somewhere_a_reader_will_find_them():
    """The acceptance criterion on #24: the pins match a tested stack, and the docs say which."""
    with open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8") as fh:
        body = fh.read()
    lowered = body.lower()
    assert "three stacks" in lowered or "three interpreters" in lowered, (
        "CLAUDE.md should name the three environments and what is verified in each")
    for marker in ("7.8.1", "7.9.3", "1.26.4"):
        assert marker in body, (
            "the OCCT/numpy versions that make the two-OCCT constraint real should be recorded: "
            "%s is missing" % marker)


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
