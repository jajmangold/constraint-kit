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




# ---- v1.1 issue-hunt additions --------------------------------------------------------------------------
# The TRAINING system prompt — same at train and deploy. Includes the decline rule + vitamin forms + sets,
# so decline/vitamin/set rows are coherent, and gold traces that reference "the instructions" refer to text
# that is actually in context (60% of gold traces do).
TRAIN_SYS = (ASM_GEN_SYS +
    "\nHONESTY: if the request names a part, variant, or feature this vocabulary cannot express "
    "(e.g. jaw/flexible coupling, living or concealed hinge, spiral bevel, worm WHEEL, tapered/needle/"
    "thrust bearing, cam, spring, universal joint, keyway, spline, timing/GT2 pulley), do NOT substitute "
    'a simpler part - output ONLY {"unsupported":"<what cannot be expressed>"}. '
    "\nVITAMINS (rendered library parts) - emit {\"type\":\"vitamin\",\"params\":{\"scad\":\"<call>\",\"name\":...,\"fn\":48}} with: "
    "nema_stepper_motor(size=17|23, h=<mm>, shaft_len=20); | rack(pitch=<mm>, teeth=N, height=8, thickness=6); | "
    "worm(circ_pitch=5, d=30, l=40); | bevel_gear(teeth=N, mate_teeth=N, mod=M, face_width=8, spiral=0); | "
    "union(){knuckle_hinge(length=L, segs=5, offset=5, knuckle_diam=6, in_place=true); zrot(180) knuckle_hinge(length=L, segs=5, offset=5, knuckle_diam=6, inner=true, in_place=true);} "
    "\nA 'set/pack of N' identical parts = N parts and mates []. ")

OOV_TAXONOMY = [
    ("jaw coupling", "only a RIGID coupling is modeled; jaw/spider elastomer couplings are not"),
    ("flexible beam coupling", "flexible couplings are not modeled"),
    ("living hinge", "only knuckle hinges exist; a living hinge is a flexure, not expressible"),
    ("concealed european hinge", "only knuckle hinges are modeled"),
    ("spiral bevel gear", "only STRAIGHT bevel gears render; spiral/hypoid are not modeled"),
    ("worm wheel", "only the worm SCREW is modeled, not the mating wheel"),
    ("tapered roller bearing", "only deep-groove ball bearings are modeled"),
    ("needle bearing", "not modeled"),
    ("thrust bearing", "not modeled"),
    ("cam and follower", "cams are not modeled"),
    ("compression spring", "springs are not modeled"),
    ("torsion spring", "springs are not modeled"),
    ("universal joint", "not modeled"),
    ("rod end bearing", "not modeled"),
    ("o-ring", "seals/gaskets are not modeled"),
    ("circlip retaining ring", "not modeled"),
    ("GT2 timing pulley", "only smooth flanged pulleys are modeled; toothed/timing are not"),
    ("keyed shaft with keyway", "shafts have no keyway feature"),
    ("splined shaft", "splines are not modeled"),
    ("cable gland", "not modeled"),
]

DECLINE_REQ = ('Write {k} varied, natural one-line requests asking for: {term}. Mix dimensioned and vague. '
               'Output ONLY JSON: {{"requests":["..."]}}.')
DECLINE_TRACE = ("You are a careful CAD assistant. The vocabulary CANNOT express the requested item ({reason}). "
                 "Write a brief first-person reasoning (40-100 words): identify what is asked, note the closest "
                 "modeled part and why substituting it would be wrong, conclude with declining. "
                 'Output ONLY JSON: {{"trace":"..."}}.')


