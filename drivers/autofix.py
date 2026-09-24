#!/usr/bin/env python3
"""Slice 2 of the autonomous loop — the VERIFIED auto-fixer.

Consumes the census's ranked gap report and autonomously closes the SAFE class (param-name aliases), under
hard gates so nothing semantically-uncertain or regressive ever ships:
  1. DeepSeek proposes, for each unexpressible param token, a canonical param it is a SYNONYM of -
     'high' confidence only when it's a true same-meaning synonym (else 'low' -> queued, never applied).
  2. high-confidence proposals are written as DATA to constraint_kit/learned_aliases.json (the resolver
     loads it on a fresh import; we never edit code).
  3. VERIFY (fresh in-container imports): each alias must FLIP a previously-unexpressible requirement to
     resolvable (mechanism works) AND the full 108-test suite must stay green (no regression).
  4. green -> keep the flipping aliases + git-commit the JSON (small, reversible). red -> revert to empty.
Ambiguous proposals and OOV missing-kinds are written to a review_queue.json (human-ratified: aliases that
aren't clear synonyms, and new parts to author) - the auto-fixer NEVER writes generator code.

  DEEPSEEK_API_KEY=... python3 drivers/autofix.py
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

ROOT = os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit")
LEARNED = f"{ROOT}/constraint_kit/learned_aliases.json"
REVIEW = f"{ROOT}/cadkit/output/review_queue.json"
REPORT = f"{ROOT}/cadkit/output/census_report.json"
DEEPSEEK = "https://api.deepseek.com/chat/completions"
KEY = os.environ.get("DEEPSEEK_API_KEY")

CANON_PARAMS = ["af", "base_length", "bend_radius", "bolt_circle", "bolt_count", "bolt_d", "bolt_spacing",
                "bore_d", "boss_d", "boss_h", "chain_pitch", "depth", "diameter", "flange_length", "head_d",
                "head_h", "height", "helix_angle", "length", "major_diameter", "module", "n_planets", "nps",
                "outer_d", "pitch", "planet_teeth", "pressure_angle", "rail_size", "rim_width", "shank_d",
                "size", "sun_teeth", "teeth", "thick", "thickness", "wall", "width"]


def deepseek(messages):
    body = {"model": "deepseek-v4-flash", "messages": messages, "temperature": 0.0,
            "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}}
    req = urllib.request.Request(DEEPSEEK, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["message"]["content"]


def _docker_py(code: str) -> str:
    return subprocess.run(["docker", "exec", "cadkit", "python3", "-c", code],
                          capture_output=True, text=True, timeout=600).stdout.strip()


def propose(tokens):
    """Ask DeepSeek which tokens are true synonyms of a canonical param. Returns {token: {canonical, confidence}}."""
    sys = ("You map a parameter NAME a user wrote to the canonical parameter it is a SYNONYM of. "
           f"Canonical params: {CANON_PARAMS}. For each input token, if it means the SAME physical quantity "
           "as exactly ONE canonical param (a true synonym, e.g. 'od'->'outer_d', 'face_width'->'width'), "
           "return that canonical with confidence 'high'. If it is ambiguous, a different quantity, or not a "
           "clear synonym (e.g. 'bolt_d' is a bolt-hole dia, NOT a bearing 'bore_d'), return canonical null, "
           'confidence "low". Output ONLY JSON: {"<token>": {"canonical": <name|null>, "confidence": "high"|"low"}}.')
    try:
        return json.loads(deepseek([{"role": "system", "content": sys},
                                    {"role": "user", "content": json.dumps(tokens)}]))
    except Exception as exc:  # noqa: BLE001
        print("proposal failed:", exc); return {}


def main():
    if not KEY:
        print("DEEPSEEK_API_KEY not set"); return
    report = json.load(open(REPORT))
    # candidate tokens = unexpressible features that look like param names BUT are not already canonical
    # params (a token that IS a canonical param is a wrong-part-choice, not a synonym to alias).
    tokens = [t for t, _ in report.get("unexpressible_features", [])
              if t and t.replace("_", "").isalpha() and t not in CANON_PARAMS]
    oov_kinds = [k for k, _ in report.get("declined_oov_kinds", [])]
    print(f"candidate param tokens: {tokens}")
    proposals = propose(tokens)

    high, queued = {}, {}
    for tok, p in proposals.items():
        canon = (p or {}).get("canonical")
        if (p or {}).get("confidence") == "high" and canon in CANON_PARAMS and canon != tok.strip().lower():
            high[tok.strip().lower()] = canon
        else:
            queued[tok] = p
    print(f"high-confidence synonym proposals (auto-apply candidates): {high}")
    print(f"queued (ambiguous, human review): {list(queued)}")

    # write the review queue (ambiguous aliases + the OOV new-part backlog) — never auto-applied
    json.dump({"ambiguous_aliases": queued, "new_part_backlog": oov_kinds},
              open(REVIEW, "w"), indent=2)

    if not high:
        print("no high-confidence aliases to apply."); return

    # apply as DATA to learned_aliases.json
    learned = json.load(open(LEARNED))
    before = dict(learned.get("req", {}))
    learned.setdefault("req", {}).update(high)
    json.dump(learned, open(LEARNED, "w"), indent=2)

    # VERIFY 1: each alias must FLIP a previously-unexpressible requirement (fresh import reads the JSON)
    flip_code = (
        "import json\nfrom constraint_kit import intent\nfrom constraint_kit.builder import ALL_PART_GENS\n"
        f"al={json.dumps(high)}\nres={{}}\n"
        "for a,c in al.items():\n"
        " ks=[k for k in ALL_PART_GENS if c in intent._param_defaults(k) and a not in intent._param_defaults(k)]\n"
        " if not ks: res[a]='no-test-part'; continue\n"
        " r=intent.resolve({'entities':[{'id':'x','kind':ks[0],'requirements':{a:1}}]})\n"
        " res[a]='flips' if not r['declined'] else 'still-declined'\n"
        "print(json.dumps(res))")
    flips = json.loads(_docker_py(flip_code) or "{}")
    print(f"flip check: {flips}")
    kept = {a: c for a, c in high.items() if flips.get(a) == "flips"}

    # VERIFY 2: full suite must stay green with the kept aliases applied
    learned["req"] = {**before, **kept}
    json.dump(learned, open(LEARNED, "w"), indent=2)
    suite = subprocess.run(["docker", "exec", "cadkit", "python3", f"{ROOT}/tests/test_kernel.py"],
                           capture_output=True, text=True, timeout=900).stdout
    green = "passed" in suite.splitlines()[-1] if suite.strip() else False
    print(f"suite: {suite.strip().splitlines()[-1] if suite.strip() else 'no output'} -> {'GREEN' if green else 'RED'}")

    if green and kept:
        msg = (f"autofix: learn verified param-name aliases {kept}\n\n"
               "Autonomous slice-2 fix: DeepSeek proposed these as high-confidence synonyms; each FLIPS a "
               "previously-unexpressible requirement to resolvable, and the full 108-suite stays green. "
               "Applied as DATA (learned_aliases.json), not code. Reversible.\n\n"
               "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>")
        subprocess.run(["git", "-C", ROOT, "-c", "user.name=autofix", "-c", "user.email=autofix@local",
                        "add", LEARNED, REVIEW], check=False)
        subprocess.run(["git", "-C", ROOT, "-c", "user.name=autofix", "-c", "user.email=autofix@local",
                        "commit", "-q", "-m", msg], check=False)
        print(f"\nACCEPTED + committed: {kept}")
    else:
        json.dump({"kind": learned.get("kind", {}), "req": before}, open(LEARNED, "w"), indent=2)  # revert
        print("\nREVERTED (suite red or nothing flipped) — no change shipped.")
    print(f"review queue -> {REVIEW}")


if __name__ == "__main__":
    main()
