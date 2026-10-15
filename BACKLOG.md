# constraint-kit backlog

The backlog lives in **atlas (Neo4j)** as a queryable graph; this file is the human index + the working
protocol. The graph is reproducible from version control via `tools/seed_backlog.py` (idempotent MERGE),
so git is the source of truth for the *structure* and atlas holds *live status*.

## Working protocol (enforced per task — `CkPolicy {id:'protocol'}`)
1. **Plan with sequential-thinking** before acting on a task.
2. **Update the task's atlas node**: `status` backlog → todo → in_progress → done, with notes.
3. **On a blocker**: create/link a `CkResearch` item, set the task `status=blocked`, and **move on to
   another task** (don't stall the queue).
Always: deterministic geometry verified by **tests, not the VLM**; provenance on every value; fail-soft.

## Graph model (namespaced `Ck*`, `project="constraint-kit"`)
```
(:CkBacklog)-[:HAS_EPIC]->(:CkEpic)-[:HAS_TASK]->(:CkTask {status, requires, detail})
                                   -[:HAS_RESEARCH]->(:CkResearch {question, status:open})
(:CkTask)-[:DEPENDS_ON]->(:CkTask)   (:CkTask)-[:BLOCKED_BY]->(:CkResearch)
(:CkBacklog)-[:GOVERNED_BY]->(:CkPolicy)
```
Every `CkTask.requires = "sequential-thinking + atlas-update"`.

## Epics (9)
| id | epic | thrust |
|---|---|---|
| E1 | geometry-richness | fillets/shells/sweeps/sheet-metal + real housings (parts aren't primitives) |
| E2 | interface-grounding | derive stable named ports/mate-frames from geometry+intent (stop hand-authoring) |
| E3 | scale-and-perf | spatial-index interference, build cache, parallel subtrees → thousands of parts |
| E4 | engineering-depth | CG/inertia, tolerance stack-up, material/load checks ("will it work") |
| E5 | spec-breadth | grow spec-compiler kinds (full ISO 286 interference, sections, codes) + reliable extraction |
| E6 | domain-libraries | reusable automotive/architectural assembly-def packages |
| E7 | manufacturing-outputs | GD&T drawings, exploded views, CAM exports, BOM documents |
| E8 | lifecycle-and-packaging | parametric edit/re-solve, versioning, CI, subpackage reorg |
| E9 | rule-validation | standards/code/design-rule checks fed by the spec compiler |

> **Ordering now lives in [`PLAN.md`](PLAN.md), and the live queue is
> [GitHub issues](../../issues), milestoned by phase.** This table is the epic map, not the work list.

31 tasks, 13 research items (see atlas / `tools/seed_backlog.py`).

**✅ done:** T3.2 spatial interference prefilter · T8.4 CI runner · T1.1 fillet/chamfer finish ·
T2.1 geometric port-derivation · T4.1 CG+inertia roll-up · T7.3 BOM document export · T1.5 parametric
housing part · T6.1 library loader · build cache.

*Those last five used to be listed here as "suggested next" long after they were finished and covered by
passing tests (`test_mass_properties_cg_and_inertia`, `test_bom_document`, `test_housing_part`,
`test_library_loader`, `test_build_cache`). A stale backlog is the document you read to decide what to
build, so it eventually makes someone rebuild something that already works.*

**Not in any epic above, and the smallest high-value item in the plan:** STEP/IGES **import** — the kernel
has export only, so it cannot see a single part that already exists ([issue #10](../../issues/10)).

Query `status='todo'` for the atlas queue; blocked items wait on their `BLOCKED_BY` research.

## CI / hooks
`tools/ci.sh` runs the suite (exit 0 = green). Enable the pre-push gate once: `git config core.hooksPath .githooks` (bypass with `git push --no-verify`).

## Use
```bash
# seed / refresh the backlog graph (idempotent)
docker exec cadkit python3 tools/seed_backlog.py

# what's ready to pick up
docker exec n4j_atlas cypher-shell -u neo4j -p $NEO4J_PASS \
  "MATCH (t:CkTask {status:'todo'}) RETURN t.id, t.title, t.detail"

# what's blocked and why
docker exec n4j_atlas cypher-shell -u neo4j -p $NEO4J_PASS \
  "MATCH (t:CkTask)-[:BLOCKED_BY]->(r:CkResearch) RETURN t.id, r.question"

# an epic's tasks
docker exec n4j_atlas cypher-shell -u neo4j -p $NEO4J_PASS \
  "MATCH (:CkEpic {id:'E1'})-[:HAS_TASK]->(t) RETURN t.id, t.status, t.title ORDER BY t.id"
```
Long-term context: `ROADMAP.md` (screw→car→home). How the built parts work: `CLAUDE.md`.
