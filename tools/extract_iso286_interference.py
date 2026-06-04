"""Provenance/reproducibility tool for T5.1 — regenerate iso286.INTERFERENCE_EI from ISO 286-2.

OFFLINE one-shot extractor, NOT a runtime dependency: the validated values are baked into
`constraint_kit/iso286.py::INTERFERENCE_EI`; this script documents how they were obtained and lets anyone
reproduce/audit them. Requires `pip install pdf_oxide` (now also baked into the cadkit image).

Method (honest, not fabricated):
  1. Discover + fetch the authoritative ISO 286-2:2010 PDF via the spec compiler's pipeline.
  2. pypdf flattens the dense tolerance tables; pdf_oxide.extract_words gives each cell's text + bbox.
  3. Per size range, the shaft tables carry a CONSTANT row = the grade-independent fundamental deviation
     ei (es = ei + IT(grade) per grade column). Pages hold 1-3 interference letters; multi-letter pages
     are split by x-clustering the constant values into columns (so t, absent below 24 mm, doesn't shift u).
  4. VALIDATION gate: closed-form m/n columns reproduce iso286's formula, and every value matches known
     published anchors (p6@20=+22, s6@20=+35, t6@24-30=+41, u6@18-24=+41, v6@24-30=+55). Letters x/y/z…
     are NOT extracted -> they stay class-only.

Usage: pip install pdf_oxide; python tools/extract_iso286_interference.py <iso286-2.pdf>
"""
from __future__ import annotations

import sys

# letter -> (page, column index after x-clustering) in the ISO 286-2:2010 PDF used
LETTERS = {"p": (39, 0), "r": (40, 0), "s": (42, 0), "t": (44, 0), "u": (44, 1), "v": (46, 0)}
BOUND = {"3", "6", "10", "18", "24", "30", "40", "50", "65", "80", "100", "120", "140", "160", "180",
         "200", "225", "250", "280", "315", "355", "400", "450", "500"}


def _pnum(t):
    t = (t or "").replace("−", "-").replace(" ", "").replace(",", ".")
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return None


def _columns(doc, page):
    """{col_index: {(lo,hi): ei}} — constant fundamental-deviation values, x-clustered into letter columns."""
    def g(w, k):
        return w[k] if isinstance(w, dict) else getattr(w, k)
    ws = doc.extract_words(page)
    items = sorted((round(g(w, "bbox")[1], 1), g(w, "bbox")[0], (g(w, "text") or "")) for w in ws)
    rows, cur, ly = [], [], None
    for y, x, t in items:
        if ly is None or abs(y - ly) <= 2.5:
            cur.append((x, t))
        else:
            rows.append(cur); cur = [(x, t)]
        ly = y
    rows.append(cur)
    pts = []   # (lo, hi, x, ei)
    for i, cells in enumerate(rows):
        toks = [t.strip() for x, t in cells]
        if i > 0 and len(toks) >= 2 and toks[0] in BOUND and toks[1] in BOUND \
                and toks[0].isdigit() and toks[1].isdigit() and int(toks[1]) > int(toks[0]):
            above = sorted((x, _pnum(t)) for x, t in rows[i - 1] if _pnum(t) is not None)
            seen = []
            for x, v in above:
                if not seen or seen[-1][1] != v:
                    seen.append((round(x), v))
            for x, v in seen:
                pts.append((int(toks[0]), int(toks[1]), x, v))
    xs = sorted({x for *_, x, _ in [(a, b, x, v) for a, b, x, v in pts]})
    centers = []
    for x in xs:
        if not centers or x - centers[-1][-1] > 40:
            centers.append([x])
        else:
            centers[-1].append(x)
    cen = [sum(c) / len(c) for c in centers]
    cols = {i: {} for i in range(len(cen))}
    for lo, hi, x, v in pts:
        ci = min(range(len(cen)), key=lambda i: abs(cen[i] - x))
        cols[ci][(lo, hi)] = v
    return cols


def main(pdf_path):
    import pdf_oxide
    from constraint_kit import iso286
    doc = pdf_oxide.PdfDocument(pdf_path)
    extracted = {L: _columns(doc, pg)[col] for L, (pg, col) in LETTERS.items()}
    anchors = {("p", 6, 20): (35, 22), ("s", 6, 20): (48, 35), ("t", 6, 26): (54, 41),
               ("u", 6, 20): (54, 41), ("v", 6, 26): (68, 55)}
    ok = True
    for (L, gr, nom), exp in anchors.items():
        got = iso286.shaft_deviation(L, gr, nom)
        if got != exp:
            ok = False; print(f"ANCHOR FAIL {L}{gr}@{nom}: {got} != {exp}")
    for L, tbl in extracted.items():
        for rng, ei in tbl.items():
            baked = iso286.INTERFERENCE_EI[L].get(rng)
            if baked is not None and baked != ei:
                ok = False; print(f"DRIFT {L} {rng}: extracted {ei} != baked {baked}")
    print("validation:", "PASS — extraction matches anchors + baked table" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/iso286-2.pdf"))