def declines(n=2500):
    per = max(1, n // len(OOV_TAXONOMY) // 20)
    rows = []
    with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
        def reqs(item):
            term, reason = item
            out = []
            for _ in range(per):
                try:
                    _r, c = _ds(DECLINE_REQ.format(k=20, term=term), term, 0.9)
                    out += [(q, term, reason) for q in json.loads(c).get("requests", []) if isinstance(q, str)]
                except Exception:
                    pass
            return out
        allreq = [r for batch in tex.map(reqs, OOV_TAXONOMY) for r in batch][:n]
        def mk(j):
            q, term, reason = j
            try:
                _r, c = _ds(DECLINE_TRACE.format(reason=reason), q, 0.5, max_tokens=400)
                tr = (json.loads(c).get("trace") or "").strip()
                return {"caption": q, "trace": tr, "decline": reason, "key": "oov:" + term} if tr else None
            except Exception:
                return None
        rows = [r for r in tex.map(mk, allreq) if r]
    with open(f"{DS_DIR}/declines.jsonl", "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"declines: {len(rows)} rows | spend: {spend()}")


AMBIG_CAP = ("Write a natural one-line request for this part mentioning ONLY the stated values below "
             "(leave everything else unspecified, e.g. 'a spur gear with 24 teeth'). "
             'Output ONLY JSON: {{"desc":"..."}}.')
AMBIG_TRACE = ("Write brief first-person reasoning (50-120 words): the request states only {stated}; choose "
               "sensible defaults for the rest ({defaults}) and map to the program params. "
               'Output ONLY JSON: {{"trace":"..."}}.')


def ambiguous(n=1500):
    from constraint_kit import intent as ck_intent
    kinds = ["spur_gear", "spacer", "washer", "shaft", "panel", "coupling", "pulley", "bolt", "nut", "link"]
    rng = random.Random(31)
    jobs = []
    for _ in range(n):
        k = rng.choice(kinds)
        dfl = {a: b for a, b in ck_intent._param_defaults(k).items() if isinstance(b, (int, float))}
        stated_keys = rng.sample(sorted(dfl), min(len(dfl), rng.choice((1, 2))))
        stated = {sk: round(dfl[sk] * rng.choice((0.8, 1.0, 1.5, 2.0)), 1) for sk in stated_keys}
        params = {**dfl, **stated}
        prog = {"parts": [{"id": "p", "type": k, "material": "steel", "params": params}], "mates": []}
        jobs.append((k, stated, dfl, prog))
    with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
        def mk(j):
            k, stated, dfl, prog = j
            try:
                _r, c = _ds(AMBIG_CAP, json.dumps({"type": k, "stated": stated}), 0.8)
                cap = (json.loads(c).get("desc") or "").strip()
                if not cap:
                    return None
                _r, c2 = _ds(AMBIG_TRACE.format(stated=json.dumps(stated),
                                                defaults=json.dumps({a: b for a, b in dfl.items() if a not in stated})),
                             cap, 0.4, max_tokens=400)
                tr = (json.loads(c2).get("trace") or "").strip()
                return {"caption": cap, "trace": tr, "program": prog, "tier": "ambiguous"} if tr else None
            except Exception:
                return None
        rows = [r for r in tex.map(mk, jobs) if r]
    with _pool() as ex:    # verify the default-resolved programs actually build
        sigs = list(ex.map(_build_and_sig, [(i, rows[i]["program"]) for i in range(len(rows))]))
    rows = [rows[i] for i, s, _e in sigs if s]
    with open(f"{DS_DIR}/traces.jsonl", "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"ambiguous: {len(rows)} rows | spend: {spend()}")


def vitamins_sets(n_vit=600, n_set=300):
    from constraint_kit.vitamins import VITAMIN_CATALOG
    rng = random.Random(37)
    vit_progs = []
    for cname, spec in VITAMIN_CATALOG.items():
        for _ in range(6):
            p = dict(spec["defaults"])
            for key in list(p):
                if isinstance(p[key], (int, float)) and key not in ("segs", "fn"):
                    p[key] = round(p[key] * rng.choice((0.8, 1.0, 1.4)))
            try:
                scad = spec["scad"].format(**p)
            except Exception:
                continue
            vit_progs.append((cname, {"parts": [{"id": "v", "type": "vitamin", "material": "steel",
                                                 "params": {"scad": scad, "name": cname, "fn": 48}}], "mates": []}))
    set_progs = []
    for _ in range(60):
        k = rng.choice(["spur_gear", "spacer", "washer", "bolt", "link"])
        nn = rng.choice((2, 3, 4, 5))
        base = rng.choice(geometry_grid())
        if base["parts"][0]["type"] != k:
            continue
        pp = base["parts"][0]["params"]
        set_progs.append((f"set of {nn} {k}", {"parts": [{"id": f"p{i}", "type": k, "material": "steel",
                                                          "params": pp} for i in range(nn)], "mates": []}))
    with _pool() as ex:
        vsig = list(ex.map(_build_and_sig, [(i, vit_progs[i][1]) for i in range(len(vit_progs))]))
        vit_ok = [vit_progs[i] for i, s, _e in vsig if s]
        ssig = list(ex.map(_build_and_sig, [(i, set_progs[i][1]) for i in range(len(set_progs))]))
        set_ok = [set_progs[i] for i, s, _e in ssig if s]
    rows = []
    with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
        def mkv(j):
            label, prog = j
            try:
                _r, c = _ds(CAP_ASM.format(style=STYLES[random.choice(list(STYLES))]),
                            structure_summary(prog), 0.8)
                cap = (json.loads(c).get("desc") or "").strip()
                if not cap:
                    return None
                facts = "; ".join(deterministic_facts(prog)) or "none"
                _r, c2 = _ds(TRACE_B.format(facts=facts),
                             json.dumps({"request": cap, "program": prog}), 0.4, max_tokens=700)
                tr = (json.loads(c2).get("trace") or "").strip()
                return {"caption": cap, "trace": tr, "program": prog, "tier": "vitamin"} if tr else None
            except Exception:
                return None
        per_v = max(1, n_vit // max(len(vit_ok), 1))
        vrows = [r for r in tex.map(mkv, [v for v in vit_ok for _ in range(per_v)]) if r]
        per_s = max(1, n_set // max(len(set_ok), 1))
        srows = [r for r in tex.map(mkv, [s for s in set_ok for _ in range(per_s)]) if r]
        for r in srows:
            r["tier"] = "set"
        rows = vrows + srows
    with open(f"{DS_DIR}/traces.jsonl", "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"vitamins+sets: {len(vrows)} vitamin, {len(srows)} set rows | spend: {spend()}")


def directs(n=8000):
    from campaign import caption_batch
    geoms = geometry_grid() + assembly_grid()
    rng = random.Random(41)
    jobs = [(rng.choice(geoms), rng.choice(list(STYLES))) for _ in range(max(1, n // 20))]
    rows = []
    with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
        with _pool() as ex:
            sig_cache = {}
            for (prog, _st), caps in zip(jobs, tex.map(lambda j: caption_batch(j[0], j[1], 20), jobs)):
                key = json.dumps(prog, sort_keys=True)
                for c in caps:
                    rows.append({"description": c, "program": prog})
    with open(f"{DS_DIR}/directs.jsonl", "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"directs: {len(rows)} rows | spend: {spend()}")


def extras():
    declines()
    ambiguous()
    vitamins_sets()
    directs()


def _geom_key(r):
    if "decline" in r:
        return r["key"]
    return json.dumps(r.get("program", {}), sort_keys=True)


def assemble():
    """v2 (issue-hunt fixes): the SAME system prompt on every row (so 'the instructions' references in 60%
    of gold traces are coherent + decline/vitamin/set behavior is anchored); geometry-KEYED ~5% val split
    (random row split leaked near-duplicates of train geometries into val -> memorization metric); declines
    + direct top-up included; honest stats incl. unique-geometry count."""
    def load(p):
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []
    traces = load(f"{DS_DIR}/traces.jsonl")
    trajs = load(f"{DS_DIR}/trajectories.jsonl")
    decls = load(f"{DS_DIR}/declines.jsonl")
    direct = load(f"{OUT}/dsl_corpus.jsonl") + load(f"{DS_DIR}/directs.jsonl")
    rows = []
    for t in traces:
        rows.append({"messages": [
            {"role": "system", "content": TRAIN_SYS},
            {"role": "user", "content": t["caption"]},
            {"role": "assistant", "content": f"<think>\n{t['trace']}\n</think>\n\n{json.dumps(t['program'])}"}],
            "meta": {"kind": "reasoning", "tier": t["tier"]}, "_key": _geom_key(t)})
    for t in trajs:
        msgs = [{"role": "system", "content": TRAIN_SYS}] + t["messages"]
        masks = [i + 1 for i in t["loss_mask_turns"]]        # shift for the prepended system turn
        rows.append({"messages": msgs, "loss_mask_turns": masks,
                     "meta": {"kind": "trajectory", **t["meta"]},
                     "_key": _geom_key({"program": json.loads(t["messages"][-1]["content"].split("</think>")[-1].strip())})})
    for dterm in decls:
        rows.append({"messages": [
            {"role": "system", "content": TRAIN_SYS},
            {"role": "user", "content": dterm["caption"]},
            {"role": "assistant", "content": f"<think>\n{dterm['trace']}\n</think>\n\n"
                                             + json.dumps({"unsupported": dterm["decline"]})}],
            "meta": {"kind": "decline"}, "_key": dterm["key"]})
    n_reason = len(rows)
    rng = random.Random(23)
    seen_caps = set()
    direct_rows = []
    for d in rng.sample(direct, len(direct)):
        c = " ".join(d["description"].lower().split())
        if c in seen_caps:
            continue
        seen_caps.add(c)
        direct_rows.append({"messages": [
            {"role": "system", "content": TRAIN_SYS},
            {"role": "user", "content": d["description"]},
            {"role": "assistant", "content": f"<think>\n\n</think>\n\n{json.dumps(d['program'])}"}],
            "meta": {"kind": "direct"}, "_key": _geom_key(d)})
        if len(direct_rows) >= n_reason // 3:                # the 75/25 rule
            break
    rows += direct_rows
    # geometry-keyed split: hold out ~5% of KEYS entirely (no leakage of a geometry across the split)
    keys = sorted({r["_key"] for r in rows})
    rng.shuffle(keys)
    val_keys = set(keys[:max(1, len(keys) // 20)])
    train, val = [], []
    for r in rows:
        (val if r.pop("_key") in val_keys else train).append(r)
    rng.shuffle(train)
    with open(f"{DS_DIR}/val.jsonl", "w") as fh:
        for r in val:
            fh.write(json.dumps(r) + "\n")
    with open(f"{DS_DIR}/train.jsonl", "w") as fh:
        for r in train:
            fh.write(json.dumps(r) + "\n")
    kinds = Counter(r["meta"]["kind"] for r in rows)
    n_r = kinds["reasoning"] + kinds["trajectory"] + kinds["decline"]
    print(f"assemble v2: {len(rows)} rows -> train {len(train)} / val {len(val)} (split by {len(keys)} geometry keys, "
          f"{len(val_keys)} held out) | mix: {dict(kinds)} ({100*n_r//len(rows)}% reasoning)")


# ---- Gemma 4 pivot --------------------------------------------------------------------------------------
# Emission is STRUCTURED (separate `reasoning` field + per-message `train` flags), NOT baked template
# tokens: the training script applies the official `gemma-4-thinking` chat template, so template details
# (<|think|> forms) live in ONE place. Trajectory bad-turns carry train:false (Unsloth/Axolotl honor it).
def _g4_row(user_content, reasoning, answer, kind, key, extra_msgs=None):
    msgs = [{"role": "system", "content": TRAIN_SYS}]
    if extra_msgs:
        msgs += extra_msgs
    msgs.append({"role": "user", "content": user_content})
    msgs.append({"role": "assistant", "reasoning": reasoning or "", "content": answer, "train": True})
    return {"messages": msgs, "meta": {"kind": kind}, "_key": key}


def assemble_gemma():
    def load(p):
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []
    rows = []
    for t in load(f"{DS_DIR}/traces.jsonl"):
        rows.append(_g4_row(t["caption"], t["trace"], json.dumps(t["program"]),
                            "reasoning", _geom_key(t)))
        rows[-1]["meta"]["tier"] = t["tier"]
    for t in load(f"{DS_DIR}/trajectories.jsonl"):
        m = t["messages"]
        prog = m[-1]["content"].split("</think>")[-1].strip()
        bad = m[1]["content"].split("</think>")[-1].strip()
        fix_think = m[-1]["content"].split("<think>")[-1].split("</think>")[0].strip()
        msgs = [{"role": "system", "content": TRAIN_SYS},
                {"role": "user", "content": m[0]["content"]},
                {"role": "assistant", "reasoning": "", "content": bad, "train": False},  # masked bad attempt
                {"role": "user", "content": m[2]["content"]},
                {"role": "assistant", "reasoning": fix_think, "content": prog, "train": True}]
        rows.append({"messages": msgs, "meta": {"kind": "trajectory", **t.get("meta", {})},
                     "_key": _geom_key({"program": json.loads(prog)})})
    for d in load(f"{DS_DIR}/declines.jsonl"):
        rows.append(_g4_row(d["caption"], d["trace"], json.dumps({"unsupported": d["decline"]}),
                            "decline", d["key"]))
    n_reason = len(rows)
    rng = random.Random(23)
    seen, n_direct = set(), 0
    pool = load(f"{OUT}/dsl_corpus.jsonl") + load(f"{DS_DIR}/directs.jsonl")
    for d in rng.sample(pool, len(pool)):
        c = " ".join(d["description"].lower().split())
        if c in seen or n_direct >= n_reason // 3:
            continue
        seen.add(c)
        rows.append(_g4_row(d["description"], "", json.dumps(d["program"]), "direct", _geom_key(d)))
        n_direct += 1
    for v in load(f"{DS_DIR}/visual_rows.jsonl"):                 # S7 multimodal (image content arrays)
        v["_key"] = v["meta"].get("key", "visual")
        rows.append(v)
    keys = sorted({r["_key"] for r in rows})
    rng.shuffle(keys)
    val_keys = set(keys[:max(1, len(keys) // 20)])
    train = [r for r in rows if r["_key"] not in val_keys]
    val = [r for r in rows if r["_key"] in val_keys]
    for r in rows:
        r.pop("_key", None)
    rng.shuffle(train)
    with open(f"{DS_DIR}/train_g4.jsonl", "w") as fh:
        for r in train:
            fh.write(json.dumps(r) + "\n")
    with open(f"{DS_DIR}/val_g4.jsonl", "w") as fh:
        for r in val:
            fh.write(json.dumps(r) + "\n")
    kinds = Counter(r["meta"]["kind"] for r in rows)
    print(f"assemble_gemma: {len(rows)} rows -> train {len(train)} / val {len(val)} | mix: {dict(kinds)}")


# ---- S7: multimodal render-pairs + visual-correction trajectories (ZERO DeepSeek, pure CPU) --------------
def s7_worklist(n_pairs=800, n_corr=300):
    """Build + export GLBs for distinct geometries (reusing existing captions/traces) + param-MUTATED
    variants for visual corrections (a visible wrong build + the deterministic diff). Host then renders
    (drivers/render_batch.py); s7_rows assembles the multimodal rows."""
    from constraint_kit import builder
    rng = random.Random(43)
    by_key = {}
    for l in open(f"{DS_DIR}/traces.jsonl"):
        t = json.loads(l)
        if t["tier"] in ("gold", "backfill") and "program" in t:
            by_key.setdefault(_geom_key(t), t)
    picks = rng.sample(sorted(by_key), min(n_pairs + n_corr, len(by_key)))
    items = []
    for i, k in enumerate(picks):
        t = by_key[k]
        item = {"id": f"s7_{i}", "caption": t["caption"], "trace": t["trace"], "program": t["program"]}
        if i < n_corr:                                       # mutate a VISIBLE param for the correction set
            mut = json.loads(json.dumps(t["program"]))
            pp = mut["parts"][0].get("params", {})
            tweaked = None
            for cand, fac in (("teeth", None), ("outer_d", 1.5), ("length", 1.6), ("width", 2.0), ("height", 1.8)):
                if cand in pp and isinstance(pp[cand], (int, float)):
                    old = pp[cand]
                    pp[cand] = (old + 8) if cand == "teeth" else round(old * fac, 1)
                    tweaked = (cand, old, pp[cand])
                    break
            if tweaked:
                item["mutant"] = mut
                item["diff"] = f"the built part has {tweaked[0]}={tweaked[2]} but the target shows {tweaked[0]}={tweaked[1]}"
        items.append(item)
    ok = 0
    with open(f"{DS_DIR}/s7_worklist.jsonl", "w") as fh:
        for it in items:
            try:
                assy, _rep = builder.build_assembly(it["program"])
                builder.export(assy, f"{OUT}/{it['id']}")
                it["glb"] = f"{OUT}/{it['id']}.glb"
                if "mutant" in it:
                    massy, _r2 = builder.build_assembly(it["mutant"])
                    builder.export(massy, f"{OUT}/{it['id']}_bad")
                    it["mutant_glb"] = f"{OUT}/{it['id']}_bad.glb"
                fh.write(json.dumps(it) + "\n")
                ok += 1
            except Exception:  # noqa: BLE001
                continue
    print(f"s7_worklist: {ok}/{len(items)} exported (renders next: drivers/render_batch.py)")


def s7_rows():
    """Assemble multimodal rows from rendered worklist: image->CAD pairs + visual-correction trajectories
    (deterministic diff text; reuses the existing trace as the reasoning)."""
    rows = []
    for l in open(f"{DS_DIR}/s7_worklist.jsonl"):
        it = json.loads(l)
        png = it.get("png")
        if not png or not os.path.exists(png):
            continue
        prog = json.dumps(it["program"])
        key = _geom_key(it)
        rows.append({"messages": [
            {"role": "system", "content": TRAIN_SYS},
            {"role": "user", "content": [{"type": "image", "image": png},
                                         {"type": "text", "text": f"Reproduce this rendered part as a program. {it['caption']}"}]},
            {"role": "assistant", "reasoning": it["trace"], "content": prog, "train": True}],
            "meta": {"kind": "visual_pair", "key": key}})
        bpng = it.get("mutant_png")
        if bpng and os.path.exists(bpng) and "diff" in it:
            rows.append({"messages": [
                {"role": "system", "content": TRAIN_SYS},
                {"role": "user", "content": [{"type": "image", "image": png},
                                             {"type": "text", "text": f"Reproduce this rendered part as a program. {it['caption']}"}]},
                {"role": "assistant", "reasoning": "", "content": json.dumps(it["mutant"]), "train": False},
                {"role": "user", "content": [{"type": "image", "image": bpng},
                                             {"type": "text", "text": f"[RENDER OF YOUR BUILD] It does not match the target: {it['diff']}. Fix the program."}]},
                {"role": "assistant",
                 "reasoning": f"The rendered build differs from the target: {it['diff']}. I correct that parameter and keep everything else.",
                 "content": prog, "train": True}],
                "meta": {"kind": "visual_correction", "key": key}})
    with open(f"{DS_DIR}/visual_rows.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    k = Counter(r["meta"]["kind"] for r in rows)
    print(f"s7_rows: {len(rows)} multimodal rows | {dict(k)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "s0asm"
    if cmd == "s0asm":
        s0asm()
    elif cmd == "s5":
        s5(int(sys.argv[2]) if len(sys.argv) > 2 else 2000)
    elif cmd == "s6":
        s6(int(sys.argv[2]) if len(sys.argv) > 2 else 800)
    elif cmd == "extras":
        extras()
    elif cmd == "s7_worklist":
        s7_worklist()
    elif cmd == "s7_rows":
        s7_rows()
    elif cmd == "assemble_gemma":
        assemble_gemma()
    elif cmd == "assemble":
        assemble()
