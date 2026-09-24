#!/usr/bin/env python3
"""Phase-0 end-to-end driver (host side, stdlib only).

Proves the whole reuse wiring on one wire:
  qwen27b plan  ->  cadkit generate+assemble+export  ->  Blender render (reused image)  ->  qwen27b VLM QA.

It calls the resident services; it spins up nothing new except the one-shot Blender render container
(the same Blender image used for rendering).

    python3 drivers/phase0.py "a 60mm steel mounting plate with a 20-tooth module-1 gear seated on its boss"
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request

CADKIT = os.environ.get("CADKIT_URL", "http://127.0.0.1:8195")
VLM = os.environ.get("VLM_URL", os.environ.get("QWEN_BASE_URL", "http://localhost:8000/v1"))
VLM_MODEL = os.environ.get("MODEL_VLM", "qwen27b")
BLENDER_IMAGE = os.environ.get("BLENDER_IMAGE", "blender:latest")
# CAD-QA renderer (constraint-kit local): WORKBENCH studio + cavity + outline -> features (teeth,
# grooves, bores) are actually legible. The old placement preset rendered parts as flat silhouettes.
CK_WORK_DIR = os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit")
RENDER_SCRIPT = os.path.join(CK_WORK_DIR, "tools/render_part.py")
CONTAINERS = os.path.join(os.path.dirname(CK_WORK_DIR), "containers")

DEFAULT_REQUEST = ("a 60mm square 6mm-thick aluminum mounting plate with a centered boss, and a "
                   "20-tooth module-1 steel spur gear seated on the boss (match boss diameter to "
                   "the gear bore)")


def _post(url: str, payload: dict, timeout: float = 240.0) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def build(request: str) -> dict:
    print(f"[1/4] plan+build via cadkit ({CADKIT}) ...")
    out = _post(f"{CADKIT}/build", {"request": request, "name": "phase0"})
    print("      spec:", json.dumps(out["spec"], indent=2))
    for p in out["parts"]:
        flag = "  (GEAR FALLBACK)" if p.get("gear_fallback") else ""
        print(f"      part {p['id']:<8} {p['type']:<10} {p['material']:<9} "
              f"mass={p['mass_g']:>9} g{flag}")
    print(f"      bbox size: {out['bbox']['size']}  glb_method={out['glb_method']}")
    print(f"      glb: {out['glb']}")
    return out


def render(glb: str, preset: str = "three_quarter", res: int = 800) -> str:
    # render_part.py takes (glb, png, res, azim, elev); keep the preset names as view aliases
    views = {"three_quarter": ("-35", "30"), "three_quarter_right": ("35", "30"),
             "hero": ("-25", "18"), "top": ("0", "85"), "front": ("0", "5")}
    azim, elev = views.get(preset, ("-35", "30"))
    png = os.path.splitext(glb)[0] + f"_{preset}.png"
    print(f"[2/4] render via {BLENDER_IMAGE} (reused) -> {png}")
    cmd = [
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp/blender-home", "-e", "XDG_CONFIG_HOME=/tmp/blender-config",
        "-e", "RENDER_ENGINE=BLENDER_WORKBENCH",
        "-v", f"{CONTAINERS}:{CONTAINERS}",
        BLENDER_IMAGE,
        "blender", "--background", "--python", RENDER_SCRIPT, "--",
        glb, png, str(res), azim, elev,
    ]
    res_proc = subprocess.run(cmd, capture_output=True, text=True)
    if res_proc.returncode != 0 or not os.path.exists(png):
        print(res_proc.stdout[-2000:])
        print(res_proc.stderr[-2000:], file=sys.stderr)
        raise RuntimeError("blender render failed")
    return png


def qa(png: str) -> str:
    print(f"[3/4] VLM QA via {VLM_MODEL} ({VLM}) ...")
    with open(png, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    payload = {
        "model": VLM_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": (
                "Render of a CAD assembly: a mounting plate with a spur gear seated on its central "
                "boss. Is the gear sitting on the boss (not floating, not sunk into the plate)? "
                "Answer YES or NO first, then one short sentence.")},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "temperature": 0.0, "max_tokens": 400,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    out = _post(f"{VLM}/chat/completions", payload)
    return (out["choices"][0]["message"].get("content") or "").strip()


def main() -> None:
    request = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REQUEST
    out = build(request)
    print(f"      stored in atlas: {out.get('stored')}")
    png = render(out["glb"])
    verdict = qa(png)
    # persist the render + verdict onto the stored assembly
    try:
        _post(f"{CADKIT}/qa", {"name": out["name"], "png": png, "verdict": verdict})
    except Exception as exc:  # noqa: BLE001
        print("      (warning: /qa persist failed:", exc, ")")
    print("[4/4] DONE")
    print("=" * 70)
    print("VLM verdict:", verdict)
    print("artifacts:", out["glb"], "|", out["step"], "|", png)


if __name__ == "__main__":
    main()
