"""Persist all constraint-kit work to atlas (an optional Neo4j at bolt 127.0.0.1:7687; fail-soft).

Graph model (all nodes namespaced with `Ck*` labels + project="constraint-kit" so we never collide
with other tenants of the shared DB):

  (:CkPartType {name})
  (:CkRun   {id, request, planner_model, created})
  (:CkAssembly {name, total_mass_g, bbox_size, glb, step, glb_method, render_png, vlm_verdict, created})
  (:CkPart  {uid, pid, type, material, volume_mm3, mass_g, fallback})

  (:CkRun)-[:PRODUCED]->(:CkAssembly)
  (:CkAssembly)-[:HAS_PART]->(:CkPart)
  (:CkPart)-[:OF_TYPE]->(:CkPartType)
  (:CkPart)-[:MATE {type, a_joint, b_joint, seq}]->(:CkPart)     # a -> b, ordered

Fail-soft by design: if the neo4j driver is missing or the DB is unreachable, every call is a no-op
(logged once) so geometry/builds never break because of storage. `enabled()` reports live status.
"""
from __future__ import annotations

import os
import time

_PROJECT = "constraint-kit"
_driver = None
_status = "uninitialised"


def _connect():
    global _driver, _status
    if _driver is not None:
        return _driver
    url = os.environ.get("NEO4J_URL", "bolt://127.0.0.1:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pw = os.environ.get("NEO4J_PASS", "")
    try:
        from neo4j import GraphDatabase
        d = GraphDatabase.driver(url, auth=(user, pw))
        d.verify_connectivity()
        _driver = d
        _status = f"connected:{url}"
        _ensure_schema(d)
    except Exception as exc:  # noqa: BLE001 -- storage is best-effort
        _status = f"disabled:{type(exc).__name__}:{exc}"
        _driver = None
    return _driver


def _ensure_schema(d) -> None:
    stmts = [
        "CREATE CONSTRAINT ck_run_id IF NOT EXISTS FOR (r:CkRun) REQUIRE r.id IS UNIQUE",
        "CREATE CONSTRAINT ck_asm_name IF NOT EXISTS FOR (a:CkAssembly) REQUIRE a.name IS UNIQUE",
        "CREATE CONSTRAINT ck_part_uid IF NOT EXISTS FOR (p:CkPart) REQUIRE p.uid IS UNIQUE",
        "CREATE CONSTRAINT ck_pt_name IF NOT EXISTS FOR (t:CkPartType) REQUIRE t.name IS UNIQUE",
    ]
    with d.session() as s:
        for q in stmts:
            s.run(q)


def enabled() -> bool:
    return _connect() is not None


def status() -> str:
    _connect()
    return _status


def record_build(name: str, request: str | None, spec: dict, parts: list[dict],
                 exported: dict, planner_model: str | None) -> bool:
    """Upsert a full build (run + assembly + parts + mates) into atlas. Idempotent on `name`."""
    d = _connect()
    if d is None:
        return False
    run_id = f"run_{name}_{int(time.time())}"
    total_mass = round(sum(p.get("mass_g", 0) for p in parts), 2)
    bbox_size = exported.get("bbox", {}).get("size")
    with d.session() as s:
        s.execute_write(_tx_record, _PROJECT, run_id, name, request, planner_model,
                        total_mass, bbox_size, exported, parts, spec.get("mates", []))
    return True


def _tx_record(tx, project, run_id, name, request, planner_model, total_mass, bbox_size,
               exported, parts, mates):
    tx.run(
        """
        MERGE (a:CkAssembly {name:$name})
          SET a.project=$project, a.total_mass_g=$total_mass, a.bbox_size=$bbox_size,
              a.glb=$glb, a.step=$step, a.glb_method=$glb_method, a.updated=timestamp()
        MERGE (r:CkRun {id:$run_id})
          SET r.project=$project, r.request=$request, r.planner_model=$planner_model,
              r.created=timestamp()
        MERGE (r)-[:PRODUCED]->(a)
        """,
        project=project, name=name, total_mass=total_mass, bbox_size=bbox_size,
        glb=exported.get("glb"), step=exported.get("step"),
        glb_method=exported.get("glb_method"), run_id=run_id, request=request,
        planner_model=planner_model,
    )
    # detach old part graph for this assembly so re-builds stay consistent
    tx.run("MATCH (a:CkAssembly {name:$name})-[:HAS_PART]->(p:CkPart) DETACH DELETE p", name=name)
    for p in parts:
        uid = f"{name}:{p['id']}"
        tx.run(
            """
            MATCH (a:CkAssembly {name:$name})
            MERGE (t:CkPartType {name:$ptype}) SET t.project=$project
            CREATE (p:CkPart {uid:$uid})
              SET p.project=$project, p.pid=$pid, p.type=$ptype, p.material=$material,
                  p.volume_mm3=$vol, p.mass_g=$mass, p.fallback=$fallback
            MERGE (a)-[:HAS_PART]->(p)
            MERGE (p)-[:OF_TYPE]->(t)
            """,
            project=project, name=name, uid=uid, pid=p["id"], ptype=p["type"],
            material=p.get("material"), vol=p.get("volume_mm3"), mass=p.get("mass_g"),
            fallback=bool(p.get("gear_fallback")),
        )
    for seq, m in enumerate(mates):
        a_id = m.get("a"); b_id = m.get("b")
        tx.run(
            """
            MATCH (pa:CkPart {uid:$ua}), (pb:CkPart {uid:$ub})
            MERGE (pa)-[r:MATE {seq:$seq}]->(pb)
              SET r.type=$type, r.a_joint=$aj, r.b_joint=$bj
            """,
            ua=f"{name}:{a_id}", ub=f"{name}:{b_id}", seq=seq,
            type=m.get("type"), aj=m.get("a_joint"), bj=m.get("b_joint"),
        )


def attach_render(name: str, png: str | None, verdict: str | None) -> bool:
    d = _connect()
    if d is None:
        return False
    with d.session() as s:
        s.run("MATCH (a:CkAssembly {name:$name}) SET a.render_png=$png, a.vlm_verdict=$verdict",
              name=name, png=png, verdict=verdict)
    return True


def recent_runs(limit: int = 20) -> list[dict]:
    d = _connect()
    if d is None:
        return []
    with d.session() as s:
        rows = s.run(
            """
            MATCH (r:CkRun)-[:PRODUCED]->(a:CkAssembly)
            RETURN r.id AS run, r.request AS request, a.name AS assembly,
                   a.total_mass_g AS mass_g, a.bbox_size AS bbox, a.vlm_verdict AS verdict,
                   r.created AS created
            ORDER BY r.created DESC LIMIT $limit
            """, limit=limit).data()
    return rows


def assembly(name: str) -> dict | None:
    d = _connect()
    if d is None:
        return None
    with d.session() as s:
        a = s.run("MATCH (a:CkAssembly {name:$name}) RETURN a", name=name).data()
        if not a:
            return None
        parts = s.run(
            "MATCH (a:CkAssembly {name:$name})-[:HAS_PART]->(p:CkPart) RETURN p ORDER BY p.pid",
            name=name).data()
        mates = s.run(
            """MATCH (a:CkAssembly {name:$name})-[:HAS_PART]->(pa)-[m:MATE]->(pb)
               RETURN pa.pid AS a, pb.pid AS b, m.type AS type, m.a_joint AS a_joint,
                      m.b_joint AS b_joint ORDER BY m.seq""", name=name).data()
    return {"assembly": a[0]["a"], "parts": [p["p"] for p in parts], "mates": mates}


def record_layout(name: str, request: str | None, spec: dict, rep: dict,
                  dxf: str, png: str, planner_model: str | None) -> bool:
    """Persist a 2D layout (run + layout + 2D parts + ordered mates) into atlas."""
    d = _connect()
    if d is None:
        return False
    run_id = f"layoutrun_{name}_{int(time.time())}"
    with d.session() as s:
        s.execute_write(_tx_layout, _PROJECT, run_id, name, request, planner_model,
                        rep, dxf, png, spec.get("mates", []))
    return True


def _tx_layout(tx, project, run_id, name, request, planner_model, rep, dxf, png, mates):
    tx.run(
        """
        MERGE (l:CkLayout {name:$name})
          SET l.project=$project, l.sheet_size=$sheet_size, l.dxf=$dxf, l.png=$png,
              l.updated=timestamp()
        MERGE (r:CkRun {id:$run_id})
          SET r.project=$project, r.request=$request, r.planner_model=$planner_model,
              r.created=timestamp(), r.kind='layout2d'
        MERGE (r)-[:PRODUCED]->(l)
        """,
        project=project, name=name, sheet_size=rep.get("sheet_size"), dxf=dxf, png=png,
        run_id=run_id, request=request, planner_model=planner_model,
    )
    tx.run("MATCH (l:CkLayout {name:$name})-[:HAS_PART]->(p:CkPart2D) DETACH DELETE p", name=name)
    for p in rep.get("parts", []):
        uid = f"{name}:{p['id']}"
        tx.run(
            """
            MATCH (l:CkLayout {name:$name})
            MERGE (t:CkPartType {name:$ptype}) SET t.project=$project
            CREATE (p:CkPart2D {uid:$uid})
              SET p.project=$project, p.pid=$pid, p.type=$ptype, p.material=$material,
                  p.place=$place, p.rotate_deg=$rot
            MERGE (l)-[:HAS_PART]->(p)
            MERGE (p)-[:OF_TYPE]->(t)
            """,
            project=project, name=name, uid=uid, pid=p["id"], ptype=p["type"],
            material=p.get("material"), place=p.get("place"), rot=p.get("rotate_deg"),
        )
    for seq, m in enumerate(mates):
        tx.run(
            """
            MATCH (pa:CkPart2D {uid:$ua}), (pb:CkPart2D {uid:$ub})
            MERGE (pa)-[r:MATE {seq:$seq}]->(pb)
              SET r.type='coincident', r.a_joint=$aj, r.b_joint=$bj, r.offset=$off
            """,
            ua=f"{name}:{m.get('a')}", ub=f"{name}:{m.get('b')}", seq=seq,
            aj=m.get("a_joint"), bj=m.get("b_joint"), off=m.get("offset"),
        )


def attach_layout_verdict(name: str, verdict: str | None) -> bool:
    d = _connect()
    if d is None:
        return False
    with d.session() as s:
        s.run("MATCH (l:CkLayout {name:$name}) SET l.vlm_verdict=$v", name=name, v=verdict)
    return True


def recent_layouts(limit: int = 20) -> list[dict]:
    d = _connect()
    if d is None:
        return []
    with d.session() as s:
        return s.run(
            """MATCH (r:CkRun {kind:'layout2d'})-[:PRODUCED]->(l:CkLayout)
               RETURN l.name AS layout, r.request AS request, l.sheet_size AS sheet_size,
                      l.dxf AS dxf, l.vlm_verdict AS verdict, r.created AS created
               ORDER BY r.created DESC LIMIT $limit""", limit=limit).data()


def record_assembly_tree(res: dict) -> bool:
    """Work-tracking breadcrumb for a hierarchical assembly build (rolled-up BOM/mass/depth, not the
    full instance graph). Fail-soft."""
    import json
    d = _connect()
    if d is None:
        return False
    with d.session() as s:
        s.run(
            """MERGE (a:CkAssemblyTree {name:$name})
               SET a.project=$project, a.mass_g=$mass, a.part_count=$pc, a.depth=$depth,
                   a.bom=$bom, a.step=$step, a.glb=$glb, a.cg=$cg, a.principal_moments=$pm,
                   a.created=timestamp()""",
            name=res.get("name", "tree"), project=_PROJECT, mass=res.get("mass_g"),
            pc=res.get("part_count"), depth=res.get("depth"),
            bom=json.dumps(res.get("bom", {})), step=res.get("step"), glb=res.get("glb"),
            cg=json.dumps(res.get("cg")), pm=json.dumps(res.get("principal_moments")))
    return True


def _git_sha() -> str | None:
    """Current repo commit, for design provenance (T8.1). Prefers env CK_GIT_SHA (set at container start,
    since only the package — not .git — is bind-mounted), else tries `git rev-parse`. Fail-soft -> None."""
    sha = os.environ.get("CK_GIT_SHA")
    if sha:
        return sha.strip()
    try:
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 -- provenance is best-effort
        return None


def design_version_id(name: str, values: dict) -> str:
    """Stable version id for a design: name + a content hash of its resolved parameter VALUES. Identical
    parameters dedupe to the same version (idempotent); any parameter edit yields a new version id."""
    import hashlib
    import json
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"dv_{name}_{digest}"


def record_design_version(res: dict, git_sha: str | None = None) -> str | None:
    """Persist a CkDesignVersion (T8.1): the resolved parameter table + git SHA, linked from the design's
    CkAssemblyTree by a HAS_VERSION edge. The version id hashes the parameter VALUES, so re-recording the
    same design is idempotent and a parametric edit creates a distinct, linked version. Returns the version
    id (or None if storage is disabled). Fail-soft."""
    import json
    params = res.get("parameters") or {}
    values = {k: v.get("value") if isinstance(v, dict) else v for k, v in params.items()}
    name = res.get("name", "design")
    vid = design_version_id(name, values)
    d = _connect()
    if d is None:
        return None
    sha = git_sha if git_sha is not None else _git_sha()
    with d.session() as s:
        s.run(
            """MERGE (a:CkAssemblyTree {name:$name}) SET a.project=$project
               MERGE (v:CkDesignVersion {id:$vid})
                 SET v.project=$project, v.name=$name, v.git_sha=$sha, v.params=$params,
                     v.mass_g=$mass, v.part_count=$pc, v.created=timestamp()
               MERGE (a)-[:HAS_VERSION]->(v)""",
            name=name, project=_PROJECT, vid=vid, sha=sha, params=json.dumps(values),
            mass=res.get("mass_g"), pc=res.get("part_count"))
    return vid


def design_versions(name: str, limit: int = 20) -> list[dict]:
    """All recorded versions of a design (most recent first). Fail-soft -> []."""
    d = _connect()
    if d is None:
        return []
    with d.session() as s:
        return s.run(
            """MATCH (a:CkAssemblyTree {name:$name})-[:HAS_VERSION]->(v:CkDesignVersion)
               RETURN v.id AS id, v.git_sha AS git_sha, v.params AS params, v.mass_g AS mass_g,
                      v.part_count AS part_count, v.created AS created
               ORDER BY v.created DESC LIMIT $limit""", name=name, limit=int(limit)).data()


def record_benchmark(b: dict) -> bool:
    """Record a scale-benchmark run (T3.4): part count + build/interference timings + pair pruning.
    Fail-soft."""
    d = _connect()
    if d is None:
        return False
    with d.session() as s:
        s.run(
            """CREATE (b:CkBenchmark {project:$project, n_parts:$n, build_s:$bs, interference_s:$isec,
                   pairs_total:$pt, pairs_candidate:$pc, created:timestamp()})""",
            project=_PROJECT, n=b.get("n_parts"), bs=b.get("build_s"),
            isec=b.get("interference_s"), pt=b.get("pairs_total"), pc=b.get("pairs_candidate"))
    return True


def record_spec_resolution(result: dict) -> bool:
    """Work-tracking breadcrumb ONLY (the spec DATA lives in SQLite, never atlas). Records that a spec
    resolution happened: query/kind/ok/cache_hit/fact count + top confidence. Fail-soft."""
    d = _connect()
    if d is None:
        return False
    facts = result.get("facts", [])
    top_conf = max((f.get("confidence", 0.0) for f in facts), default=0.0)
    with d.session() as s:
        s.run(
            """MERGE (r:CkSpecResolution {id:$id})
               SET r.project=$project, r.query=$q, r.kind=$kind, r.ok=$ok,
                   r.cache_hit=$cache_hit, r.n_facts=$n_facts, r.top_confidence=$conf,
                   r.designation=$designation, r.created=timestamp()""",
            # NB: neo4j Session.run() reserves the kwarg name 'query' for the Cypher string itself,
            # so the bound parameter must NOT be called 'query' -> use $q.
            id=result.get("id", "spec_unknown"), project=_PROJECT, q=result.get("query"),
            kind=result.get("kind"), ok=bool(result.get("ok")),
            cache_hit=bool(result.get("cache_hit")), n_facts=len(facts), conf=top_conf,
            designation=(facts[0].get("designation") if facts else None))
    return True


def catalog() -> list[dict]:
    d = _connect()
    if d is None:
        return []
    with d.session() as s:
        return s.run(
            """MATCH (t:CkPartType) OPTIONAL MATCH (p:CkPart)-[:OF_TYPE]->(t)
               RETURN t.name AS type, count(p) AS instances ORDER BY type""").data()
