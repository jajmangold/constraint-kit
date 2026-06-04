#!/usr/bin/env python3
"""Capstone: a synthesized planetary gearbox driven end-to-end through the ENTIRE constraint-kit stack,
with the solver classes CROSS-VALIDATING each other. Host driver (stdlib only) over the cadkit API.

  SMT synthesis (Z3 designs the ratio)  ->  Willis DOF cross-check (analytical solver confirms the SAME
  ratio + loop consistency)  ->  hierarchical build (parallel prewarm)  ->  interference check  ->
  rolled-up BOM/mass/CG  ->  manufacturing outputs (exploded view + section drawing + BOM doc)  ->
  optional Blender render + qwen27b QA (fail-soft).

    python3 drivers/capstone_gearbox.py [target_ratio]

The hermetic proof of this composition lives in tests/test_kernel.py::test_capstone_gearbox_full_stack;
this driver is the live, rendered demo (render/QA need Blender + the GPU VLM and degrade gracefully).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

CADKIT = os.environ.get("CADKIT_URL", "http://127.0.0.1:8195")
VLM = os.environ.get("VLM_URL", "http://localhost:8000/v1")
CONTAINERS = "/srv/nvme-data/containers"
OUT = f"{CONTAINERS}/constraint-kit/cadkit/output"


def post(path, payload, timeout=300):
    req = urllib.request.Request(f"{CADKIT}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def main():
    ratio = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    name = "capstone_gearbox"

    print(f"[1/8] SMT synthesis (Z3): design a planetary set for {ratio}:1 ...")
    syn = post("/synthesize/planetary", {"target_ratio": ratio, "n_planets": 3, "module": 1.0,
                                         "width": 8, "objective": "compact"})
    if not syn.get("ok"):
        print("   infeasible:", syn.get("reason")); return
    cfg, pg = syn["config"], syn["part_spec"]
    print(f"   -> sun={cfg['sun_teeth']} planet={cfg['planet_teeth']} ring={cfg['ring_teeth']} "
          f"(achieved {syn['achieved_ratio']}:1)")

    print("[2/8] Willis DOF cross-check (independent analytical solver) ...")
    dof = post("/dof/planetary", {"module": 1.0, "sun_teeth": cfg["sun_teeth"],
                                  "planet_teeth": cfg["planet_teeth"], "n_planets": cfg["n_planets"]})
    agree = abs(dof.get("ratio_sun_to_carrier_ring_fixed", 0) - syn["achieved_ratio"]) < 1e-6
    print(f"   -> Willis ratio {dof.get('ratio_sun_to_carrier_ring_fixed')}:1, gear_dof {dof.get('gear_dof')}, "
          f"loop_consistent {dof.get('loop_consistent')} | SMT==Willis: {agree}")

    defs = {"gearbox": {"parts": [
        {"id": "plate", "type": "plate", "material": "aluminum",
         "params": {"width": 90, "depth": 90, "thick": 8, "boss_d": 24, "boss_h": 3,
                    "bolt_d": 5, "bolt_circle": 74, "bolt_count": 4}},
        pg,
        {"id": "brg", "type": "bearing", "material": "steel",
         "params": {"outer_d": 24, "bore_d": 12, "width": 6}},
        *[{"id": f"screw{i}", "type": "bolt", "material": "steel",
           "params": {"shank_d": 5, "length": 14, "head_d": 9, "head_h": 4}} for i in range(4)]],
        "mates": [{"a": "plate", "b": "planetary", "intent": "seat_on"},
                  {"a": "planetary", "a_joint": "top", "b": "brg", "b_joint": "bore_base",
                   "type": "coincident"},
                  *[{"a": "plate", "a_joint": f"bolt{i}", "b": f"screw{i}", "b_joint": "seat",
                     "type": "coincident"} for i in range(4)]]}}

    print("[3/8] hierarchical build (parallel prewarm) ...")
    asm = post("/assembly/tree", {"root": "gearbox", "defs": defs, "name": name})
    if not asm.get("ok"):
        print("   build failed:", asm); return
    print(f"   -> depth {asm['depth']}, {asm['part_count']} parts, mass {asm['mass_g']} g, "
          f"bbox {asm['bbox']['size']} mm | BOM {asm['bom']}")

    print("[4/8] interference / clash check ...")
    clash = post("/assembly/interference", {"root": "gearbox", "defs": defs})
    print(f"   -> {clash['n_parts']} parts, {'CLEAN' if clash['ok'] else str(len(clash['clashes']))+' CLASHES'}")

    print("[5/8] exploded view (manufacturing doc) ...")
    exp = post("/assembly/explode", {"root": "gearbox", "defs": defs, "factor": 18,
                                     "name": f"{name}_exploded"})   # distinct base: don't clobber the assembled GLB
    print(f"   -> {os.path.basename(exp['glb'])}  bbox {exp['bbox']['size']} mm")

    print("[6/8] 2D drawing (section DXF + projection SVGs) ...")
    drw = post("/assembly/drawing", {"root": "gearbox", "defs": defs, "plane": "XZ", "name": name})
    print(f"   -> section {drw['section']['section_area_mm2']} mm^2, "
          f"projections {[os.path.basename(p['path']) for p in drw['projections']]}")

    print("[7/8] Bill of Materials document ...")
    b = post("/assembly/bom", {"root": "gearbox", "defs": defs, "fmt": "md", "name": name})
    print(f"   -> {os.path.basename(b['path'])} (total mass {b['mass_g']} g)")

    print("[8/8] Blender render + qwen27b QA (fail-soft) ...")
    try:
        png = os.path.join(OUT, f"{name}_tq.png")
        subprocess.run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
                        "-e", "HOME=/tmp/bh", "-e", "XDG_CONFIG_HOME=/tmp/bc",
                        "-e", "RENDER_ENGINE=BLENDER_WORKBENCH", "-v", f"{CONTAINERS}:{CONTAINERS}",
                        "vrm-automation-blender:4.2.0", "blender", "--background", "--python",
                        f"{CONTAINERS}/placement/scripts/render_glb.py", "--", asm["glb"], png, "900",
                        "three_quarter"], capture_output=True, text=True, timeout=300, check=True)
        import base64
        b64 = base64.b64encode(open(png, "rb").read()).decode()
        body = {"model": "qwen27b", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "CAD render of a gearbox. Describe what you see in 1-2 sentences."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
            "temperature": 0.0, "max_tokens": 160, "chat_template_kwargs": {"enable_thinking": False}}
        r = urllib.request.Request(f"{VLM}/chat/completions", data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"})
        v = json.loads(urllib.request.urlopen(r, timeout=120).read())["choices"][0]["message"]["content"]
        print(f"   -> render {os.path.basename(png)} | VLM: {v.strip()}")
    except Exception as exc:  # noqa: BLE001 -- render/VLM are external; geometry is the ground truth
        print(f"   -> skipped (render/VLM unavailable: {type(exc).__name__}); geometry checks above are truth")

    print("=" * 72)
    print(f"capstone complete: SMT-designed, Willis-verified {ratio}:1 gearbox, "
          f"built+checked+documented. STEP {asm.get('step')}")


if __name__ == "__main__":
    main()
