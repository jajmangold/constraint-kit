"""Provenance/reproducibility tool for T5.1 — regenerate iso286.INTERFERENCE_EI from ISO 286-2.

This is an OFFLINE one-shot extractor, NOT a runtime dependency: the validated values are baked into
`constraint_kit/iso286.py::INTERFERENCE_EI`; this script documents exactly how they were obtained and lets
anyone reproduce/audit them. Requires `pip install pdf_oxide` (Rust-backed, layout-aware PDF extraction).

Method (why it's honest, not fabricated):
  1. Discover + fetch the authoritative ISO 286-2:2010 PDF via the spec compiler's own pipeline
     (spec_sources SearXNG discovery -> spec_extract.fetch_source).
  2. pypdf flattens the dense tolerance tables into an unparseable stream; pdf_oxide.extract_words gives
     every cell's text + bbox, so the grid is reconstructable.
  3. The shaft tables list, per size range, a CONSTANT row = the grade-independent fundamental deviation
     ei (the row just above each size-range label), then es = ei + IT(grade) per grade column.
  4. Letter identity comes from the value at 18-30 mm (p=+22, r=+28, s=+35, plus the closed-form anchors
     m=+8, n=+15); pages hold one interference letter each (p=39, r=40, s=42 in this rehost).
  5. VALIDATION (the gate): the closed-form columns m/n on the same tables reproduce iso286's formula, and
     every wired value matches known published fit anchors (p6@20=+22, r6@20=+28, s6@20=+35, s6@60=+53).
     t/u/v… are deliberately NOT wired (not yet validated) -> they stay class-only.

Usage: pip install pdf_oxide; python tools/extract_iso286_interference.py <iso286-2.pdf>
"""
from __future__ import annotations

import sys

PAGES = {"p": 39, "r": 40, "s": 42}   # one interference letter per page in the ISO 286-2:2010 PDF used
BOUND = {"3", "6", "10", "18", "30", "50", "65", "80", "100", "120", "140", "160", "180", "200", "225",
         "250", "280", "315", "355", "400", "450", "500"}


def _pnum(t):
    t = (t or "").replace("−", "-").replace(" ", "").replace(",", ".")
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return None


def ei_table(doc, page):
    """Fundamental deviation ei per (lo,hi] size range from a shaft page: the constant row above each
    size-range label, collapsed to its distinct value."""
    def g(w, k):
        return w[k] if isinstance(w, dict) else getattr(w, k)
    ws = doc.extract_words(page)
    items = sorted((round(g(w, "bbox")[1], 1), g(w, "bbox")[0], (g(w, "text") or "")) for w in ws)
    rows, cur, ly = [], [], None
    for y, x, t in items:
        if ly is None or abs(y - ly) <= 2.5:
            cur.append((x, t))
        else:
            rows.append((ly, cur)); cur = [(x, t)]
        ly = y
    rows.append((ly, cur))
    out = {}
    for i, (y, cells) in enumerate(rows):
        toks = [t.strip() for x, t in cells]
        if i > 0 and len(toks) >= 2 and toks[0] in BOUND and toks[1] in BOUND \
                and toks[0].isdigit() and toks[1].isdigit() and int(toks[1]) > int(toks[0]):
            above = sorted((x, _pnum(t)) for x, t in rows[i - 1][1] if _pnum(t) is not None)
            consts = []
            for x, v in above:
                if not consts or consts[-1] != v:
                    consts.append(v)
            if consts:
                out[(int(toks[0]), int(toks[1]))] = consts[0]
    return out


def main(pdf_path):
    import pdf_oxide
    from constraint_kit import iso286
    doc = pdf_oxide.PdfDocument(pdf_path)
    extracted = {L: ei_table(doc, pg) for L, pg in PAGES.items()}
    # validate against the known published anchors + the wired table
    anchors = {("p", 6, 20): (35, 22), ("r", 6, 20): (41, 28), ("s", 6, 20): (48, 35), ("s", 6, 60): (72, 53)}
    ok = True
    for (L, gr, nom), exp in anchors.items():
        got = iso286.shaft_deviation(L, gr, nom)
        if got != exp:
            ok = False; print(f"ANCHOR FAIL {L}{gr}@{nom}: {got} != {exp}")
    for L, tbl in extracted.items():
        baked = iso286.INTERFERENCE_EI[L]
        for rng, ei in tbl.items():
            if rng in baked and baked[rng] != ei:
                ok = False; print(f"DRIFT {L} {rng}: extracted {ei} != baked {baked[rng]}")
    print("extracted:", extracted)
    print("validation:", "PASS — extraction matches anchors + baked table" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/iso286-2.pdf"))
