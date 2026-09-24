# constraint-kit

Generate **exact parametric CAD assemblies** from natural language. An LLM plans parts + parameters + mates, parametric generators emit B-rep geometry, a deterministic mate kernel assembles them, and the result is rendered + QA'd.

## Features

- **Natural language → CAD**: describe an assembly in plain English, get exact STEP/GLB output
- **Four solver classes**: deterministic tree mates, analytical gear mobility (Willis), geometric loops (SolveSpace), discrete synthesis (Z3/SMT)
- **Real engineering parts**: gears, extrusions, bearings, fasteners, plumbing via build123d/bd_warehouse
- **First-class oriented joints**: named mate frames that survive parameter changes (no selector drift)
- **Spec compiler**: on-demand engineering-fact resolution with provenance (threads, fits, bearings, materials)
- **Hierarchical assemblies**: recursive subassemblies with ports, BOM roll-ups, mass properties
- **Top-down design**: requirements → derived parameters → geometry, with incremental re-solve
- **Interference validation**: bbox prefilter + exact OCC boolean clash detection
- **2D layout → DXF**: deterministic in-plane solve for laser/CNC
- **76 tests**, all geometric ground truth (not VLM-dependent)

## Quick start

The planner and vision QA default to [DeepSeek's API](https://api-docs.deepseek.com/), so you need a key:

```bash
export DEEPSEEK_API_KEY=sk-...
```

### With Docker (recommended)

```bash
docker compose -f cadkit/docker-compose.yaml up -d --build
curl -s http://127.0.0.1:8195/health
```

### Without Docker

```bash
pip install -e ".[all]"
uvicorn constraint_kit.api:app --host 127.0.0.1 --port 8195
```

### First assembly

```python
import httpx

r = httpx.post("http://127.0.0.1:8195/build", json={
    "request": "a plate with a spacer and a 24-tooth gear stacked on its boss"
})
print(r.json())
```

Or via the CLI:

```bash
curl -X POST http://127.0.0.1:8195/build \
  -H "Content-Type: application/json" \
  -d '{"request": "a plate with a spacer and a 24-tooth gear stacked on its boss"}'
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | **Required** for the default DeepSeek backend (`LLM_API_KEY` also works) |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible LLM endpoint for planning + vision QA. Set to a local vLLM / llama.cpp URL (e.g. `http://localhost:8000/v1`) to self-host |
| `MODEL_PLANNER` | `deepseek-v4-pro` | Model that turns requests into assembly specs |
| `MODEL_VLM` | `deepseek-flash` | Vision model for render QA and table extraction (must accept images) |
| `PLANNER_URL` / `VLM_URL` | `LLM_BASE_URL` | Optional per-role endpoint overrides |
| `SPEC_SEARCH_URL` | `http://localhost:8080` | SearXNG instance for spec compiler discovery |
| `CK_WORK_DIR` | `/tmp/constraint-kit` | Working directory for outputs and caches |
| `NEO4J_PASS` | `changeme` | Neo4j password (optional, for atlas persistence) |
| `BLENDER_IMAGE` | `blender:latest` | Docker image for GLB rendering |
| `SPEC_DB_PATH` | `<work_dir>/spec_store.sqlite` | SQLite path for spec compiler cache |
| `SPEC_CACHE_DIR` | `<work_dir>/spec_cache` | JSON artifact dump for spec resolutions |

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  LLM Plan   │────▶│ Parametric Gen   │────▶│  Mate Kernel    │
│  (qwen)     │     │ (cadquery/b123d) │     │ (deterministic) │
└─────────────┘     └──────────────────┘     └────────┬────────┘
                                                       │
                    ┌──────────────────┐     ┌─────────▼────────┐
                    │  Render + QA     │◀────│  Export STEP/GLB │
                    │  (Blender/VLM)   │     │  + Mass + BOM    │
                    └──────────────────┘     └──────────────────┘
```

**Doctrine**: AI for semantics, deterministic geometry for placement. The LLM proposes; exact geometry + the mate kernel dispose.

## API

All endpoints on `127.0.0.1:8195` (host-only by default).

### Core

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check + atlas status |
| `POST` | `/plan` | LLM spec only (no build) |
| `POST` | `/build` | Plan + assemble in one shot |
| `POST` | `/assemble` | Build from a spec |
| `POST` | `/qa` | Attach render + verdict to a stored assembly |

### 2D Layout

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/layout2d` | Plan/solve a 2D layout → DXF + PNG |
| `POST` | `/layout_qa` | Attach a layout verdict |

### DOF Solvers

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/dof/planetary` | Gear-loop mobility (Willis) |
| `POST` | `/dof/four_bar` | 4-bar linkage (SolveSpace) |
| `POST` | `/dof/slider_crank` | Slider-crank (SolveSpace) |
| `POST` | `/mechanism/four_bar` | Pose a 4-bar as an assembly |

### Design

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/design` | Top-down parametric design |
| `POST` | `/design/reedit` | Incremental re-solve (changed requirements) |
| `POST` | `/assembly/tree` | Hierarchical assembly with subassemblies |
| `POST` | `/assembly/bom` | Bill of Materials (md/csv) |
| `POST` | `/assembly/interference` | Clash detection |
| `POST` | `/assembly/explode` | Exploded view |
| `POST` | `/assembly/drawing` | 2D drawing set (DXF + SVG) |

### Intent & DSL

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/intent/design` | NL → intent → build |
| `POST` | `/intent/resolve` | Resolve intent → canonical form |
| `POST` | `/dsl/check` | Static validation of a DSL program |
| `POST` | `/dsl/verify` | Geometric equivalence check |

### Synthesis

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/synthesize/planetary` | SMT gear-train synthesis to a ratio spec |
| `POST` | `/synthesize/tolerance` | SMT tolerance allocation |
| `POST` | `/synthesize/gear_train` | Multi-stage gear synthesis |

### Spec Compiler

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/spec/resolve` | Resolve an engineering fact |
| `POST` | `/spec/validate` | Schema-check a fact |
| `POST` | `/spec/search` | Debug SearXNG discovery |
| `GET` | `/spec/cache` | Recent SQLite resolutions |

### Design Rules

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/rules/fastener_engagement` | Engagement check |
| `POST` | `/rules/fit` | Fit check |
| `POST` | `/rules/clearance` | Min-gap check |
| `POST` | `/rules/beam_bending` | Cantilever stress/deflection |
| `POST` | `/rules/bolt_preload` | Bolt proof load + preload |

## Part vocabulary

**Native parts** (`parts.py`): `plate`, `spur_gear`, `ring_gear`, `planetary_gearset`, `spacer`, `shaft`, `bolt`, `nut`, `washer`, `bearing`, `threaded_rod`, `link`, `adapter`, `housing`, `sheet_bracket`, `panel`, `four_bar`

**Catalog parts** (`parts_bd.py`): `extrusion` (V-slot 2020/2040), `ball_bearing`, `screw` (ISO), `bearing_block`, `sprocket`, `flange`, `pipe`

**Mates**: `coincident`, `rigid`, `contact`, `mesh`, `revolute`, `cylindrical`

## Running tests

```bash
# Inside the cadkit container:
tests/run.sh

# Or directly:
python -m pytest tests/ -v
```

76 tests, all geometric ground truth verified (seating zero-gap, gear center distances, mass vs density, STEP/GLB export, DXF validity, atlas round-trip).

## Project structure

```
constraint_kit/        # the Python package
  parts.py             # native 3D parametric generators
  parts_bd.py          # build123d/bd_warehouse catalog parts
  parts2d.py           # 2D footprint generators
  joints.py            # oriented mate frames
  mate_intent.py       # semantic intent → concrete frames
  mates.py             # deterministic mate kernel
  layout.py            # 2D layout solver + DXF export
  builder.py           # leaf assembly builder
  assembly.py          # hierarchical assemblies + BOM
  parameters.py        # top-down parametric design
  synthesis.py         # SMT design synthesis (Z3)
  spec_compiler.py     # engineering-fact resolution
  api.py               # FastAPI server
  ...
cadkit/                # Dockerfile + docker-compose
drivers/               # orchestration scripts
tests/                 # regression suite
training/              # Gemma 4 fine-tuning
tools/                 # utilities
libraries/             # reusable assembly defs
```

## License

MIT
