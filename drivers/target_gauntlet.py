#!/usr/bin/env python3
"""Target gauntlet — measure-first capability probe.

Drive real, just-beyond-reach requests through /intent/design (NL -> qwen parse -> resolve -> build) and
CLASSIFY each outcome into a ranked GAP LIST:
  built        - resolved + compiled + has a geometry signature (success).
  declined     - a `kind` no part expresses (EXPRESSIVENESS gap; the missing part is recorded).
  build-failed - resolved but check/compile failed (REALIZE / anchor / mate / param gap).
The point is to let the gap COUNTS decide the next big investment (feature-op DSL vs more named parts vs
decomposition vs verify), instead of guessing. Doubles as an honest 'what can this build today' snapshot.

Server-side parse (qwen via the endpoint) — no API key needed, just cadkit.
  python3 drivers/target_gauntlet.py
"""
from __future__ import annotations

import json
import urllib.request
from collections import Counter

CADKIT = "http://127.0.0.1:8195"

# real, just-beyond-reach mechanical targets — a mix of in-vocab composition and likely-OOV/feature needs,
# chosen so the failures are DIAGNOSTIC (pinpoint missing parts/relations), not a flood.
TARGETS = [
    "a NEMA 17 motor mount: a 42mm square aluminum plate 6mm thick with a 22mm center bore and four M3 holes on a 31mm square pattern",
    "a pillow block bearing on a 2020 aluminum extrusion, with a 10mm shaft through the bearing and a 20-tooth sprocket on the shaft",
    "a 2-stage spur gear reducer: a 16-tooth module-1.5 pinion on an input shaft meshing a 48-tooth gear on an output shaft, inside a rectangular housing",
    "a 2-inch weld-neck pipe flange joint: two flanges bolted face to face with eight M16 bolts",
    "a planetary gear reduction: sun 12 teeth, three planets of 18 teeth, module 1, 8mm wide",
    "a bolted L-bracket joining two 2020 extrusions at 90 degrees with M5 screws",
    "a GoPro-style clevis mount: a flat base with two parallel tabs 15mm apart, each with a 5mm pivot hole",
    "a helical gear, 24 teeth, module 2, 20 degree helix angle, 10mm wide",
    "a keyed output shaft, 12mm diameter, 60mm long, with a 4mm keyway",
    "a flexible jaw coupling joining two 8mm shafts",
]


def design(req):
    r = urllib.request.Request(f"{CADKIT}/intent/design",
                               data=json.dumps({"request": req, "build": True}).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=240).read())


def classify(d):
    if d.get("declined"):
        kinds = [u.get("kind") for u in d.get("resolved", {}).get("unresolved", [])]
        return "declined", kinds
    chk = d.get("check", {})
    if not chk.get("ok"):
        errs = [x.get("message") for x in chk.get("diagnostics", []) if x.get("severity") == "error"]
        return "build-failed", errs or [d.get("build_error", "check failed")]
    if d.get("build_error"):
        return "build-failed", [d["build_error"]]
    if d.get("signature"):
        return "built", d["signature"]
    return "unknown", [str(d)[:120]]


def main():
    counts = Counter()
    missing_kinds = Counter()
    build_failures = []
    rows = []
    for req in TARGETS:
        try:
            d = design(req)
        except Exception as exc:  # noqa: BLE001
            counts["error"] += 1
            rows.append(("ERROR", req, f"{type(exc).__name__}: {exc}"))
            continue
        outcome, detail = classify(d)
        counts[outcome] += 1
        kinds = [e.get("kind") for e in d.get("intent", {}).get("entities", [])]
        if outcome == "declined":
            for k in detail:
                missing_kinds[k] += 1
        elif outcome == "build-failed":
            build_failures.append((req, detail))
        rows.append((outcome.upper(), req, f"kinds={kinds}" + (f"  detail={detail}" if outcome != "built" else
                                                               f"  vol={detail.get('volume')}")))
    print("=== target gauntlet ===")
    for tag, req, info in rows:
        print(f"  [{tag:12}] {req[:62]}")
        print(f"               {info[:110]}")
    print("\n=== outcome counts ===")
    for k, v in counts.most_common():
        print(f"  {k}: {v}/{len(TARGETS)}")
    print("=== EXPRESSIVENESS gaps (missing kinds, ranked) ===")
    for k, v in missing_kinds.most_common():
        print(f"  {k}: {v}")
    print("=== REALIZE/build failures ===")
    for req, detail in build_failures:
        print(f"  {req[:60]} -> {detail}")


if __name__ == "__main__":
    main()
