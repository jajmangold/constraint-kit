"""Spec compiler core: preseed, rule-based trust scoring, fact construction, validation, and the
public resolve()/resolve_thread() entry points (which drive the LangGraph in spec_graph.py).

Thesis: the moat is the *bot that reliably resolves a fact with provenance*, not a giant static DB.
Flow (orchestrated by LangGraph): need fact -> SQLite cache -> SearXNG discovery -> fetch -> extract
(text/table, VLM if visual) -> normalize -> score trust -> validate -> persist SQLite -> JSON artifact.
For v1 (ISO metric threads) the authoritative value also comes from the designation itself + a small
PRESEED; live sources add confirmation/provenance/confidence. Never trusts LLM/VLM as ground truth.
"""
from __future__ import annotations

import re
import uuid

# --- preseed: small bootstrap table, NOT a standards database, NOT an official source ---------------
PRESEED_THREADS = {  # (nominal_d_mm, pitch_mm) ISO metric coarse
    (3.0, 0.5): "M3x0.5", (4.0, 0.7): "M4x0.7", (5.0, 0.8): "M5x0.8",
    (6.0, 1.0): "M6x1", (8.0, 1.25): "M8x1.25", (10.0, 1.5): "M10x1.5",
}
_COARSE_BY_NOMINAL = {d: p for (d, p) in PRESEED_THREADS}

PRESEED_SOURCE_META = {
    "source_type": "preseed",
    "title": "constraint-kit preseed engineering table (threads/bearings/materials/fits)",
    "url": None,
    "license_note": ("local preseed for bootstrap; replace/confirm with authoritative source "
                     "when live source is available"),
}

# --- rule-based trust scoring (inspectable) ---------------------------------------------------------
_BASE = {"official_standard": 1.00, "manufacturer_catalog": 0.90, "distributor_catalog": 0.75,
         "handbook": 0.70, "webpage": 0.25, "forum": 0.10, "preseed": 0.85, "unknown": 0.25}
_MODS = {"structured_table": 0.20, "confirmed_second": 0.20, "has_edition_date": 0.10,
         "unit_stated": 0.10, "preseed_value": 0.05, "ocr_only": -0.20, "vlm_only": -0.15,
         "conflicting": -0.30, "missing_edition": -0.20}


def score_confidence(source_type: str, **flags: bool) -> float:
    """Base source score + evidence modifiers, clamped to [0,1]. Pure and inspectable."""
    s = _BASE.get(source_type, 0.25)
    for key, delta in _MODS.items():
        if flags.get(key):
            s += delta
    return max(0.0, min(1.0, round(s, 4)))


def parse_thread(designation: str):
    """Parse 'M6x1', 'M6x1.0', or 'M6' -> (nominal_mm, pitch_mm). Pitch falls back to coarse default.
    Returns (nominal, pitch) or None if unparseable."""
    if not designation:
        return None
    # accept 'M6x1', 'M6x1.0', 'M6×1', the bd_warehouse 'M6-1.0' dash form, or bare 'M6'
    m = re.search(r"M\s*(\d+(?:\.\d+)?)\s*(?:[xX×\-]\s*(\d+(?:\.\d+)?))?", designation)
    if not m:
        return None
    nominal = float(m.group(1))
    if m.group(2) is not None:
        return nominal, float(m.group(2))
    if nominal in _COARSE_BY_NOMINAL:
        return nominal, _COARSE_BY_NOMINAL[nominal]
    return None  # no pitch given and not a known coarse size


def canonical_thread(nominal: float, pitch: float) -> str:
    return f"M{nominal:g}x{pitch:g}"


def make_source(source_type: str, *, title=None, url=None, retrieved_at=None, source_hash=None,
                page=None, table=None, engine=None, license_note=None, confidence=0.0,
                sid: str | None = None) -> dict:
    return {"id": sid or f"src_{uuid.uuid4().hex[:10]}", "title": title, "url": url,
            "source_type": source_type, "retrieved_at": retrieved_at, "source_hash": source_hash,
            "page": page, "table": table, "engine": engine, "license_note": license_note,
            "confidence": confidence}


def make_value(value, unit, quantity, source_ref, confidence) -> dict:
    return {"value": value, "unit": unit, "quantity": quantity,
            "source_ref": source_ref, "confidence": confidence}


