"""constraint-kit — parametric-CAD assembly generator.

Pipeline (per README.md): LLM plans parts+params+mates -> parametric generators emit exact
CAD geometry -> deterministic mate kernel assembles -> render+VLM/measurement QA. The GPU-heavy
planner/VLM are REUSED from the resident model server; this package is CPU-only geometry.

Phase 0 uses a single coherent cadquery + cq_gears stack (one OCC build, native involute gears,
GLB/STEP export) to de-risk the first end-to-end run. build123d Joints come in Phase 2.
"""
# Submodules import cadquery (container-only); import them explicitly where needed rather than
# eagerly here, so host-side tooling can import the package without the CAD stack present.

