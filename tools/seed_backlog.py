#!/usr/bin/env python3
"""Seed the constraint-kit work backlog into atlas (Neo4j) — idempotent (MERGE), reproducible from git.

Graph: (:CkBacklog)-[:HAS_EPIC]->(:CkEpic)-[:HAS_TASK]->(:CkTask)
                                          -[:HAS_RESEARCH]->(:CkResearch)
       (:CkTask)-[:DEPENDS_ON]->(:CkTask) · (:CkTask)-[:BLOCKED_BY]->(:CkResearch)
       (:CkBacklog)-[:GOVERNED_BY]->(:CkPolicy)
All nodes namespaced Ck* + project="constraint-kit". Run:  docker exec cadkit python3 tools/seed_backlog.py
Query:  docker exec n4j_atlas cypher-shell -u neo4j -p $NEO4J_PASS \\
          "MATCH (e:CkEpic)-[:HAS_TASK]->(t:CkTask) RETURN e.id,t.id,t.status ORDER BY t.id"
"""
from __future__ import annotations

import os

PROJECT = "constraint-kit"
REQUIRES = "sequential-thinking + atlas-update"   # working protocol enforced on every task

POLICY = ("Per task: (1) run sequential-thinking to plan before acting; (2) update the task's atlas node "
          "status backlog->todo->in_progress->done with notes; (3) on a blocker, create/link a CkResearch "
          "item, set status=blocked, and move on to another task. Always: deterministic geometry verified "
          "by tests (not the VLM), provenance on every value, fail-soft.")

EPICS = [
    ("E1", "geometry-richness", "Parts are primitives; need fillets/shells/sweeps/sheet-metal + real housings."),
    ("E2", "interface-grounding", "Ports/mates are hand-authored; derive stable named frames from geometry+intent."),
    ("E3", "scale-and-perf", "O(n^2) interference, slow threads, no cache — won't reach thousands of parts."),
    ("E4", "engineering-depth", "Beyond geometry: CG/inertia, tolerance stack-up, material/load checks ('will it work')."),
    ("E5", "spec-breadth", "Spec compiler is thin; grow kinds (full ISO286 interference, sections, codes) + reliable extraction."),
    ("E6", "domain-libraries", "Reusable automotive/architectural assembly-def packages: compose, don't model from scratch."),
    ("E7", "manufacturing-outputs", "No GD&T drawings, exploded views, CAM-ready exports, BOM documents."),
    ("E8", "lifecycle-and-packaging", "No parametric edit/re-solve, versioning, CI, or subpackage structure."),
    ("E9", "rule-validation", "Phase C second half: standards/code/design-rule checks fed by the spec compiler."),
]

