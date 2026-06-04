"""LangGraph orchestration of the spec-compilation flow.

  parse_request -> check_sqlite_cache -[hit&prefer]-> return_result
                                      -[miss]-> search_sources -> rank_sources -> fetch_source
       -> extract_text_tables -[pdf/low-conf]-> extract_visual_tables_with_vlm -> normalize_facts
                               -[else]--------------------------------------------> normalize_facts
       -> score_confidence -> validate_fact_schema -> persist_sqlite -> write_json_artifact -> return_result

Fail-soft throughout: no internet / no PDF / no VLM -> still resolves from preseed+designation (offline),
or returns a structured ok:false with warnings. Never raises out of run().
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import spec_cache, spec_compiler, spec_db, spec_extract, spec_sources


class SpecState(TypedDict, total=False):
    query: str
    kind: str
    prefer_cache: bool
    allow_live: bool
    designation: str
    parsed: dict
    cache_hit: bool
    candidates: list
    ranked: list
    fetched: dict
    need_vlm: bool
    live_confirmed: bool
    confirm_source: dict
    sources: list
    facts: list
    ok: bool
    warnings: list
    result: dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _warn(state: SpecState, msg: str) -> list:
    return list(state.get("warnings", [])) + [msg]


# --- nodes -----------------------------------------------------------------------------------------
def parse_request(state: SpecState) -> dict:
    out: dict = {"warnings": list(state.get("warnings", []))}
    kind, parsed = spec_compiler.parse_for_kind(state["query"], state.get("kind", "unknown"))
    out["kind"] = kind                              # may be refined by auto-detect when 'unknown'
    if parsed:
        out["parsed"] = parsed
        out["designation"] = parsed["designation"]
    else:
        out["warnings"] = _warn(state, f"could not parse {kind} from {state['query']!r}")
    return out


def check_sqlite_cache(state: SpecState) -> dict:
    if not state.get("prefer_cache", True):
        return {"cache_hit": False}
    try:
        cached = spec_db.find_cached(state["query"], state.get("kind", "unknown"),
                                     state.get("designation"))
    except Exception:  # noqa: BLE001 -- cache is best-effort
        cached = None
    if cached:
        return {"cache_hit": True, "result": cached}
    return {"cache_hit": False}


def search_sources(state: SpecState) -> dict:
    if not state.get("allow_live", True):
        return {"candidates": []}
    des = state.get("designation") or state["query"]
    kind = state.get("kind", "unknown")
    queries = [f"{des} {kind} dimensions specification", f"{des} datasheet table"]
    cands = []
    for q in queries:
        cands.extend(spec_sources.search_searxng(q))
    return {"candidates": cands}


def rank_sources(state: SpecState) -> dict:
    return {"ranked": spec_sources.rank_sources(state.get("candidates", []))}


def fetch_source(state: SpecState) -> dict:
    ranked = state.get("ranked", [])
    if not ranked:
        return {"fetched": {"ok": False, "error": "no candidate sources"}}
    top = ranked[0]
    fetched = spec_extract.fetch_source(top["url"])
    fetched["source_type_guess"] = top.get("source_type_guess", "webpage")
    fetched["title"] = top.get("title")
    fetched["engine"] = top.get("engine")
    return {"fetched": fetched}


def _value_confirmed(text_or_tables, parsed: dict) -> bool:
    """A source confirms a fact only if it states ALL of its numeric values as standalone numeric TOKENS
    (T5.3 hardening: `spec_extract.value_confirmed` tokenizes numbers, so 6 no longer 'confirms' against
    16/0.6 — eliminating substring false-positives)."""
    return spec_extract.value_confirmed(text_or_tables, parsed)


def extract_text_tables(state: SpecState) -> dict:
    fetched = state.get("fetched", {})
    if not fetched.get("ok"):
        return {"need_vlm": False}
    if "text" in fetched:
        ext = spec_extract.extract_text_tables(fetched["text"])
        if ext.get("ok") and state.get("parsed") and _value_confirmed(
                ext.get("tables") or fetched["text"], state["parsed"]):
            cs = spec_compiler.make_source(
                fetched.get("source_type_guess", "webpage"), title=fetched.get("title"),
                url=fetched.get("url"), retrieved_at=fetched.get("retrieved_at"),
                source_hash=fetched.get("source_hash"), engine=fetched.get("engine"),
                table="html_table" if ext.get("n_tables") else None)
            cs["confidence"] = spec_compiler.score_confidence(
                cs["source_type"], structured_table=bool(ext.get("n_tables")),
                unit_stated=True, has_edition_date=False)
            return {"need_vlm": False, "live_confirmed": True, "confirm_source": cs}
        return {"need_vlm": False}
    # PDF: try the DETERMINISTIC layout extractor (pdf_oxide, T5.1) FIRST — deterministic-first doctrine.
    # Only if it can't confirm the parsed values do we fall to the untrusted VLM (last resort).
    pdf = fetched.get("pdf_path")
    if pdf:
        ext = spec_extract.extract_pdf(pdf)
        if ext.get("ok") and state.get("parsed") and spec_extract.value_confirmed(ext["text"], state["parsed"]):
            cs = spec_compiler.make_source(
                fetched.get("source_type_guess", "webpage"), title=fetched.get("title"),
                url=fetched.get("url"), retrieved_at=fetched.get("retrieved_at"),
                source_hash=fetched.get("source_hash"), engine=fetched.get("engine"),
                table=f"pdf_layout:{ext.get('extractor')}")
            cs["confidence"] = spec_compiler.score_confidence(
                cs["source_type"], structured_table=True, unit_stated=True, has_edition_date=False)
            return {"need_vlm": False, "live_confirmed": True, "confirm_source": cs}
        return {"need_vlm": True}            # deterministic PDF extraction didn't confirm -> VLM last resort
    return {"need_vlm": False}


def extract_visual_tables_with_vlm(state: SpecState) -> dict:
    """VLM is used ONLY here, for visual/PDF tables; its output is NOT trusted until validated, and for
    v1 a matched value yields a (VLM-penalized) confirming source. Fail-soft."""
    fetched = state.get("fetched", {})
    pdf = fetched.get("pdf_path")
    parsed = state.get("parsed")
    # v1 VLM extraction schema is thread-specific; only attempt for thread kind with a PDF.
    if not pdf or state.get("kind") != "thread" or not parsed:
        return {}
    schema = {"type": "object", "additionalProperties": False,
              "required": ["nominal_diameter_mm", "pitch_mm"],
              "properties": {"nominal_diameter_mm": {"type": ["number", "null"]},
                             "pitch_mm": {"type": ["number", "null"]}}}
    ext = spec_extract.extract_with_qwen_vlm(
        pdf, schema, f"Extract the ISO metric thread row for {state.get('designation')}.")
    if not ext.get("ok"):
        return {"warnings": _warn(state, f"vlm extraction failed: {ext.get('error')}")}
    data = ext.get("data", {})
    pv = parsed["values"]
    # VLM output must MATCH the deterministic designation value to count (never trusted alone)
    if (data.get("nominal_diameter_mm") == pv["nominal_diameter"]["value"]
            and data.get("pitch_mm") == pv["pitch"]["value"]):
        cs = spec_compiler.make_source(
            fetched.get("source_type_guess", "webpage"), title=fetched.get("title"),
            url=fetched.get("url"), retrieved_at=fetched.get("retrieved_at"),
            source_hash=fetched.get("source_hash"), table="vlm_table")
        cs["confidence"] = spec_compiler.score_confidence(cs["source_type"], vlm_only=True,
                                                          unit_stated=True)
        return {"live_confirmed": True, "confirm_source": cs}
    return {"warnings": _warn(state, "vlm extraction did not match deterministic value; discarded")}


def normalize_facts(state: SpecState) -> dict:
    parsed = state.get("parsed")
    if not parsed:
        return {"ok": False, "facts": [], "sources": [],
                "warnings": _warn(state, "no resolvable value (unparsed / no preseed match)")}
    ps = spec_compiler.make_source(
        "preseed", title=spec_compiler.PRESEED_SOURCE_META["title"], url=None,
        retrieved_at=_now(), license_note=spec_compiler.PRESEED_SOURCE_META["license_note"])
    ps["confidence"] = spec_compiler.score_confidence("preseed", preseed_value=True,
                                                      unit_stated=True, missing_edition=True)
    sources = [ps]
    if state.get("live_confirmed") and state.get("confirm_source"):
        sources.append(state["confirm_source"])
    fact = spec_compiler.build_fact(state.get("kind", "unknown"), parsed, sources)
    return {"sources": sources, "facts": [fact], "ok": True}


def score_confidence(state: SpecState) -> dict:
    # confidences are already computed per-source/per-fact during normalize; this node is the explicit
    # taxonomy step (and a hook for future re-scoring / conflict detection).
    return {}


def validate_fact_schema(state: SpecState) -> dict:
    warnings = list(state.get("warnings", []))
    ok = state.get("ok", False)
    for fact in state.get("facts", []):
        issues = spec_compiler.validate_fact(fact)
        if issues:
            ok = False
            warnings.extend(f"schema: {i}" for i in issues)
    return {"ok": ok, "warnings": warnings}


def _assemble(state: SpecState) -> dict:
    return {
        "id": state.get("resolution_id") or f"res_{uuid.uuid4().hex[:10]}",
        "ok": bool(state.get("ok", False)),
        "query": state["query"], "kind": state.get("kind", "unknown"),
        "cache_hit": bool(state.get("cache_hit", False)),
        "facts": state.get("facts", []), "sources": state.get("sources", []),
        "warnings": state.get("warnings", []),
    }


def persist_sqlite(state: SpecState) -> dict:
    result = _assemble(state)
    try:
        rid = spec_db.write_resolution(result)
        result["id"] = rid
    except Exception as exc:  # noqa: BLE001 -- never crash a resolution on a storage error
        result["warnings"] = list(result.get("warnings", [])) + [f"sqlite persist failed: {exc}"]
    # work-tracking breadcrumb in atlas (NOT spec data) -- fail-soft
    try:
        from . import store
        store.record_spec_resolution(result)
    except Exception:  # noqa: BLE001
        pass
    return {"result": result}


def write_json_artifact(state: SpecState) -> dict:
    result = state.get("result") or _assemble(state)
    path = spec_cache.write_json_artifact(result)
    if path:
        result = dict(result); result["artifact"] = path
    return {"result": result}


def return_result(state: SpecState) -> dict:
    res = state.get("result")
    if res:                       # cache hit or already assembled
        res = dict(res); res["cache_hit"] = bool(state.get("cache_hit", res.get("cache_hit", False)))
        return {"result": res}
    return {"result": _assemble(state)}


# --- routing ---------------------------------------------------------------------------------------
def _route_cache(state: SpecState) -> str:
    return "return_result" if (state.get("cache_hit") and state.get("prefer_cache", True)) \
        else "search_sources"


def _route_extract(state: SpecState) -> str:
    return "extract_visual_tables_with_vlm" if state.get("need_vlm") else "normalize_facts"


_APP = None


def _build():
    g = StateGraph(SpecState)
    for name, fn in [
        ("parse_request", parse_request), ("check_sqlite_cache", check_sqlite_cache),
        ("search_sources", search_sources), ("rank_sources", rank_sources),
        ("fetch_source", fetch_source), ("extract_text_tables", extract_text_tables),
        ("extract_visual_tables_with_vlm", extract_visual_tables_with_vlm),
        ("normalize_facts", normalize_facts), ("score_confidence", score_confidence),
        ("validate_fact_schema", validate_fact_schema), ("persist_sqlite", persist_sqlite),
        ("write_json_artifact", write_json_artifact), ("return_result", return_result),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "parse_request")
    g.add_edge("parse_request", "check_sqlite_cache")
    g.add_conditional_edges("check_sqlite_cache", _route_cache,
                            {"return_result": "return_result", "search_sources": "search_sources"})
    g.add_edge("search_sources", "rank_sources")
    g.add_edge("rank_sources", "fetch_source")
    g.add_edge("fetch_source", "extract_text_tables")
    g.add_conditional_edges("extract_text_tables", _route_extract,
                            {"extract_visual_tables_with_vlm": "extract_visual_tables_with_vlm",
                             "normalize_facts": "normalize_facts"})
    g.add_edge("extract_visual_tables_with_vlm", "normalize_facts")
    g.add_edge("normalize_facts", "score_confidence")
    g.add_edge("score_confidence", "validate_fact_schema")
    g.add_edge("validate_fact_schema", "persist_sqlite")
    g.add_edge("persist_sqlite", "write_json_artifact")
    g.add_edge("write_json_artifact", "return_result")
    g.add_edge("return_result", END)
    return g.compile()


def run(query: str, kind: str = "unknown", prefer_cache: bool = True, allow_live: bool = True) -> dict:
    """Invoke the compiled LangGraph; always returns a structured result dict (never raises)."""
    global _APP
    if _APP is None:
        _APP = _build()
    try:
        final = _APP.invoke({"query": query, "kind": kind, "prefer_cache": prefer_cache,
                             "allow_live": allow_live, "warnings": []})
        return final.get("result") or {"ok": False, "query": query, "kind": kind,
                                        "cache_hit": False, "facts": [], "sources": [],
                                        "warnings": ["graph produced no result"]}
    except Exception as exc:  # noqa: BLE001 -- top-level fail-soft
        return {"ok": False, "query": query, "kind": kind, "cache_hit": False, "facts": [],
                "sources": [], "warnings": [f"graph error: {type(exc).__name__}: {exc}"]}