# --- broadened kinds (small preseeds, same flow) ---------------------------------------------------
PRESEED_BEARINGS = {  # deep-groove ball bearings: (bore, outer_diameter, width) mm (standard catalog)
    "608": (8, 22, 7), "688": (8, 16, 5), "626": (6, 19, 6), "623": (3, 10, 4), "624": (4, 13, 5),
    "625": (5, 16, 5), "695": (5, 13, 4), "698": (8, 19, 6),
    "6000": (10, 26, 8), "6001": (12, 28, 8), "6002": (15, 32, 9), "6003": (17, 35, 10),
    "6200": (10, 30, 9), "6201": (12, 32, 10), "6202": (15, 35, 11), "6203": (17, 40, 12),
    "6204": (20, 47, 14), "6205": (25, 52, 15), "6802": (15, 24, 5), "6803": (17, 26, 5),
}
PRESEED_MATERIALS = {  # density g/cm^3 (common engineering materials)
    "ti-6al-4v": 4.43, "aisi 304": 8.0, "304 stainless": 8.0, "carbon steel": 7.85,
    "aluminum": 2.70, "aluminium": 2.70, "6061": 2.70, "7075": 2.81, "titanium": 4.51,
    "stainless": 8.0, "brass": 8.50, "bronze": 8.80, "copper": 8.96, "cast iron": 7.20,
    "magnesium": 1.74, "zinc": 7.14, "nylon": 1.14, "steel": 7.85, "abs": 1.04, "pla": 1.24,
    "petg": 1.27, "ptfe": 2.20, "polycarbonate": 1.20, "acrylic": 1.18, "delrin": 1.41, "pom": 1.41,
}
PRESEED_FITS = {  # ISO 286 hole-basis fit -> class
    "h7/g6": "clearance", "h7/h6": "clearance", "h7/f7": "clearance",
    "h7/k6": "transition", "h7/n6": "transition", "h7/p6": "interference", "h7/s6": "interference",
}
PRESEED_SECTIONS = {  # metric structural sections (EN), dims mm, mass kg/m — standard handbook values
    # European IPE I-beams (S235): depth h, flange width b, web tw, flange tf
    "IPE 80":  {"shape": "i_beam",  "h": 80,  "b": 46,  "tw": 3.8, "tf": 5.2,  "mass_per_m": 6.0},
    "IPE 100": {"shape": "i_beam",  "h": 100, "b": 55,  "tw": 4.1, "tf": 5.7,  "mass_per_m": 8.1},
    "IPE 120": {"shape": "i_beam",  "h": 120, "b": 64,  "tw": 4.4, "tf": 6.3,  "mass_per_m": 10.4},
    "IPE 160": {"shape": "i_beam",  "h": 160, "b": 82,  "tw": 5.0, "tf": 7.4,  "mass_per_m": 15.8},
    "IPE 200": {"shape": "i_beam",  "h": 200, "b": 100, "tw": 5.6, "tf": 8.5,  "mass_per_m": 22.4},
    "IPE 240": {"shape": "i_beam",  "h": 240, "b": 120, "tw": 6.2, "tf": 9.8,  "mass_per_m": 30.7},
    "IPE 300": {"shape": "i_beam",  "h": 300, "b": 150, "tw": 7.1, "tf": 10.7, "mass_per_m": 42.2},
    # European UPN channels
    "UPN 80":  {"shape": "channel", "h": 80,  "b": 45,  "tw": 6.0, "tf": 8.0,  "mass_per_m": 8.64},
    "UPN 100": {"shape": "channel", "h": 100, "b": 50,  "tw": 6.0, "tf": 8.5,  "mass_per_m": 10.6},
    "UPN 120": {"shape": "channel", "h": 120, "b": 55,  "tw": 7.0, "tf": 9.0,  "mass_per_m": 13.4},
    "UPN 160": {"shape": "channel", "h": 160, "b": 65,  "tw": 7.5, "tf": 10.5, "mass_per_m": 18.8},
    "UPN 200": {"shape": "channel", "h": 200, "b": 75,  "tw": 8.5, "tf": 11.5, "mass_per_m": 25.3},
    # EN 10056 equal-leg angles L a×a×t
    "L 20x20x3": {"shape": "angle", "leg": 20, "t": 3, "mass_per_m": 0.88},
    "L 30x30x3": {"shape": "angle", "leg": 30, "t": 3, "mass_per_m": 1.36},
    "L 40x40x4": {"shape": "angle", "leg": 40, "t": 4, "mass_per_m": 2.42},
    "L 50x50x5": {"shape": "angle", "leg": 50, "t": 5, "mass_per_m": 3.77},
    "L 60x60x6": {"shape": "angle", "leg": 60, "t": 6, "mass_per_m": 5.42},
}


def _thread_parse(query: str):
    parsed = parse_thread(query)
    if not parsed:
        return None
    n, p = parsed
    return {"designation": canonical_thread(n, p), "system": "ISO metric", "values": {
        "nominal_diameter": {"value": n, "unit": "mm", "quantity": "nominal_thread_diameter"},
        "pitch": {"value": p, "unit": "mm", "quantity": "thread_pitch"}}}


