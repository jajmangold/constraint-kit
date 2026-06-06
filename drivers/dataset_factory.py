#!/usr/bin/env python3
"""Dataset factory — the $10 first-iteration SFT dataset for qwen3.6-27B (train on vast.ai later).

Stages (each tracks DeepSeek spend live):
  s0asm       per-style caption->program ROUND-TRIP yield on ASSEMBLIES (the unknown that gates the mix)
  s5 [n]      reasoning triples: tier A (gold: thinking-on, verifier-filtered) + tier B (answer-conditioned
              backfill) with DETERMINISTIC facts (pitch dia, mesh center distance, sanity checks) woven in
  s6 [n]      correction trajectories: error-inject a verified program along the MEASURED failure taxonomy ->
              REAL checker diagnostics -> trained correction turn (bad turn LOSS-MASKED)
  assemble    enforce >=75% reasoning / <=25% direct, emit Qwen <think> ChatML rows -> dataset/{train,val}.jsonl

Run IN the container:  docker exec -e DEEPSEEK_API_KEY=$KEY cadkit python3 drivers/dataset_factory.py <stage>
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import random
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from campaign import STYLES, geometry_grid  # noqa: E402
from corpus_factory import _build_and_sig, _match  # noqa: E402
from dsl_experiment import SYSTEM as ASM_GEN_SYS  # noqa: E402  (the benchmark-validated NL->DSL prompt w/ mates)

OUT = "/srv/nvme-data/containers/constraint-kit/cadkit/output"
DS_DIR = f"{OUT}/dataset"
os.makedirs(DS_DIR, exist_ok=True)
KEY = os.environ.get("DEEPSEEK_API_KEY")
DS_WORKERS = 128

# ---- spend tracking -------------------------------------------------------------------------------------
_usage = Counter()
_ulock = threading.Lock()


def _ds(system, user, temp=0.3, thinking=False, max_tokens=2200):
    body = {"model": "deepseek-v4-flash", "temperature": temp, "max_tokens": max_tokens,
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if not thinking:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    r = json.loads(urllib.request.urlopen(req, timeout=300).read())
    u = r.get("usage", {})
    with _ulock:
        _usage["out"] += u.get("completion_tokens", 0)
        _usage["hit"] += u.get("prompt_cache_hit_tokens", 0)
        _usage["miss"] += u.get("prompt_cache_miss_tokens", u.get("prompt_tokens", 0))
    m = r["choices"][0]["message"]
    return (m.get("reasoning_content") or "").strip(), (m.get("content") or "").strip()


def spend():
    c = _usage["out"] / 1e6 * 0.28 + _usage["miss"] / 1e6 * 0.14 + _usage["hit"] / 1e6 * 0.0028
    return f"${c:.2f} (out {_usage['out']/1e6:.1f}M tok)"


# ---- assembly templates (mated by construction) ---------------------------------------------------------
def assembly_grid():
    G = []
    for t in (16, 24, 32, 48):                              # gear seated on a plate boss
        for w in (50, 70):
            G.append({"parts": [
                {"id": "plate", "type": "plate", "material": "aluminum",
                 "params": {"width": w, "depth": w, "thick": 6, "boss_d": 12, "boss_h": 4,
                            "bolt_d": 5, "bolt_circle": w - 16, "bolt_count": 4}},
                {"id": "gear", "type": "spur_gear", "material": "steel",
                 "params": {"module": 1, "teeth": t, "width": 6, "bore_d": 12}}],
                "mates": [{"a": "plate", "b": "gear", "intent": "seat_on"}]})
    for od in (14, 18, 24):                                 # plate -> spacer -> gear stack
        for t in (20, 36):
            G.append({"parts": [
                {"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
                {"id": "sp", "type": "spacer", "material": "steel",
                 "params": {"outer_d": od, "bore_d": 6, "height": 12}},
                {"id": "gear", "type": "spur_gear", "material": "steel",
                 "params": {"module": 1, "teeth": t, "width": 6, "bore_d": 12}}],
                "mates": [{"a": "plate", "a_joint": "mount", "b": "sp", "b_joint": "bottom", "type": "coincident"},
                          {"a": "sp", "a_joint": "top", "b": "gear", "b_joint": "bore_base", "type": "coincident"}]})
    for sd in (4, 5, 6):                                    # bolt fastened in a plate hole (+washer under head)
        G.append({"parts": [
            {"id": "plate", "type": "plate", "material": "aluminum", "params": {"bolt_d": sd + 0.5}},
            {"id": "bolt", "type": "bolt", "material": "steel",
             "params": {"shank_d": sd, "length": 16, "head_d": round(sd * 1.7, 1), "head_h": round(sd * 0.7, 1)}}],
            "mates": [{"a": "plate", "b": "bolt", "intent": "fasten"}]})
    for za, zb in ((16, 24), (20, 40), (18, 54), (24, 24)):  # meshed gear pairs
        for m in (1, 1.5):
            G.append({"parts": [
                {"id": "g1", "type": "spur_gear", "material": "steel",
                 "params": {"module": m, "teeth": za, "width": 6, "bore_d": 8}},
                {"id": "g2", "type": "spur_gear", "material": "steel",
                 "params": {"module": m, "teeth": zb, "width": 6, "bore_d": 8}}],
                "mates": [{"a": "g1", "a_joint": "bore_base", "b": "g2", "b_joint": "bore_base", "type": "mesh"}]})
    for od in (12, 16, 22):                                 # stacked spacers (2-3)
        for n in (2, 3):
            parts = [{"id": f"s{i}", "type": "spacer", "material": "steel",
                      "params": {"outer_d": od, "bore_d": 5, "height": 8}} for i in range(n)]
            mates = [{"a": f"s{i}", "a_joint": "top", "b": f"s{i+1}", "b_joint": "bottom", "type": "coincident"}
                     for i in range(n - 1)]
            G.append({"parts": parts, "mates": mates})
    for d in (8, 10):                                       # shaft seated into a coupling bore
        for ln in (50, 90):
            G.append({"parts": [
                {"id": "cpl", "type": "coupling", "material": "steel",
                 "params": {"outer_d": 25, "bore_d": d, "length": 30, "set_screw_d": 4}},
                {"id": "sh", "type": "shaft", "material": "steel", "params": {"diameter": d, "length": ln}}],
                "mates": [{"a": "cpl", "a_joint": "end_a", "b": "sh", "b_joint": "base", "type": "coincident"}]})
    for od in (16, 22):                                     # washer seated on plate boss
        G.append({"parts": [
            {"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
            {"id": "w", "type": "washer", "material": "steel",
             "params": {"outer_d": od, "bore_d": 8, "thick": 2}}],
            "mates": [{"a": "plate", "b": "w", "intent": "seat_on"}]})
    return G


def structure_summary(prog):
    """Human-readable structure (parts + mates) for captioning an ASSEMBLY faithfully."""
    ps = [{"id": p["id"], "type": p["type"], "params": p.get("params", {})} for p in prog["parts"]]
    ms = [(m.get("intent") or m.get("type", "?"), m["a"], m["b"]) for m in prog.get("mates", [])]
    return json.dumps({"parts": ps, "structure": [f"{b} {rel} {a}" for rel, a, b in ms]})


CAP_ASM = ("You caption a known mechanical ASSEMBLY as a natural-language request someone would type. {style} "
           "The request must describe the PARTS (with key dims, mm) AND the STRUCTURE (what attaches/seats/"
           'meshes to what). Parts+structure follow. Output ONLY JSON: {{"desc":"..."}}.')


def deterministic_facts(prog):
    """Machine-TRUE facts to weave into reasoning traces (computed, not imagined)."""
    facts = []
    gears = {p["id"]: p["params"] for p in prog["parts"] if p["type"] in ("spur_gear", "helical_gear")}
    for pid, pp in gears.items():
        facts.append(f"{pid}: pitch diameter = module*teeth = {pp['module']}*{pp['teeth']} = "
                     f"{round(pp['module']*pp['teeth'], 2)} mm")
    for m in prog.get("mates", []):
        if (m.get("type") == "mesh" or m.get("intent") == "mesh") and m["a"] in gears and m["b"] in gears:
            ga, gb = gears[m["a"]], gears[m["b"]]
            cd = ga["module"] * (ga["teeth"] + gb["teeth"]) / 2
            facts.append(f"mesh {m['a']}-{m['b']}: center distance = m*(z1+z2)/2 = {cd} mm; "
                         f"ratio = {gb['teeth']}/{ga['teeth']} = {round(gb['teeth']/ga['teeth'], 3)}")
    for p in prog["parts"]:
        pp = p.get("params", {})
        if "bore_d" in pp and "outer_d" in pp and pp["bore_d"]:
            facts.append(f"{p['id']}: bore {pp['bore_d']} < outer {pp['outer_d']} ✓")
    return facts


# ---- stages ---------------------------------------------------------------------------------------------
def _pool():
    return ProcessPoolExecutor(max_workers=32, mp_context=mp.get_context("spawn"))


def _gen(desc):
    try:
        _r, c = _ds(ASM_GEN_SYS, desc, 0.2)
        prog = json.loads(c)
        return prog if isinstance(prog, dict) and prog.get("parts") else None
    except Exception:  # noqa: BLE001
        return None


def _cap(prog, style, temp=0.7):
    try:
        _r, c = _ds(CAP_ASM.format(style=STYLES[style]), structure_summary(prog), temp)
        return (json.loads(c).get("desc") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def s0asm():
    grid = assembly_grid()
    asms = random.Random(7).sample(grid, min(40, len(grid)))
    with _pool() as ex:
        ref = {i: s for i, s, _e in ex.map(_build_and_sig, list(enumerate(asms)))}
        jobs = [(i, st) for i in range(len(asms)) for st in STYLES]
        with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            caps = list(tex.map(lambda j: (j[0], j[1], _cap(asms[j[0]], j[1])), jobs))
        with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            regs = list(tex.map(lambda c: (c[0], c[1], _gen(c[2]) if c[2] else None), caps))
        live = [(i, st, p) for i, st, p in regs if p]
        sigs = list(ex.map(_build_and_sig, [(k, live[k][2]) for k in range(len(live))]))
    per, ok = Counter(), Counter()
    for k, sig, _e in sigs:
        i, st, _p = live[k]
        per[st] += 1
        if sig and _match(sig, ref.get(i)):
            ok[st] += 1
    table = {st: round(ok[st] / per[st], 3) if per[st] else 0.0 for st in STYLES}
    json.dump({"yields": table}, open(f"{DS_DIR}/s0asm.json", "w"), indent=2)
    print("=== ASSEMBLY S0 per-style round-trip yield ===")
    for st, y in table.items():
        print(f"  {st:11} {y:.0%}")
    print(f"spend: {spend()}")


TRACE_A = (ASM_GEN_SYS + "\nFIRST reason carefully step by step: identify the part(s), extract every stated "
           "dimension, choose params, choose the mates/structure, and sanity-check the math. THEN output "
           "ONLY the final JSON program.")
TRACE_B = ("You are a mechanical engineer writing the WORKED DERIVATION from a request to a known-correct "
           "constraint-kit program: identify parts, extract dims, map to params, justify mates, include the "
           "arithmetic. 150-350 words, first person, no headers. You MAY use these verified facts: {facts}. "
           'Output ONLY JSON: {{"trace":"..."}}.')


def s5(n=2000, gold_frac=0.25, asm_frac=0.45):
    geoms = geometry_grid()
    asms = assembly_grid()
    rng = random.Random(13)
    picks = [rng.choice(asms) if rng.random() < asm_frac else rng.choice(geoms) for _ in range(n)]
    with _pool() as ex:
        refs = {i: s for i, s, _e in ex.map(_build_and_sig, list(enumerate(picks)))}
        styles = list(STYLES)
        with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            caps = list(tex.map(lambda i: _cap(picks[i], styles[i % 5]), range(n)))
        rows, gold_jobs = [], []
        for i, c in enumerate(caps):
            if not c or refs.get(i) is None:
                continue
            (gold_jobs if rng.random() < gold_frac else rows).append((i, c))
        # tier A: thinking-on, verifier-filtered (reasoning in reasoning_content, program in content)
        def gold(j):
            i, c = j
            try:
                rsn, content = _ds(TRACE_A, c, 0.3, thinking=True, max_tokens=2600)
                prog = json.loads(content)
                return i, c, rsn, prog
            except Exception:  # noqa: BLE001
                return i, c, None, None
        with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            gouts = list(tex.map(gold, gold_jobs))
        gl = [(i, c, r, p) for i, c, r, p in gouts if r and p]
        gsigs = list(ex.map(_build_and_sig, [(k, gl[k][3]) for k in range(len(gl))]))
        kept = []
        for k, sig, _e in gsigs:
            i, c, r, p = gl[k]
            if sig and _match(sig, refs[i]):
                kept.append({"caption": c, "trace": r, "program": p, "tier": "gold"})
        # tier B: answer-conditioned backfill (caption + KNOWN program -> derivation)
        def backfill(j):
            i, c = j
            facts = "; ".join(deterministic_facts(picks[i])) or "none"
            try:
                _r, content = _ds(TRACE_B.format(facts=facts),
                                  json.dumps({"request": c, "program": picks[i]}), 0.4, max_tokens=900)
                tr = (json.loads(content).get("trace") or "").strip()
                return {"caption": c, "trace": tr, "program": picks[i], "tier": "backfill"} if tr else None
            except Exception:  # noqa: BLE001
                return None
        with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            kept += [r for r in tex.map(backfill, rows) if r]
    with open(f"{DS_DIR}/traces.jsonl", "a") as fh:
        for r in kept:
            fh.write(json.dumps(r) + "\n")
    gold_n = sum(1 for r in kept if r["tier"] == "gold")
    print(f"s5: {len(kept)} traces kept ({gold_n} gold verifier-filtered of {len(gl)} attempted, "
          f"{len(kept)-gold_n} backfill) | spend: {spend()}")


MUTATORS = {
    "unknown-anchor":   lambda p: _mut_anchor(p),
    "unknown-type":     lambda p: _mut_type(p),
    "bad-mate-ref":     lambda p: _mut_ref(p),
    "bad-mate-type":    lambda p: _mut_matetype(p),
    "garbage-param":    lambda p: _mut_param(p),
}


def _deep(p):
    return json.loads(json.dumps(p))


def _mut_anchor(p):
    p = _deep(p)
    if p.get("mates") and "b_joint" in p["mates"][0]:
        p["mates"][0]["b_joint"] = "shank"
        return p
    return None


def _mut_type(p):
    p = _deep(p)
    p["parts"][0]["type"] = p["parts"][0]["type"][:-1] + "rr"
    return p


def _mut_ref(p):
    p = _deep(p)
    if p.get("mates"):
        p["mates"][0]["b"] = "nonexistent_part"
        return p
    return None


def _mut_matetype(p):
    p = _deep(p)
    if p.get("mates") and "type" in p["mates"][0]:
        p["mates"][0]["type"] = "weld"
        return p
    return None


def _mut_param(p):
    p = _deep(p)
    pp = p["parts"][0].setdefault("params", {})
    if pp:
        k = sorted(pp)[0]
        pp[k] = -abs(pp[k]) if isinstance(pp[k], (int, float)) else "garbage"
        return p
    return None


FIX_SYS = ("You are a mechanical engineer using a CAD DSL with a checker. Given the request, your previous "
           "(broken) program, the checker diagnostics, and the known-correct program: write the brief "
           "diagnosis-and-fix reasoning (60-150 words, first person: read the diagnostic, identify the "
           'mistake, state the fix). Output ONLY JSON: {{"trace":"..."}}.').replace("{{", "{").replace("}}", "}")


def s6(n=800):
    from constraint_kit import dsl
    asms, geoms = assembly_grid(), geometry_grid()
    rng = random.Random(17)
    rows = []
    cands = []
    while len(cands) < n:
        prog = rng.choice(asms) if rng.random() < 0.6 else rng.choice(geoms)
        mname = rng.choice(list(MUTATORS))
        bad = MUTATORS[mname](prog)
        if bad is None:
            continue
        chk = dsl.check(bad)
        if chk["ok"]:
            continue                                        # mutation didn't actually break it -> skip
        diags = [d for d in chk["diagnostics"] if d["severity"] == "error"][:3]
        cands.append((prog, bad, diags, mname))
    with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
        caps = list(tex.map(lambda c: _cap(c[0], random.choice(list(STYLES))), cands))

        def fix(j):
            (prog, bad, diags, mname), cap = j
            if not cap:
                return None
            try:
                _r, content = _ds(FIX_SYS, json.dumps({"request": cap, "broken_program": bad,
                                                       "diagnostics": diags, "correct_program": prog}),
                                  0.4, max_tokens=600)
                tr = (json.loads(content).get("trace") or "").strip()
            except Exception:  # noqa: BLE001
                return None
            if not tr:
                return None
            return {"messages": [
                {"role": "user", "content": cap},
                {"role": "assistant", "content": f"<think>\nMapping the request to parts and mates.\n</think>\n\n"
                                                 f"{json.dumps(bad)}"},
                {"role": "user", "content": "[CHECKER] " + json.dumps(diags)},
                {"role": "assistant", "content": f"<think>\n{tr}\n</think>\n\n{json.dumps(prog)}"},
            ], "loss_mask_turns": [1], "meta": {"mutator": mname}}
        rows = [r for r in tex.map(fix, zip(cands, caps)) if r]
    with open(f"{DS_DIR}/trajectories.jsonl", "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    by = Counter(r["meta"]["mutator"] for r in rows)
    print(f"s6: {len(rows)} trajectories | per class: {dict(by)} | spend: {spend()}")


def assemble():
    traces = [json.loads(l) for l in open(f"{DS_DIR}/traces.jsonl")] if os.path.exists(f"{DS_DIR}/traces.jsonl") else []
    trajs = [json.loads(l) for l in open(f"{DS_DIR}/trajectories.jsonl")] if os.path.exists(f"{DS_DIR}/trajectories.jsonl") else []
    direct = [json.loads(l) for l in open(f"{OUT}/dsl_corpus.jsonl")]
    rows = []
    for t in traces:
        rows.append({"messages": [
            {"role": "user", "content": t["caption"]},
            {"role": "assistant", "content": f"<think>\n{t['trace']}\n</think>\n\n{json.dumps(t['program'])}"}],
            "meta": {"kind": "reasoning", "tier": t["tier"]}})
    for t in trajs:
        rows.append({"messages": t["messages"], "loss_mask_turns": t["loss_mask_turns"],
                     "meta": {"kind": "trajectory", **t["meta"]}})
    n_reason = len(rows)
    max_direct = n_reason // 3                               # ceil to the 75/25 rule
    rng = random.Random(23)
    for d in rng.sample(direct, min(max_direct, len(direct))):
        rows.append({"messages": [
            {"role": "user", "content": d["description"]},
            {"role": "assistant", "content": f"<think>\n\n</think>\n\n{json.dumps(d['program'])}"}],
            "meta": {"kind": "direct"}})
    rng.shuffle(rows)
    cut = max(1, len(rows) // 50)                            # 2% val
    with open(f"{DS_DIR}/val.jsonl", "w") as fh:
        for r in rows[:cut]:
            fh.write(json.dumps(r) + "\n")
    with open(f"{DS_DIR}/train.jsonl", "w") as fh:
        for r in rows[cut:]:
            fh.write(json.dumps(r) + "\n")
    kinds = Counter(r["meta"]["kind"] for r in rows)
    print(f"assemble: {len(rows)} rows -> train {len(rows)-cut} / val {cut} | mix: {dict(kinds)} "
          f"({100*(kinds['reasoning']+kinds['trajectory'])//len(rows)}% reasoning)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "s0asm"
    if cmd == "s0asm":
        s0asm()
    elif cmd == "s5":
        s5(int(sys.argv[2]) if len(sys.argv) > 2 else 2000)
    elif cmd == "s6":
        s6(int(sys.argv[2]) if len(sys.argv) > 2 else 800)
    elif cmd == "assemble":
        assemble()
