# cadkit -- CPU-only parametric-CAD + planning service for constraint-kit.
# Single coherent OCC stack: cadquery + cq_gears (native involute gears) + glb/step export.
# Code is bind-mounted at runtime (see docker-compose.yaml) so edits need only a restart, not a rebuild.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglu1-mesa libxrender1 libxext6 libsm6 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# cadquery pulls cadquery-ocp (OpenCASCADE) as a manylinux wheel -> no source compilation on CPU.
# cq_gears is GitHub-only (involute gear generator, deps cadquery+numpy).
# build123d + bd_warehouse: co-resolve with cadquery on the SAME cadquery-ocp 7.8.1.1 (verified, no
# conflict) -> real threaded fasteners, aluminum extrusions, pipe/flange plumbing, ball bearings.
RUN pip install --no-cache-dir \
        cadquery \
        build123d \
        bd_warehouse \
        "cq_gears @ git+https://github.com/meadiode/cq_gears.git" \
        numpy \
        trimesh \
        ezdxf \
        "fastapi" \
        "uvicorn[standard]" \
        httpx \
        pydantic \
        neo4j \
        langgraph \
        beautifulsoup4 \
        pypdf \
        pdf_oxide \
        z3-solver

# python-solvespace = the geometric closed-loop DOF solver (planar mechanisms: 4-bar, slider-crank).
# It's a C++/Cython extension with no wheel -> compile it, then purge the toolchain to keep the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir python-solvespace \
    && apt-get purge -y --auto-remove build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/nvme-data/containers/constraint-kit
EXPOSE 8195
CMD ["uvicorn", "constraint_kit.api:app", "--host", "127.0.0.1", "--port", "8195"]