def _bearing_parse(query: str):
    for des, (b, od, w) in PRESEED_BEARINGS.items():
        if re.search(rf"\b{des}\b", query):
            return {"designation": des, "system": "deep-groove ball bearing", "values": {
                "bore": {"value": float(b), "unit": "mm", "quantity": "bearing_bore"},
                "outer_diameter": {"value": float(od), "unit": "mm", "quantity": "bearing_outer_diameter"},
                "width": {"value": float(w), "unit": "mm", "quantity": "bearing_width"}}}
    return None


def _material_parse(query: str):
    q = query.lower()
    for name, dens in sorted(PRESEED_MATERIALS.items(), key=lambda kv: -len(kv[0])):
        if name in q:
            return {"designation": name, "system": "material", "values": {
                "density": {"value": dens, "unit": "g/cm^3", "quantity": "density"}}}
    return None


def _fit_parse(query: str):
    """Parse 'H7/g6' (+ optional nominal like 'at 20mm' / 'Ø20'). With a nominal size and supported
    letters, compute EXACT ISO 286 deviations + clearances (µm) via iso286; otherwise fall back to the
    preseed fit CLASS with a warning (never fabricate micron values)."""
    from . import iso286
    m = re.search(r"([A-Za-z])(\d+)\s*/\s*([A-Za-z]{1,2})(\d+)", query)   # shaft may be 'js'
    if not m:
        return None
    hl, hg, sl, sg = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    fit_str = f"{hl}{hg}/{sl}{sg}".lower()
    nm = (re.search(r"(?:Ø|ø)\s*(\d+(?:\.\d+)?)", query)
          or re.search(r"(\d+(?:\.\d+)?)\s*mm", query)
          or re.search(r"(?:at|nominal|dia(?:meter)?)\s*(\d+(?:\.\d+)?)", query, re.I))
    nominal = float(nm.group(1)) if nm else None
    warnings: list[str] = []

    if nominal is not None:
        f = iso286.fit(hl, hg, sl, sg, nominal)
        if f:
            def _v(val, unit, q):
                return {"value": val, "unit": unit, "quantity": q}
            return {"designation": f"{hl.upper()}{hg}/{sl.lower()}{sg}@{nominal:g}mm",
                    "system": "ISO 286 (computed)", "warnings": [], "values": {
                        "nominal_diameter": _v(f["nominal_mm"], "mm", "nominal_diameter"),
                        "min_clearance": _v(f["min_clearance_um"], "µm", "minimum_clearance"),
                        "max_clearance": _v(f["max_clearance_um"], "µm", "maximum_clearance"),
                        "hole_upper_dev": _v(f["hole_upper_dev_um"], "µm", "hole_upper_deviation"),
                        "hole_lower_dev": _v(f["hole_lower_dev_um"], "µm", "hole_lower_deviation"),
                        "shaft_upper_dev": _v(f["shaft_upper_dev_um"], "µm", "shaft_upper_deviation"),
                        "shaft_lower_dev": _v(f["shaft_lower_dev_um"], "µm", "shaft_lower_deviation"),
                        "fit_class": _v(f["fit_class"], "category", "fit_class")}}
        warnings.append(f"exact deviations unavailable for {fit_str} (interference letter p/r/s or "
                        f"size outside 3–500mm); reporting fit class only")
    else:
        warnings.append("provide a nominal size (e.g. 'H7/g6 at 20mm') for exact ISO 286 tolerances")

    cls = PRESEED_FITS.get(fit_str)
    if not cls:
        return None
    return {"designation": f"{hl.upper()}{hg}/{sl.lower()}{sg}", "system": "ISO 286 fit (class)",
            "warnings": warnings, "values": {
                "fit_class": {"value": cls, "unit": "category", "quantity": "fit_class"}}}


def _section_parse(query: str):
    """Parse a metric structural-section designation (T5.2): 'IPE 200'/'UPN 100' (I-beam/channel) or
    'L 40x40x4' (equal-leg angle). Returns dims + mass/length from the preseed, or None if not recognized
    (an unknown family/size is NOT fabricated)."""
    q = " ".join(query.upper().split())
    ma = re.search(r"\bL\s*(\d+)\s*[X×]\s*(\d+)\s*[X×]\s*(\d+)\b", q)
    if ma:
        a, b, t = int(ma.group(1)), int(ma.group(2)), int(ma.group(3))
        ent = PRESEED_SECTIONS.get(f"L {a}x{b}x{t}")
        if not ent:
            return None
        return {"designation": f"L {a}x{b}x{t}", "system": "EN 10056 equal-leg angle", "values": {
            "leg_length": {"value": float(ent["leg"]), "unit": "mm", "quantity": "angle_leg_length"},
            "thickness": {"value": float(ent["t"]), "unit": "mm", "quantity": "angle_thickness"},
            "mass_per_length": {"value": float(ent["mass_per_m"]), "unit": "kg/m",
                                "quantity": "mass_per_length"}}}
    mb = re.search(r"\b(IPE|UPN)\s*0*(\d+)\b", q)
    if mb:
        fam, num = mb.group(1), int(mb.group(2))
        ent = PRESEED_SECTIONS.get(f"{fam} {num}")
        if not ent:
            return None
        system = "EN IPE I-beam" if fam == "IPE" else "EN UPN channel"
        return {"designation": f"{fam} {num}", "system": system, "values": {
            "depth": {"value": float(ent["h"]), "unit": "mm", "quantity": "section_depth"},
            "width": {"value": float(ent["b"]), "unit": "mm", "quantity": "flange_width"},
            "web_thickness": {"value": float(ent["tw"]), "unit": "mm", "quantity": "web_thickness"},
            "flange_thickness": {"value": float(ent["tf"]), "unit": "mm", "quantity": "flange_thickness"},
            "mass_per_length": {"value": float(ent["mass_per_m"]), "unit": "kg/m",
                                "quantity": "mass_per_length"}}}
    return None


