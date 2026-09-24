#!/usr/bin/env python3
"""Slice 1 of the autonomous loop — the concurrent CENSUS + corpus (READ-ONLY, safe to run unattended).

DeepSeek (2500-concurrent, cheap) generates a diverse NL request stream; each request runs through
/intent/design (qwen parse -> resolve -> build/verify) concurrently; outcomes are classified and gaps RANKED
BY FREQUENCY — the statistically-powered successor to the hand-run gauntlet. Verified (description -> program)
pairs are appended to the corpus (training data + a growing regression benchmark). The auto-FIXER (slice 2)
consumes this census's ranked gap report; this stage changes NOTHING in the system.

Honest governor: DeepSeek generation is effectively free; the throughput limit is cadkit's CPU-bound build,
so the /intent/design fan-out is a bounded worker pool, not 2500-wide.

  DEEPSEEK_API_KEY=... python3 drivers/census.py [n_per_category] [build_workers]
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

DEEPSEEK = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"
KEY = os.environ.get("DEEPSEEK_API_KEY")
CADKIT = os.environ.get("CADKIT_URL", "http://127.0.0.1:8195")
OUTDIR = os.path.join(os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit"), "cadkit/output")
CORPUS = f"{OUTDIR}/dsl_corpus.jsonl"

CATEGORIES = [
    "metric fasteners (bolts, nuts, washers, screws)",
    "gears and gear trains (spur, helical, planetary, ring)",
    "shafts, bearings, couplings, sprockets",
    "mounting plates and sheet-metal brackets",
    "aluminum V-slot extrusion frames and joints",
    "pipes, flanges, plumbing fittings",
    "enclosures, housings, panels, boxes",
    "linkages and simple mechanisms",
    "multi-part sub-assemblies (a few parts mated together)",
    "unusual or specialized mechanical parts",
]


def _post(url, payload, headers, timeout=240):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def deepseek(messages, temperature=0.9):
    body = {"model": MODEL, "messages": messages, "temperature": temperature,
            "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}}
    return _post(DEEPSEEK, body, {"Authorization": f"Bearer {KEY}"})["choices"][0]["message"]["content"]


def gen_requests(cat, n):
    sysmsg = (f"Generate {n} short, realistic, VARIED natural-language requests for individual mechanical "
              f"parts or small assemblies in this category: {cat}. Mix fully-specified (with mm dimensions) "
              f"and vague ones, as an engineer would ask. Output ONLY JSON: {{\"requests\": [\"...\", ...]}}.")
    try:
        out = json.loads(deepseek([{"role": "system", "content": sysmsg}, {"role": "user", "content": cat}]))
        reqs = out.get("requests", []) if isinstance(out, dict) else out
        return [r for r in reqs if isinstance(r, str) and r.strip()][:n]
    except Exception:  # noqa: BLE001 -- a flaky generation call just yields fewer requests
        return []


def is_build_request(text: str) -> bool:
    """Reject NON-BUILD requests (the visual census caught an advice question — 'What type of bolts are
    best…?' — entering the corpus as a build pair). Interrogatives/advice aren't design intents."""
    t = (text or "").strip().lower()
    if not t or t.endswith("?"):
        return False
    if t.split()[0] in ("what", "which", "how", "why", "when", "where", "who",
                        "is", "are", "can", "could", "should", "do", "does", "would"):
        return False
    return not any(p in t for p in ("best for", "recommend", "advice", "which one", "what kind"))


def design(req):
    return _post(f"{CADKIT}/intent/design", {"request": req, "build": True}, {})


def _norm_err(msg):
    return re.sub(r"\d+", "N", str(msg or "unknown")).strip()[:90]   # collapse specific values to cluster causes


def classify(d):
    resolved = d.get("resolved", {})
    if resolved.get("declined"):
        unexp = [f for e in resolved.get("entities", []) for f in e.get("unexpressible", [])]
        if unexp:
            return "declined-unexpressible", unexp
        return "declined-oov", [u.get("kind") for u in resolved.get("unresolved", []) if u.get("kind")]
    chk = d.get("check", {})
    if not chk.get("ok"):
        return "build-failed", [x.get("message") for x in chk.get("diagnostics", [])
                                if x.get("severity") == "error"] or [d.get("build_error", "check failed")]
    if d.get("build_error"):
        return "build-failed", [d["build_error"]]
    if d.get("signature"):
        return "built", d
    return "unknown", [str(d)[:100]]


def main():
    if not KEY:
        print("DEEPSEEK_API_KEY not set"); return
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    # 1) generate a diverse request stream (concurrent across categories) + dedupe
    print(f"generating ~{n*len(CATEGORIES)} requests across {len(CATEGORIES)} categories...")
    requests, seen = [], set()
    filtered = 0
    with ThreadPoolExecutor(max_workers=len(CATEGORIES)) as ex:
        for reqs in ex.map(lambda c: gen_requests(c, n), CATEGORIES):
            for r in reqs:
                if not is_build_request(r):
                    filtered += 1
                    continue
                k = r.strip().lower()
                if k not in seen:
                    seen.add(k); requests.append(r)
    print(f"  -> {len(requests)} unique build requests ({filtered} non-build filtered)")

    # 2) run through /intent/design concurrently (cadkit build = the governor)
    counts = Counter()
    declined_kinds, unexpressible, build_fail = Counter(), Counter(), Counter()
    kept = 0
    t0 = time.time()

    def run(req):
        try:
            return req, design(req)
        except Exception as exc:  # noqa: BLE001
            return req, {"_error": f"{type(exc).__name__}: {exc}"}

    with open(CORPUS, "a") as fh, ThreadPoolExecutor(max_workers=workers) as ex:
        for req, d in ex.map(run, requests):
            if "_error" in d:
                counts["error"] += 1; continue
            outcome, detail = classify(d)
            counts[outcome] += 1
            if outcome == "declined-oov":
                for k in detail:
                    declined_kinds[k] += 1
            elif outcome == "declined-unexpressible":
                for f in detail:
                    unexpressible[f] += 1
            elif outcome == "build-failed":
                for c in detail:
                    build_fail[_norm_err(c)] += 1
            elif outcome == "built":
                # pile-up gate (visual-census finding): a multi-part program whose parts overlap massively
                # was built with missing mates (everything at the origin) — NOT a faithful pair, don't keep.
                if d.get("overlap_fraction", 0) > 0.5:
                    counts["built-but-piled"] += 1
                    continue
                fh.write(json.dumps({"description": req, "program": d.get("program"),
                                     "signature": d.get("signature")}) + "\n")
                kept += 1

    # 3) ranked gap report (consumed by the slice-2 auto-fixer)
    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "n_requests": len(requests),
        "elapsed_s": round(time.time() - t0, 1), "counts": dict(counts),
        "declined_oov_kinds": declined_kinds.most_common(25),
        "unexpressible_features": unexpressible.most_common(25),
        "build_failures": build_fail.most_common(25), "verified_pairs_added": kept,
    }
    with open(f"{OUTDIR}/census_report.json", "w") as fh:
        json.dump(report, fh, indent=2)

    print(f"\n=== census ({len(requests)} requests, {report['elapsed_s']}s, {workers} build workers) ===")
    for k, v in counts.most_common():
        print(f"  {k}: {v}")
    print(f"  verified pairs added to corpus: {kept}")
    print("--- declined: OOV kinds (ranked) ---")
    for k, v in declined_kinds.most_common(12):
        print(f"  {k}: {v}")
    print("--- declined: unexpressible features (ranked) ---")
    for k, v in unexpressible.most_common(12):
        print(f"  {k}: {v}")
    print("--- build failures (clustered, ranked) ---")
    for k, v in build_fail.most_common(12):
        print(f"  {k}: {v}")
    print(f"\nreport -> {OUTDIR}/census_report.json")


if __name__ == "__main__":
    main()
