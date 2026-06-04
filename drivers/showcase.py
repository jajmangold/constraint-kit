#!/usr/bin/env python3
"""Showcase: a synthesized, mounted, fastened planetary gearbox — exercises the whole constraint-kit stack
in one artifact (host driver, stdlib only; talks to cadkit + reuses the Blender image + qwen27b).

  SMT synthesis (design gears to a ratio spec)  ->  hierarchical assembly (planetary + housing + 4 cap
  screws + 2020 extrusion frame, composed by ports)  ->  BOM + mass roll-up  ->  interference check  ->
  thread-fact provenance (spec compiler)  ->  STEP/GLB export  ->  Blender render  ->  qwen27b QA.

    python3 drivers/showcase.py [target_ratio]
"""
from __future__ import annotations

import base64
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

    print(f"[1/6] SMT synthesis: design a planetary gearset for {ratio}:1 ...")
    syn = post("/synthesize/planetary", {"target_ratio": ratio, "n_planets": 3, "module": 1.0,
                                         "width": 8, "objective": "compact"})
    if not syn.get("ok"):
        print("   infeasible:", syn.get("reason")); return
    cfg = syn["config"]
    print(f"   -> sun={cfg['sun_teeth']} planet={cfg['planet_teeth']} ring={cfg['ring_teeth']} "
          f"(achieved {syn['achieved_ratio']}:1)")
    pg = syn["part_spec"]                       # ready-to-build planetary geometry

    print("[2/6] thread-fact provenance for the M5 cap screws (spec compiler) ...")
    spec = post("/spec/resolve", {"query": "M5-0.8", "kind": "thread", "allow_live": False})
    sf = spec["facts"][0]["values"]
    print(f"   -> M5 nominal {sf['nominal_diameter']['value']}mm pitch {sf['pitch']['value']}mm "
          f"[{spec['sources'][0]['source_type']}, conf {spec['facts'][0]['confidence']}]")

    # hierarchical gearbox: a housing (plate + planetary + 4 cap screws) on a 2020 extrusion frame
    defs = {
        "housing": {
            "parts": [
                {"id": "plate", "type": "plate", "material": "aluminum",
                 "params": {"width": 90, "depth": 90, "thick": 8, "boss_d": 24, "boss_h": 3,
                            "bolt_d": 5, "bolt_circle": 74, "bolt_count": 4}},
                pg,
                *[{"id": f"screw{i}", "type": "bolt", "material": "steel",
                   "params": {"shank_d": 5, "length": 14, "head_d": 9, "head_h": 4}} for i in range(4)],
            ],
            "mates": [
                {"a": "plate", "a_joint": "mount", "b": "planetary", "b_joint": "base", "type": "coincident"},
                *[{"a": "plate", "a_joint": f"bolt{i}", "b": f"screw{i}", "b_joint": "seat",
                   "type": "coincident"} for i in range(4)],
            ],
            "ports": {"base": {"origin": [0, 0, -4]}},   # plate bottom face (thick 8, centred)
        },
        "frame": {"parts": [{"id": "rail", "type": "extrusion", "material": "aluminum",
                             "params": {"rail_size": "20x40", "length": 140}}],
                  "ports": {"top": {"origin": [0, 0, 140]}}},
        "gearbox": {"children": [
            {"instance": "frame", "ref": "frame", "place": {"port": "top", "at": [0, 0, 0]}},
            {"instance": "housing", "ref": "housing", "place": {"port": "base", "at": [0, 0, 0]}}]},
    }

    print("[3/6] hierarchical build (planetary + housing + 4 screws on a 2020 frame) ...")
    asm = post("/assembly/tree", {"root": "gearbox", "defs": defs, "name": "gearbox_showcase"})
    if not asm.get("ok"):
        print("   build failed:", asm); return
    print(f"   -> depth {asm['depth']}, {asm['part_count']} parts, mass {asm['mass_g']} g, "
          f"bbox {asm['bbox']['size']} mm")
    print(f"   -> BOM: {asm['bom']}")

    print("[4/6] interference / clash check ...")
    clash = post("/assembly/interference", {"root": "gearbox", "defs": defs})
    print(f"   -> {clash['n_parts']} parts, {clash['pairs_exact_checked']} exact pairs checked, "
          f"{'CLEAN' if clash['ok'] else str(len(clash['clashes']))+' clashes'}")

    glb = asm["glb"]
    png = os.path.join(OUT, "gearbox_showcase_tq.png")
    print(f"[5/6] render via Blender -> {os.path.basename(png)}")
    subprocess.run(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
                    "-e", "HOME=/tmp/bh", "-e", "XDG_CONFIG_HOME=/tmp/bc",
                    "-e", "RENDER_ENGINE=BLENDER_WORKBENCH", "-v", f"{CONTAINERS}:{CONTAINERS}",
                    "vrm-automation-blender:4.2.0", "blender", "--background", "--python",
                    f"{CONTAINERS}/placement/scripts/render_glb.py", "--", glb, png, "900",
                    "three_quarter"], capture_output=True, text=True)

    print("[6/6] qwen27b QA ...")
    b64 = base64.b64encode(open(png, "rb").read()).decode()
    body = {"model": "qwen27b", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "CAD render of a gearbox assembly. Describe what you see in 1-2 sentences."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
        "temperature": 0.0, "max_tokens": 180, "chat_template_kwargs": {"enable_thinking": False}}
    r = urllib.request.Request(f"{VLM}/chat/completions", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    verdict = json.loads(urllib.request.urlopen(r, timeout=120).read())["choices"][0]["message"]["content"]
    print("   VLM:", verdict.strip())
    print("=" * 72)
    print("artifacts:", glb, "|", asm["step"], "|", png)


if __name__ == "__main__":
    main()