PARSERS = {"thread": _thread_parse, "bearing": _bearing_parse, "material": _material_parse,
           "fit": _fit_parse, "section": _section_parse}


def parse_for_kind(query: str, kind: str):
    """Parse a query for a known kind, or AUTO-DETECT when kind is 'unknown'. Returns (kind, parsed)
    or (kind, None)."""
    if kind in PARSERS:
        return kind, PARSERS[kind](query)
    for k, fn in PARSERS.items():       # auto-detect
        parsed = fn(query)
        if parsed:
            return k, parsed
    return kind, None


def build_fact(kind: str, parsed: dict, sources: list[dict]) -> dict:
    """Generic fact builder: wrap each parsed value with provenance (source_ref) + confidence."""
    primary = sources[0]
    confirmed = len(sources) > 1
    conf = score_confidence(primary["source_type"], preseed_value=primary["source_type"] == "preseed",
                            unit_stated=True, missing_edition=True, confirmed_second=confirmed)
    values = {name: make_value(v["value"], v["unit"], v["quantity"], primary["id"], conf)
              for name, v in parsed["values"].items()}
    return {"kind": kind, "designation": parsed["designation"], "system": parsed["system"],
            "values": values, "confidence": conf, "warnings": list(parsed.get("warnings", []))}


def validate_fact(fact: dict) -> list[str]:
    """Schema/unit/provenance validation of a normalized fact. [] = valid."""
    issues: list[str] = []
    if not fact.get("kind"):
        issues.append("fact missing 'kind'")
    if not fact.get("designation"):
        issues.append("fact missing 'designation'")
    if not fact.get("system"):
        issues.append("fact missing 'system'")
    c = fact.get("confidence")
    if not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
        issues.append(f"fact confidence out of [0,1]: {c!r}")
    values = fact.get("values") or {}
    if not values:
        issues.append("fact has no values")
    for name, v in values.items():
        for field in ("value", "unit", "quantity", "source_ref", "confidence"):
            if field not in v or v[field] is None:
                issues.append(f"value '{name}' missing '{field}'")
        vc = v.get("confidence")
        if isinstance(vc, (int, float)) and not (0.0 <= vc <= 1.0):
            issues.append(f"value '{name}' confidence out of [0,1]: {vc!r}")
    return issues


# --- public entry points (drive the LangGraph) ------------------------------------------------------
def resolve(query: str, kind: str = "unknown", prefer_cache: bool = True,
            allow_live: bool = True) -> dict:
    """Resolve an engineering fact via the LangGraph spec-compilation flow. Always returns a structured
    dict (ok true/false), never raises."""
    from . import spec_graph
    return spec_graph.run(query=query, kind=kind, prefer_cache=prefer_cache, allow_live=allow_live)


def resolve_thread(designation: str, prefer_cache: bool = True, allow_live: bool = True) -> dict:
    return resolve(designation, kind="thread", prefer_cache=prefer_cache, allow_live=allow_live)


def resolve_bearing(designation: str, prefer_cache: bool = True, allow_live: bool = True) -> dict:
    return resolve(designation, kind="bearing", prefer_cache=prefer_cache, allow_live=allow_live)


def resolve_material(name: str, prefer_cache: bool = True, allow_live: bool = True) -> dict:
    return resolve(name, kind="material", prefer_cache=prefer_cache, allow_live=allow_live)


def resolve_fit(fit: str, prefer_cache: bool = True, allow_live: bool = True) -> dict:
    return resolve(fit, kind="fit", prefer_cache=prefer_cache, allow_live=allow_live)


def resolve_section(designation: str, prefer_cache: bool = True, allow_live: bool = False) -> dict:
    """Resolve a structural-section designation (IPE/UPN/L) — offline preseed by default (T5.2)."""
    return resolve(designation, kind="section", prefer_cache=prefer_cache, allow_live=allow_live)
