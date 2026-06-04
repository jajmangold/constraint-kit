"""SQLite durable store for the spec compiler (NOT atlas — spec DATA lives here).

Tables: spec_resolutions, spec_facts, spec_sources, spec_fact_sources, spec_cache_index.
All writes are parameterized and a resolution+facts+sources land in ONE atomic transaction.
Fail-soft is the caller's job (api boundary); these functions raise on real DB bugs so tests catch them.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_PATH = os.environ.get(
    "SPEC_DB_PATH", "/srv/nvme-data/containers/constraint-kit/cadkit/output/specs.sqlite")

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS spec_resolutions (
        id TEXT PRIMARY KEY, query TEXT NOT NULL, kind TEXT NOT NULL, ok INTEGER NOT NULL,
        cache_hit INTEGER NOT NULL, created_at TEXT NOT NULL, result_json TEXT NOT NULL,
        warnings_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS spec_facts (
        id TEXT PRIMARY KEY, resolution_id TEXT NOT NULL, kind TEXT NOT NULL, designation TEXT,
        system TEXT, confidence REAL NOT NULL, fact_json TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(resolution_id) REFERENCES spec_resolutions(id))""",
    """CREATE TABLE IF NOT EXISTS spec_sources (
        id TEXT PRIMARY KEY, title TEXT, url TEXT, source_type TEXT NOT NULL, retrieved_at TEXT,
        source_hash TEXT, page TEXT, table_name TEXT, engine TEXT, license_note TEXT,
        confidence REAL NOT NULL, source_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS spec_fact_sources (
        fact_id TEXT NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(fact_id, source_id),
        FOREIGN KEY(fact_id) REFERENCES spec_facts(id),
        FOREIGN KEY(source_id) REFERENCES spec_sources(id))""",
    """CREATE TABLE IF NOT EXISTS spec_cache_index (
        cache_key TEXT PRIMARY KEY, kind TEXT NOT NULL, designation TEXT, query TEXT NOT NULL,
        resolution_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT,
        FOREIGN KEY(resolution_id) REFERENCES spec_resolutions(id))""",
    "CREATE INDEX IF NOT EXISTS idx_spec_cache_kind_designation ON spec_cache_index(kind, designation)",
    "CREATE INDEX IF NOT EXISTS idx_spec_cache_query ON spec_cache_index(query)",
    "CREATE INDEX IF NOT EXISTS idx_spec_facts_designation ON spec_facts(designation)",
    "CREATE INDEX IF NOT EXISTS idx_spec_sources_hash ON spec_sources(source_hash)",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | None = None) -> str:
    path = path or DEFAULT_PATH
    conn = _connect(path)
    try:
        with conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)
    finally:
        conn.close()
    return path


def cache_key(query: str, kind: str, designation: str | None) -> str:
    return f"{kind}|{designation}" if designation else f"{kind}|q|{query.strip().lower()}"


def write_resolution(result: dict, path: str | None = None) -> str:
    """Persist a full resolution (resolution row + facts + sources + links + cache index) atomically.
    Returns the resolution id. Reuses `result['id']` if present, else derives one."""
    path = path or DEFAULT_PATH
    init_db(path)
    rid = result.get("id") or f"res_{abs(hash((result.get('query'), result.get('kind'), _now()))) & 0xffffffff:08x}"
    now = _now()
    facts = result.get("facts", [])
    sources = result.get("sources", [])
    designation = facts[0].get("designation") if facts else result.get("designation")
    conn = _connect(path)
    try:
        with conn:  # atomic: all-or-nothing
            conn.execute(
                "INSERT OR REPLACE INTO spec_resolutions(id,query,kind,ok,cache_hit,created_at,"
                "result_json,warnings_json) VALUES(?,?,?,?,?,?,?,?)",
                (rid, result.get("query", ""), result.get("kind", "unknown"),
                 1 if result.get("ok") else 0, 1 if result.get("cache_hit") else 0, now,
                 json.dumps(result), json.dumps(result.get("warnings", []))))
            # sources first (facts link to them)
            for s in sources:
                conn.execute(
                    "INSERT OR REPLACE INTO spec_sources(id,title,url,source_type,retrieved_at,"
                    "source_hash,page,table_name,engine,license_note,confidence,source_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (s["id"], s.get("title"), s.get("url"), s.get("source_type", "unknown"),
                     s.get("retrieved_at"), s.get("source_hash"),
                     None if s.get("page") is None else str(s.get("page")),
                     s.get("table"), s.get("engine"), s.get("license_note"),
                     float(s.get("confidence", 0.0)), json.dumps(s)))
            for i, f in enumerate(facts):
                fid = f.get("id") or f"{rid}_fact{i}"
                conn.execute(
                    "INSERT OR REPLACE INTO spec_facts(id,resolution_id,kind,designation,system,"
                    "confidence,fact_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (fid, rid, f.get("kind", "unknown"), f.get("designation"), f.get("system"),
                     float(f.get("confidence", 0.0)), json.dumps(f), now))
                # link fact -> each source referenced by its values
                refs = {v.get("source_ref") for v in f.get("values", {}).values() if v.get("source_ref")}
                for sref in refs:
                    conn.execute("INSERT OR IGNORE INTO spec_fact_sources(fact_id,source_id) "
                                 "VALUES(?,?)", (fid, sref))
            conn.execute(
                "INSERT OR REPLACE INTO spec_cache_index(cache_key,kind,designation,query,"
                "resolution_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (cache_key(result.get("query", ""), result.get("kind", "unknown"), designation),
                 result.get("kind", "unknown"), designation, result.get("query", ""), rid, now,
                 result.get("expires_at")))
    finally:
        conn.close()
    return rid


def find_cached(query: str, kind: str, designation: str | None = None,
                path: str | None = None) -> dict | None:
    """Return the cached normalized result for (kind, designation) or (kind, query), or None."""
    path = path or DEFAULT_PATH
    init_db(path)
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT r.result_json FROM spec_cache_index c JOIN spec_resolutions r "
            "ON r.id=c.resolution_id WHERE c.cache_key=?",
            (cache_key(query, kind, designation),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    res = json.loads(row["result_json"])
    res["cache_hit"] = True
    return res


def recent(limit: int = 20, path: str | None = None) -> list[dict]:
    path = path or DEFAULT_PATH
    init_db(path)
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT id,query,kind,ok,cache_hit,created_at FROM spec_resolutions "
            "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def validate_integrity(path: str | None = None) -> list[str]:
    """Return a list of integrity problems ([] = healthy): SQLite integrity + FK + orphan checks."""
    path = path or DEFAULT_PATH
    init_db(path)
    issues: list[str] = []
    conn = _connect(path)
    try:
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            issues.append(f"integrity_check: {ic}")
        for r in conn.execute("PRAGMA foreign_key_check").fetchall():
            issues.append(f"foreign_key_violation: {tuple(r)}")
        orphan_facts = conn.execute(
            "SELECT count(*) FROM spec_facts f LEFT JOIN spec_resolutions r ON r.id=f.resolution_id "
            "WHERE r.id IS NULL").fetchone()[0]
        if orphan_facts:
            issues.append(f"orphan_facts: {orphan_facts}")
    finally:
        conn.close()
    return issues