# (id, epic, title, detail, status)
TASKS = [
    ("T1.1", "E1", "fillet/chamfer post-op", "deterministic edge-selected fillet/chamfer in the builder pipeline; tests on edge count/volume", "todo"),
    ("T1.2", "E1", "shell/hollow op", "wall-thickness shell for housings/enclosures", "backlog"),
    ("T1.3", "E1", "sweep/loft profile part", "build123d BuildLine->sweep/loft generator for non-prismatic parts", "backlog"),
    ("T1.4", "E1", "sheet-metal primitive", "flange/bend sheet-metal part", "blocked"),
    ("T1.5", "E1", "parametric housing/enclosure part", "box+bore+bosses+mount-flange+fillets — first 'rich' part", "backlog"),
    ("T2.1", "E2", "geometric port-derivation", "derive bore-axis / largest-planar-face / bolt-circle frames from a built solid -> named oriented ports", "todo"),
    ("T2.2", "E2", "mate-by-intent resolver", "map semantic intent ('seat gear bore on plate boss') to derived frames + mate type", "backlog"),
    ("T2.3", "E2", "derived-port stability tests", "derived ports survive param changes (generalize the joint-name-stability test)", "backlog"),
    ("T3.1", "E3", "content-hash build cache", "skip rebuilding unchanged parts/subtrees keyed by spec hash", "backlog"),
    ("T3.2", "E3", "spatial-bucket interference prefilter", "replace O(n^2) AABB with uniform grid/BVH neighbour query", "todo"),
    ("T3.3", "E3", "parallel subtree builds", "process/thread pool for CPU-bound OCC builds", "blocked"),
    ("T3.4", "E3", "scale benchmark harness", "build 100->1000-part assembly; record build/interference timings to atlas", "backlog"),
    ("T4.1", "E4", "CG + inertia roll-up", "OCC mass props per part -> assembly centre-of-gravity + inertia tensor", "backlog"),
    ("T4.2", "E4", "tolerance stack-up", "chain toleranced dims (spec-compiler fits) -> worst-case/RSS clearance at an interface", "backlog"),
    ("T4.3", "E4", "material strength check", "material kind gains yield/E; a simple bolt-preload or beam-bending check", "blocked"),
    ("T5.1", "E5", "ISO286 p..zc interference table", "exact interference-fit fundamental deviations (currently class-only)", "blocked"),
    ("T5.2", "E5", "structural-sections kind", "I-beam/angle/channel section dimensions in the spec compiler", "backlog"),
    ("T5.3", "E5", "robust live extraction", "harden spec_extract table/VLM parsing + confidence gate; offline fixtures", "blocked"),
    ("T6.1", "E6", "library loader/registry", "assembly defs as versioned packages under libraries/ + a registry", "backlog"),
    ("T6.2", "E6", "automotive starter defs", "wheel/hub/axle/diff/gearbox/suspension-linkage as reusable defs", "backlog"),
    ("T6.3", "E6", "architectural starter defs", "stud/wall/opening/floor/room defs (2D layout + extrude)", "blocked"),
    ("T7.1", "E7", "exploded-view generator", "offset children along ports for assembly docs/renders", "backlog"),
    ("T7.2", "E7", "2D drawing / GD&T export", "section + dimensioned drawings on the cadquery/OCC stack", "blocked"),
    ("T7.3", "E7", "BOM document export", "CSV/MD BOM with mass + source_refs from a built tree", "backlog"),
    ("T8.1", "E8", "design versioning in atlas", "CkDesignVersion linked to params + git SHA", "backlog"),
    ("T8.2", "E8", "parametric edit/re-solve API", "change a requirement -> rebuild only affected subtree (uses T3.1)", "backlog"),
    ("T8.3", "E8", "package reorg into subpackages", "solvers/ spec/ assembly/ parts/ io/ — test-guarded; deferred until interfaces settle", "backlog"),
    ("T8.4", "E8", "CI test runner", "run tests/run.sh on commit (git hook or simple script)", "todo"),
    ("T9.1", "E9", "fastener-engagement rule", "thread engagement length >= spec, using spec-compiler facts", "backlog"),
    ("T9.2", "E9", "fit-appropriateness rule", "e.g. bearing seat should be a specific fit class (iso286 + spec)", "backlog"),
    ("T9.3", "E9", "clearance rules", "min gap between moving parts (interference + clearance margin)", "backlog"),
]

# (id, epic, question, why)
RESEARCH = [
    ("R1.a", "E1", "Durable edge identification for fillets across regen", "selector fragility = the topo-naming problem for edges"),
    ("R1.b", "E1", "Best build123d/cadquery sheet-metal path", "unclear which lib gives clean flange/bend ops"),
    ("R2.a", "E2", "Topological-naming-resistant feature IDs", "persistent edge/face IDs across parametric regen — the core hard problem"),
    ("R2.b", "E2", "LLM intent -> frame mapping schema", "how to specify mate intent so it resolves to derived frames"),
    ("R3.a", "E3", "OCC/cadquery thread/process safety for parallel builds", "needed before parallelizing subtree builds"),
    ("R3.b", "E3", "Incremental re-export of only changed subtrees", "avoid re-exporting the whole tree on a small edit"),
    ("R4.a", "E4", "Minimal credible static mechanical checks without full FEA", "what checks are honest at this fidelity"),
    ("R4.b", "E4", "Tolerance method: worst-case vs RSS default", "which to default to and when"),
    ("R5.a", "E5", "Source the ISO 286-2 p..zc fundamental-deviation table (license-clean)", "the known blocker for exact interference fits"),
    ("R5.b", "E5", "Robust engineering-table extraction eval set", "to measure/harden HTML/PDF/VLM extraction reliability"),
    ("R6.a", "E6", "Architectural/BIM minimal data model (walls+openings+MEP)", "how to represent buildings in this kernel"),
    ("R7.a", "E7", "GD&T/drawing generation path on the cadquery/OCC stack", "feasibility without FreeCAD"),
    ("R9.a", "E9", "Catalog of automatable mechanical/code design rules + data sources", "what rules to encode and where the facts come from"),
]

