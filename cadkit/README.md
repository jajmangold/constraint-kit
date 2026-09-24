# cadkit — constraint-kit's CPU geometry + planning service

Phase 0 of constraint-kit (see `../README.md` for the thesis). A FastAPI service that turns a
natural-language request into an exact parametric CAD assembly, calling a hosted LLM for
planning (DeepSeek by default, or any OpenAI-compatible server) — the service itself uses no GPU.

## Stack
cadquery 2.7 + cadquery-ocp (OpenCASCADE) + cq_gears (involute gears) + build123d 0.10 + bd_warehouse 0.2
(catalog parts: extrusions/fasteners/bearings) + ezdxf + trimesh + matplotlib, on `python:3.12-slim`.
build123d co-resolves with cadquery on the SAME cadquery-ocp (no conflict, no second image); bd_warehouse
parts convert via the shared TopoDS. Code is bind-mounted from `../constraint_kit/`, so edits need only a
restart.

## Run
```bash
cd ${CK_WORK_DIR:-/tmp/constraint-kit}
HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose -f cadkit/docker-compose.yaml up -d --build
curl -s http://127.0.0.1:8195/health
```
`network_mode: host` lets cadkit reach optional local services bound to `127.0.0.1` (a local LLM server,
Neo4j, SearXNG).

## API (`127.0.0.1:8195`)
- `GET  /health`
- `POST /plan      {"request": "..."}`            → LLM assembly spec only (qwen27b, schema-enforced)
- `POST /assemble  {"spec": {...}, "name": "..."}` → build + export from an explicit spec
- `POST /build     {"request": "...","name":"..."}`→ plan THEN assemble + export (one shot)

Outputs (`.step`, `.glb`) land in `cadkit/output/` (the shared host path, valid on host for rendering).
Response includes per-part `mass_g`/`volume_mm3`, assembly `bbox`, and the artifact paths.

## End-to-end (plan → build → render → VLM QA)
```bash
python3 drivers/phase0.py "a 60mm aluminum plate with a centered boss and a 20-tooth module-1 steel gear seated on it"
```
Renders via the reused Blender image and asks qwen27b to confirm seating.

## Part vocabulary (Phase 0)
- `plate(width,depth,thick,boss_d,boss_h,bolt_d,bolt_circle,bolt_count)` — anchors `mount`,`base`,`boss_axis`
- `spur_gear(module,teeth,width,bore_d,pressure_angle)` — anchors `bore_base`,`bore_top`; falls back to a
  bored cylinder blank if cq_gears errors

Mate: `coincident` (lands B's anchor frame on A's). Extend parts in `constraint_kit/parts.py::PART_GENS`
and mates in `constraint_kit/mates.py`.