DEPENDS_ON = [("T2.2", "T2.1"), ("T3.4", "T3.2"), ("T8.2", "T3.1")]
BLOCKED_BY = [("T1.4", "R1.b"), ("T3.3", "R3.a"), ("T4.3", "R4.a"), ("T5.1", "R5.a"),
              ("T5.3", "R5.b"), ("T6.3", "R6.a"), ("T7.2", "R7.a")]


def main():
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(os.environ.get("NEO4J_URL", "bolt://127.0.0.1:7687"),
                               auth=(os.environ.get("NEO4J_USER", "neo4j"),
                                     os.environ.get("NEO4J_PASS", "")))
    drv.verify_connectivity()
    with drv.session() as s:
        s.run("MERGE (b:CkBacklog {id:'backlog'}) SET b.project=$p, b.updated=timestamp()", p=PROJECT)
        s.run("MERGE (pol:CkPolicy {id:'protocol'}) SET pol.project=$p, pol.rule=$r", p=PROJECT, r=POLICY)
        s.run("MATCH (b:CkBacklog {id:'backlog'}),(pol:CkPolicy {id:'protocol'}) MERGE (b)-[:GOVERNED_BY]->(pol)")
        for eid, title, why in EPICS:
            s.run("MERGE (e:CkEpic {id:$id}) SET e.project=$p, e.title=$t, e.why=$w, e.status='active' "
                  "WITH e MATCH (b:CkBacklog {id:'backlog'}) MERGE (b)-[:HAS_EPIC]->(e)",
                  id=eid, p=PROJECT, t=title, w=why)
        for tid, eid, title, detail, status in TASKS:
            s.run("MERGE (t:CkTask {id:$id}) SET t.project=$p, t.title=$ti, t.detail=$d, t.status=$s, "
                  "t.epic=$e, t.requires=$req "
                  "WITH t MATCH (e:CkEpic {id:$e}) MERGE (e)-[:HAS_TASK]->(t)",
                  id=tid, p=PROJECT, ti=title, d=detail, s=status, e=eid, req=REQUIRES)
        for rid, eid, q, why in RESEARCH:
            s.run("MERGE (r:CkResearch {id:$id}) SET r.project=$p, r.question=$q, r.why=$w, r.status='open' "
                  "WITH r MATCH (e:CkEpic {id:$e}) MERGE (e)-[:HAS_RESEARCH]->(r)",
                  id=rid, p=PROJECT, q=q, w=why, e=eid)
        for a, b in DEPENDS_ON:
            s.run("MATCH (x:CkTask {id:$a}),(y:CkTask {id:$b}) MERGE (x)-[:DEPENDS_ON]->(y)", a=a, b=b)
        for t, r in BLOCKED_BY:
            s.run("MATCH (x:CkTask {id:$t}),(y:CkResearch {id:$r}) MERGE (x)-[:BLOCKED_BY]->(y)", t=t, r=r)
        n = s.run("MATCH (n) WHERE n.project=$p AND any(l IN labels(n) WHERE l STARTS WITH 'Ck') "
                  "AND (n:CkEpic OR n:CkTask OR n:CkResearch) RETURN labels(n)[0] AS l, count(*) AS c "
                  "ORDER BY l", p=PROJECT).data()
    drv.close()
    print("seeded backlog:", {r["l"]: r["c"] for r in n})


if __name__ == "__main__":
    main()
